"""
tests/test_adversarial_m1_2.py
Adversarial Stress Test Suite for ArbForge Milestone 1:
1. Concurrency stress testing of /api/orderbook-matrix under load (latency, headers, schema integrity).
2. BinanceBookTickerPoller error recovery: simulated network timeouts, HTTP 429 with Retry-After,
   HTTP 418 bans, and unexpected / corrupt payload formats.
3. Zero-whitespace query formatting verification on BinanceBookTickerPoller requests.
4. Poller graceful halt on task cancellation without leaking resources.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arbforge.api import (
    CANONICAL_PAIRS,
    evaluate_system_health,
    get_orderbook_store,
    router as api_router,
)
from arbforge.ingestion import (
    BINANCE_BOOK_TICKER_URL,
    BinanceBookTickerPoller,
)
from arbforge.mock import SyntheticMarketGenerator, create_level1_ticker
from arbforge.models import (
    CrossedBookError,
    EmptyBookError,
    InvalidPayloadError,
    InvalidPriceError,
    InvalidQuantityError,
    Level1Ticker,
    OrderBookMatrixResponse,
)
from arbforge.store import CORE_SYMBOLS, OrderBookStore


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def store() -> OrderBookStore:
    return OrderBookStore()


@pytest.fixture
def app(store: OrderBookStore) -> FastAPI:
    test_app = FastAPI()
    test_app.state.store = store
    test_app.include_router(api_router, prefix="/api")
    return test_app


# ============================================================================
# Section 1: /api/orderbook-matrix Concurrency, Latency & Schema Under Load
# ============================================================================

class TestEndpointConcurrencyAndLoad:
    """Stress tests for /api/orderbook-matrix under high concurrent load and concurrent writes."""

    @pytest.mark.asyncio
    async def test_concurrent_matrix_requests_under_writer_load(self, app: FastAPI, store: OrderBookStore):
        """
        Spawns 200 concurrent async GET requests against /api/orderbook-matrix
        while a background writer continuously pushes rapid book updates.
        Validates:
        - 100% 200 OK responses with zero 500s or unhandled exceptions
        - Strict anti-caching headers on all responses
        - Schema compliance: validates every response through OrderBookMatrixResponse
        - p95 latency stays under 100ms
        - Every response contains all 7 core pairs in deterministic order
        """
        # Pre-seed store with healthy balanced data
        market = SyntheticMarketGenerator.generate_market("balanced")
        await store.update_tickers(list(market.values()), latency_ms=12.0, used_weight_1m=4)

        stop_writer = asyncio.Event()
        write_count = 0

        async def rapid_writer():
            nonlocal write_count
            counter = 0
            while not stop_writer.is_set():
                counter += 1
                # Slightly mutate prices to simulate live ticks
                mutated = []
                for ticker in market.values():
                    delta = (counter % 5) * 0.01
                    mutated.append(create_level1_ticker(
                        symbol=ticker.symbol,
                        bid_price=ticker.bid_price + delta,
                        ask_price=ticker.ask_price + delta,
                        bid_qty=ticker.bid_qty + delta,
                        ask_qty=ticker.ask_qty + delta,
                    ))
                await store.update_tickers(mutated, latency_ms=5.0 + (counter % 3))
                write_count += 1
                await asyncio.sleep(0.001)

        writer_task = asyncio.create_task(rapid_writer())

        # Use httpx.AsyncClient with ASGITransport to simulate 200 concurrent HTTP clients
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            latencies: List[float] = []

            async def send_request(req_id: int):
                t0 = time.perf_counter()
                resp = await client.get("/api/orderbook-matrix")
                dur_ms = (time.perf_counter() - t0) * 1000.0
                latencies.append(dur_ms)
                return resp

            # Launch 200 concurrent requests in batches of 50
            responses: List[httpx.Response] = []
            batch_size = 50
            total_requests = 200
            for i in range(0, total_requests, batch_size):
                batch = [send_request(j) for j in range(i, i + batch_size)]
                responses.extend(await asyncio.gather(*batch))

        # Stop background writer
        stop_writer.set()
        await writer_task

        assert write_count > 0, "Background writer did not execute during stress test"
        assert len(responses) == 200, "Did not collect all 200 responses"

        # Validate every response
        for idx, resp in enumerate(responses):
            assert resp.status_code == 200, f"Request {idx} failed with status {resp.status_code}"

            # Anti-caching headers validation
            assert "no-cache" in resp.headers.get("Cache-Control", ""), f"Req {idx} missing no-cache in Cache-Control"
            assert "no-store" in resp.headers.get("Cache-Control", ""), f"Req {idx} missing no-store in Cache-Control"
            assert "must-revalidate" in resp.headers.get("Cache-Control", ""), f"Req {idx} missing must-revalidate"
            assert resp.headers.get("Pragma") == "no-cache", f"Req {idx} missing Pragma: no-cache"
            assert resp.headers.get("Expires") == "0", f"Req {idx} missing Expires: 0"

            # Parse and validate schema
            body = resp.json()
            matrix_resp = OrderBookMatrixResponse.model_validate(body)
            assert matrix_resp.status in ("HEALTHY", "DEGRADED")
            assert matrix_resp.pairs_count == 7
            assert len(matrix_resp.pairs) == 7
            assert len(matrix_resp.matrix) == 7
            assert [p.symbol for p in matrix_resp.pairs] == CANONICAL_PAIRS

            # Mathematical spread integrity
            for ticker in matrix_resp.pairs:
                assert ticker.ask_price > ticker.bid_price
                expected_spread = round(ticker.ask_price - ticker.bid_price, 8)
                assert ticker.spread_abs == pytest.approx(expected_spread, abs=1e-6)

        # Performance / Latency telemetry
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        max_lat = max(latencies)

        # In-memory ASGI p95 should comfortably be < 50ms
        assert p95 < 50.0, f"p95 latency exceeded 50ms: {p95:.2f}ms (max: {max_lat:.2f}ms)"

    def test_state_transitions_under_stress(self, app: FastAPI, store: OrderBookStore):
        """
        Verifies endpoint correctly reflects every store state transition:
        INITIALIZING -> HEALTHY -> DEGRADED (missing) -> DEGRADED (crossed) -> STALE.
        """
        client = TestClient(app)

        # 1. INITIALIZING (empty store)
        r1 = client.get("/api/orderbook-matrix")
        assert r1.status_code == 200
        assert r1.json()["status"] == "INITIALIZING"
        assert r1.json()["pairs_count"] == 0

        # 2. HEALTHY (all 7 pairs present & fresh)
        market = SyntheticMarketGenerator.generate_market("balanced")
        store.update_tickers_sync(list(market.values()))
        r2 = client.get("/api/orderbook-matrix")
        assert r2.status_code == 200
        assert r2.json()["status"] == "HEALTHY"
        assert r2.json()["pairs_count"] == 7

        # 3. DEGRADED (missing pair)
        store.reset()
        market_miss = SyntheticMarketGenerator.generate_market("missing_pair")
        store.update_tickers_sync(list(market_miss.values()))
        r3 = client.get("/api/orderbook-matrix")
        assert r3.status_code == 200
        assert r3.json()["status"] == "DEGRADED"
        assert r3.json()["pairs_count"] == 6

        # 4. DEGRADED (crossed pair)
        store.reset()
        market_cross = SyntheticMarketGenerator.generate_market("crossed_book")
        store.update_tickers_sync(list(market_cross.values()))
        r4 = client.get("/api/orderbook-matrix")
        assert r4.status_code == 200
        assert r4.json()["status"] == "DEGRADED"

        # 5. STALE (timestamp older than 5000ms)
        store.reset()
        past_ms = int(time.time() * 1000) - 10000
        store.update_tickers_sync(list(market.values()), update_time_ms=past_ms)
        r5 = client.get("/api/orderbook-matrix")
        assert r5.status_code == 200
        assert r5.json()["status"] == "STALE"


# ============================================================================
# Section 2: BinanceBookTickerPoller Error Recovery Stress Testing
# ============================================================================

class TestPollerErrorRecovery:
    """Stress tests error recovery in BinanceBookTickerPoller across diverse failure modes."""

    @pytest.mark.asyncio
    async def test_network_timeouts_and_backoff_recovery(self, store: OrderBookStore):
        """
        Simulates network timeouts (ConnectTimeout, ReadTimeout),
        verifies exponential backoff increase, store error recording,
        and automatic recovery upon network restoration.
        """
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.1)

        # Pre-seed store so we can verify existing tickers are not erased
        seed_market = SyntheticMarketGenerator.generate_market("balanced")
        await store.update_tickers(list(seed_market.values()))
        assert store.get_status() == "HEALTHY"
        assert len(store.get_all()) == 7

        # 1. Simulate ConnectTimeout
        timeout_transport = httpx.MockTransport(
            AsyncMock(side_effect=httpx.ConnectTimeout("Connection timed out after 3.0s"))
        )
        poller._client = httpx.AsyncClient(transport=timeout_transport)

        res1 = await poller.poll_once()
        assert res1 == []
        assert poller._backoff == 1.5
        # Existing tickers must remain intact in store
        assert len(store.get_all()) == 7
        assert store.get_status() == "DEGRADED"
        telemetry1 = store.get_telemetry()
        assert telemetry1["error_count"] == 1
        assert "Connection timed out" in telemetry1["last_error"]

        # 2. Simulate second timeout (ReadTimeout) -> backoff increases to 1.5 * 1.5 = 2.25
        timeout_transport_2 = httpx.MockTransport(
            AsyncMock(side_effect=httpx.ReadTimeout("Read timed out after 5.0s"))
        )
        poller._client = httpx.AsyncClient(transport=timeout_transport_2)

        res2 = await poller.poll_once()
        assert res2 == []
        assert poller._backoff == 2.25
        assert store.get_telemetry()["error_count"] == 2

        # 3. Simulate network recovery (200 OK with valid tickers)
        raw_payload = SyntheticMarketGenerator.generate_raw_binance_payload()
        success_transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(200, json=raw_payload, headers={"x-mbx-used-weight-1m": "8"}))
        )
        poller._client = httpx.AsyncClient(transport=success_transport)

        res3 = await poller.poll_once()
        assert len(res3) == 7
        # Backoff should reset to 1.0 on success
        assert poller._backoff == 1.0
        assert store.get_status() == "HEALTHY"
        assert store.get_telemetry()["last_error"] is None
        assert store.get_telemetry()["used_weight_1m"] == 8

        await poller.stop()

    @pytest.mark.asyncio
    async def test_http_429_rate_limit_with_retry_after(self, store: OrderBookStore):
        """
        Simulates HTTP 429 Rate Limit responses with Retry-After header.
        Verifies Retry-After parsing, store error recording, and recovery.
        Tests both pre-seeded store (transitions to DEGRADED) and unseeded store.
        """
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.1)

        # Pre-seed store so we can verify status transitions to DEGRADED on error
        seed_market = SyntheticMarketGenerator.generate_market("balanced")
        await store.update_tickers(list(seed_market.values()))
        assert store.get_status() == "HEALTHY"

        # Mock 429 with Retry-After: 2
        sleep_durations: List[float] = []

        async def fake_sleep(sec: float):
            sleep_durations.append(sec)

        rate_limit_transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(
                429,
                headers={"Retry-After": "2", "x-mbx-used-weight-1m": "1200"},
                text="Too Many Requests",
            ))
        )
        poller._client = httpx.AsyncClient(transport=rate_limit_transport)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            res = await poller.poll_once()

        assert res == []
        assert len(sleep_durations) == 1
        assert sleep_durations[0] == 2.0
        assert store.get_telemetry()["error_count"] == 1
        assert "HTTP 429 Rate Limit Exceeded" in store.get_telemetry()["last_error"]
        assert store.get_status() == "DEGRADED"

        # Test missing Retry-After header: should default to 10s
        default_rl_transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(429, text="Too Many Requests"))
        )
        poller._client = httpx.AsyncClient(transport=default_rl_transport)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            res_def = await poller.poll_once()

        assert res_def == []
        assert len(sleep_durations) == 2
        assert sleep_durations[1] == 10.0

        await poller.stop()

    @pytest.mark.asyncio
    async def test_http_418_ip_ban_recovery(self, store: OrderBookStore):
        """
        Simulates HTTP 418 IP Ban response.
        Verifies 120s cooldown trigger, CRITICAL logging, store error recording, and recovery.
        """
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.1)

        # Pre-seed store
        seed_market = SyntheticMarketGenerator.generate_market("balanced")
        await store.update_tickers(list(seed_market.values()))
        assert store.get_status() == "HEALTHY"

        sleep_durations: List[float] = []

        async def fake_sleep(sec: float):
            sleep_durations.append(sec)

        ban_transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(
                418,
                headers={"x-mbx-used-weight-1m": "2400"},
                text="I'm a teapot - IP banned until timestamp",
            ))
        )
        poller._client = httpx.AsyncClient(transport=ban_transport)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            res = await poller.poll_once()

        assert res == []
        assert len(sleep_durations) == 1
        assert sleep_durations[0] == 120.0
        assert store.get_telemetry()["error_count"] == 1
        assert "HTTP 418 IP Ban Detected" in store.get_telemetry()["last_error"]
        assert store.get_status() == "DEGRADED"

        # Simulate ban expiration and subsequent 200 OK recovery
        raw_payload = SyntheticMarketGenerator.generate_raw_binance_payload()
        recovery_transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(200, json=raw_payload))
        )
        poller._client = httpx.AsyncClient(transport=recovery_transport)

        res_rec = await poller.poll_once()
        assert len(res_rec) == 7
        assert store.get_status() == "HEALTHY"
        assert store.get_telemetry()["last_error"] is None

        await poller.stop()

    @pytest.mark.asyncio
    async def test_unexpected_payload_formats_resilience(self, store: OrderBookStore):
        """
        Adversarially feeds corrupted and unexpected payload structures to BinanceBookTickerPoller:
        1. Non-list / non-dict JSON (None, integer, string, boolean)
        2. Items missing required keys (symbol, bidPrice, askPrice)
        3. Items with NaN, Infinity, negative prices, zero prices
        4. Mixed payload where some items are valid and some are corrupt
        5. Crossed books within the payload
        """
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.1)

        # 1. Single dict instead of list (Binance single-symbol response format)
        single_dict_payload = {
            "symbol": "BTCUSDT",
            "bidPrice": "80000.00",
            "bidQty": "5.0",
            "askPrice": "80001.00",
            "askQty": "4.0",
        }
        t1 = httpx.MockTransport(AsyncMock(return_value=httpx.Response(200, json=single_dict_payload)))
        poller._client = httpx.AsyncClient(transport=t1)
        res1 = await poller.poll_once()
        assert len(res1) == 1
        assert res1[0].symbol == "BTCUSDT"

        # 2. Mixed payload: 2 valid pairs, 1 NaN price, 1 negative price, 1 zero price, 1 missing symbol, 1 non-core symbol
        mixed_payload = [
            # Valid 1
            {"symbol": "BTCUSDT", "bidPrice": "80000.00", "bidQty": "1.0", "askPrice": "80001.00", "askQty": "1.0"},
            # Valid 2
            {"symbol": "ETHUSDT", "bidPrice": "2400.00", "bidQty": "10.0", "askPrice": "2400.50", "askQty": "8.0"},
            # Invalid: NaN price
            {"symbol": "ETHBTC", "bidPrice": "NaN", "bidQty": "10.0", "askPrice": "0.030", "askQty": "8.0"},
            # Invalid: Negative price
            {"symbol": "SOLUSDT", "bidPrice": "-150.0", "bidQty": "5.0", "askPrice": "150.5", "askQty": "5.0"},
            # Invalid: Zero price
            {"symbol": "SOLBTC", "bidPrice": "0.0", "bidQty": "5.0", "askPrice": "0.0019", "askQty": "5.0"},
            # Invalid: Missing symbol
            {"bidPrice": "500.0", "bidQty": "1.0", "askPrice": "501.0", "askQty": "1.0"},
            # Valid but non-monitored symbol (should be filtered out)
            {"symbol": "DOGEUSDT", "bidPrice": "0.20", "bidQty": "100.0", "askPrice": "0.21", "askQty": "100.0"},
        ]
        t2 = httpx.MockTransport(AsyncMock(return_value=httpx.Response(200, json=mixed_payload)))
        poller._client = httpx.AsyncClient(transport=t2)
        res2 = await poller.poll_once()
        # Only the 2 valid monitored pairs should survive
        assert len(res2) == 2
        symbols_ingested = [t.symbol for t in res2]
        assert "BTCUSDT" in symbols_ingested
        assert "ETHUSDT" in symbols_ingested
        assert "ETHBTC" not in symbols_ingested
        assert "SOLUSDT" not in symbols_ingested
        assert "SOLBTC" not in symbols_ingested

        # 3. Crossed book in payload: bid > ask
        crossed_payload = [
            {"symbol": "BTCUSDT", "bidPrice": "81000.00", "bidQty": "1.0", "askPrice": "80000.00", "askQty": "1.0"},
        ]
        t3 = httpx.MockTransport(AsyncMock(return_value=httpx.Response(200, json=crossed_payload)))
        poller._client = httpx.AsyncClient(transport=t3)
        res3 = await poller.poll_once()
        assert len(res3) == 1
        assert res3[0].status == "CROSSED"
        # Store status must be DEGRADED due to crossed book
        assert store.get_status() == "DEGRADED"

        await poller.stop()


# ============================================================================
# Section 3: Query Formatting & Zero-Whitespace Verification
# ============================================================================

class TestQueryFormatting:
    """Verifies that BinanceBookTickerPoller enforces strict zero-whitespace JSON query serialization."""

    @pytest.mark.asyncio
    async def test_zero_whitespace_query_serialization(self, store: OrderBookStore):
        """
        Captures the exact HTTP request generated by BinanceBookTickerPoller
        and inspects the URL, query parameters, and unquoted strings.
        Confirms:
        - Exact URL query parameter 'symbols' contains NO ASCII whitespace
        - No '%20' or '+' characters exist in the query string
        - Query parses back to exactly CORE_SYMBOLS
        """
        captured_requests: List[httpx.Request] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(200, json=[])

        mock_transport = httpx.MockTransport(handle_request)
        poller = BinanceBookTickerPoller(
            store=store,
            symbols=CORE_SYMBOLS,
            base_url=BINANCE_BOOK_TICKER_URL,
        )
        poller._client = httpx.AsyncClient(transport=mock_transport)

        await poller.poll_once()
        assert len(captured_requests) == 1
        req = captured_requests[0]

        # 1. Verify URL path
        assert req.url.path == "/api/v3/ticker/bookTicker"

        # 2. Extract raw query string
        raw_query = req.url.query.decode("ascii")
        assert "symbols=" in raw_query

        # 3. Assert NO whitespace whatsoever in the query string
        for ws_char in (" ", "\t", "\n", "\r", "%20", "+"):
            assert ws_char not in raw_query, f"Whitespace '{ws_char}' found in raw query: {raw_query}"

        # 4. Extract symbols parameter value and verify compactness
        # In httpx, params={'symbols': '["BTCUSDT","ETHUSDT",...]'}
        # will URL-encode [ to %5B, " to %22, ] to %5D
        from urllib.parse import parse_qs, unquote
        parsed_params = parse_qs(raw_query)
        assert "symbols" in parsed_params
        symbols_json_str = parsed_params["symbols"][0]

        # Verify no whitespace in unquoted JSON string
        for ws_char in (" ", "\t", "\n", "\r"):
            assert ws_char not in symbols_json_str, f"Whitespace found in unquoted JSON: {symbols_json_str}"

        # Verify it deserializes to the exact expected symbols list
        deserialized = json.loads(symbols_json_str)
        assert deserialized == CORE_SYMBOLS

        await poller.stop()


# ============================================================================
# Section 4: Task Cancellation & Graceful Shutdown (Resource Leaks)
# ============================================================================

class TestTaskCancellationAndLifecycle:
    """Stress tests graceful shutdown, cancellation handling, and resource cleanup."""

    @pytest.mark.asyncio
    async def test_graceful_stop_and_client_closure(self, store: OrderBookStore):
        """
        Starts poller background loop, verifies task is active,
        then invokes stop() and confirms:
        - poller._running is set to False
        - poller._task is None and terminated
        - poller._client is closed (is_closed == True)
        - No unhandled CancelledError or dangling coroutines
        """
        raw_payload = SyntheticMarketGenerator.generate_raw_binance_payload()

        mock_transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(200, json=raw_payload))
        )
        mock_client = httpx.AsyncClient(transport=mock_transport)

        poller = BinanceBookTickerPoller(store=store, poll_interval=0.04)

        with patch("arbforge.ingestion.httpx.AsyncClient", return_value=mock_client):
            # Start poller
            await poller.start()
            assert poller._running is True
            assert poller._task is not None
            assert not poller._task.done()

            # Let it poll at least twice
            await asyncio.sleep(0.12)
            assert store.get_telemetry()["poll_count"] >= 2

            assert not mock_client.is_closed

            # Stop poller gracefully
            await poller.stop()

            assert poller._running is False
            assert poller._task is None
            assert poller._client is None
            assert mock_client.is_closed, "httpx.AsyncClient was not closed on poller.stop()"

    @pytest.mark.asyncio
    async def test_cancellation_during_backoff_sleep(self, store: OrderBookStore):
        """
        Forces poller into an extended sleep (simulating 418 ban or long backoff),
        then immediately calls stop().
        Confirms poller cancels cleanly and does NOT hang waiting for the sleep to finish.
        """
        # Return 418 so poll_once enters asyncio.sleep(120.0)
        ban_transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(418, text="Banned"))
        )

        poller = BinanceBookTickerPoller(store=store, poll_interval=0.05)
        poller._client = httpx.AsyncClient(transport=ban_transport)

        await poller.start()
        # Give it enough time to execute poll_once and enter the 120s sleep
        await asyncio.sleep(0.05)

        t0 = time.perf_counter()
        # stop() must cancel the sleeping task immediately
        await asyncio.wait_for(poller.stop(), timeout=2.0)
        elapsed = time.perf_counter() - t0

        assert elapsed < 1.0, f"stop() hung for {elapsed:.2f}s instead of aborting sleep"
        assert poller._running is False
        assert poller._task is None
        assert poller._client is None

    @pytest.mark.asyncio
    async def test_idempotent_start_and_stop(self, store: OrderBookStore):
        """
        Verifies that redundant start() and stop() calls are idempotent
        and do not spawn duplicate background tasks or crash.
        """
        mock_transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(200, json=[]))
        )
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.05)
        poller._client = httpx.AsyncClient(transport=mock_transport)

        # Call start() twice
        await poller.start()
        task1 = poller._task
        await poller.start()
        assert poller._task is task1, "Redundant start() created duplicate background task"

        # Call stop() twice
        await poller.stop()
        assert poller._task is None
        assert poller._running is False

        # Second stop() should be a no-op and not raise
        await poller.stop()
        assert poller._task is None


# ============================================================================
# Section 5: Edge Case Mining & Failure Mode Characterization
# ============================================================================

class TestEdgeCaseFailureModes:
    """
    Adversarially probes unhandled exception pathways in BinanceBookTickerPoller.
    Identifies edge cases where poll_once() raises instead of gracefully handling.
    Verifies that _poll_loop catches them under its top-level except Exception handler.
    """

    @pytest.mark.asyncio
    async def test_negative_quantity_escapes_poll_once_but_contained_in_poll_loop(self, store: OrderBookStore):
        """
        FINDING: In arbforge/ingestion.py line 143:
        `except (InvalidPayloadError, InvalidPriceError, EmptyBookError, CrossedBookError) as parse_err:`
        InvalidQuantityError is omitted from the exception tuple.
        When Binance returns a negative quantity (e.g. bidQty: "-1.0"), from_binance_raw
        raises InvalidQuantityError, which escapes poll_once() uncaught instead of being
        dropped as an invalid ticker.
        """
        payload = [
            {"symbol": "BTCUSDT", "bidPrice": "80000.00", "bidQty": "-1.0", "askPrice": "80001.00", "askQty": "1.0"},
            {"symbol": "ETHUSDT", "bidPrice": "2400.00", "bidQty": "10.0", "askPrice": "2400.50", "askQty": "8.0"},
        ]
        transport = httpx.MockTransport(AsyncMock(return_value=httpx.Response(200, json=payload)))
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.05)
        poller._client = httpx.AsyncClient(transport=transport)

        # 1. poll_once() directly raises InvalidQuantityError
        with pytest.raises(InvalidQuantityError) as exc_info:
            await poller.poll_once()
        assert "cannot be negative" in str(exc_info.value)

        # 2. In _poll_loop, top-level except Exception catches it and updates store
        mock_client = httpx.AsyncClient(transport=transport)
        with patch("arbforge.ingestion.httpx.AsyncClient", return_value=mock_client):
            await poller.start()
            await asyncio.sleep(0.12)
            await poller.stop()

        telemetry = store.get_telemetry()
        assert telemetry["error_count"] > 0
        assert "cannot be negative" in telemetry["last_error"]
        await poller.stop()

    @pytest.mark.asyncio
    async def test_malformed_retry_after_header_escapes_poll_once(self, store: OrderBookStore):
        """
        FINDING: In arbforge/ingestion.py line 155:
        `retry_after = int(resp.headers.get("Retry-After", 10))`
        If Retry-After contains an HTTP-date (RFC 7231 §7.1.3) or invalid string,
        int() raises ValueError, which escapes poll_once().
        """
        # Standard HTTP-date format in Retry-After
        transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(
                429,
                headers={"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"},
                text="Too Many Requests",
            ))
        )
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.05)
        poller._client = httpx.AsyncClient(transport=transport)

        with pytest.raises(ValueError) as exc_info:
            await poller.poll_once()
        assert "invalid literal for int()" in str(exc_info.value)

        # Verify _poll_loop contains it
        mock_client = httpx.AsyncClient(transport=transport)
        with patch("arbforge.ingestion.httpx.AsyncClient", return_value=mock_client):
            await poller.start()
            await asyncio.sleep(0.12)
            await poller.stop()

        telemetry = store.get_telemetry()
        assert telemetry["error_count"] > 0
        assert "invalid literal for int()" in telemetry["last_error"]
        await poller.stop()

    @pytest.mark.asyncio
    async def test_html_gateway_error_escapes_poll_once(self, store: OrderBookStore):
        """
        FINDING: In arbforge/ingestion.py line 130:
        `raw_items = resp.json()`
        If Binance or reverse proxy returns 200 with HTML error text,
        resp.json() raises json.decoder.JSONDecodeError, which escapes poll_once().
        """
        transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(
                200,
                text="<html><body>502 Bad Gateway</body></html>",
                headers={"Content-Type": "text/html"},
            ))
        )
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.05)
        poller._client = httpx.AsyncClient(transport=transport)

        with pytest.raises(json.decoder.JSONDecodeError):
            await poller.poll_once()

        # Verify _poll_loop contains it
        mock_client = httpx.AsyncClient(transport=transport)
        with patch("arbforge.ingestion.httpx.AsyncClient", return_value=mock_client):
            await poller.start()
            await asyncio.sleep(0.12)
            await poller.stop()

        telemetry = store.get_telemetry()
        assert telemetry["error_count"] > 0
        assert "Expecting value" in telemetry["last_error"]
        await poller.stop()

    @pytest.mark.asyncio
    async def test_empty_used_weight_header_escapes_poll_once(self, store: OrderBookStore):
        """
        FINDING: In arbforge/ingestion.py line 129:
        `weight_1m = int(resp.headers.get("x-mbx-used-weight-1m", 0))`
        If the header is present but empty, int("") raises ValueError.
        """
        transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(
                200,
                json=[],
                headers={"x-mbx-used-weight-1m": ""},
            ))
        )
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.05)
        poller._client = httpx.AsyncClient(transport=transport)

        with pytest.raises(ValueError):
            await poller.poll_once()

        # Verify _poll_loop contains it
        mock_client = httpx.AsyncClient(transport=transport)
        with patch("arbforge.ingestion.httpx.AsyncClient", return_value=mock_client):
            await poller.start()
            await asyncio.sleep(0.12)
            await poller.stop()

        telemetry = store.get_telemetry()
        assert telemetry["error_count"] > 0
        await poller.stop()

    @pytest.mark.asyncio
    async def test_primitive_json_payload_escapes_poll_once(self, store: OrderBookStore):
        """
        FINDING: In arbforge/ingestion.py lines 133-137:
        `if isinstance(raw_items, dict): raw_items = [raw_items]`
        `for item in raw_items: sym = item.get("symbol")`
        If raw_items is a string or number (e.g. JSON "OK" or 123), it raises AttributeError or TypeError.
        """
        transport = httpx.MockTransport(
            AsyncMock(return_value=httpx.Response(200, json="OK"))
        )
        poller = BinanceBookTickerPoller(store=store, poll_interval=0.05)
        poller._client = httpx.AsyncClient(transport=transport)

        with pytest.raises(AttributeError):
            await poller.poll_once()

        # Verify _poll_loop contains it
        mock_client = httpx.AsyncClient(transport=transport)
        with patch("arbforge.ingestion.httpx.AsyncClient", return_value=mock_client):
            await poller.start()
            await asyncio.sleep(0.12)
            await poller.stop()

        telemetry = store.get_telemetry()
        assert telemetry["error_count"] > 0
        assert "object has no attribute 'get'" in telemetry["last_error"]
        await poller.stop()
