"""
tests/test_store.py
Unit and concurrency tests for ArbForge OrderBookStore.
Verifies Atomic Snapshot Reference Swap pattern, lockless reads, canonical ordering,
staleness detection, health status transitions, and telemetry.
"""

import asyncio
import time
import pytest

from arbforge.mock import SyntheticMarketGenerator, create_level1_ticker
from arbforge.models import Level1Ticker
from arbforge.store import CORE_SYMBOLS, OrderBookStore


@pytest.fixture
def store() -> OrderBookStore:
    return OrderBookStore()


def test_initial_store_state(store: OrderBookStore):
    """Verifies that an unseeded store starts in INITIALIZING state and is stale."""
    assert store.get_all() == []
    assert store.get("BTCUSDT") is None
    assert store.get_ticker("BTCUSDT") is None
    assert store.get_matrix() == {}
    assert store.is_stale() is True
    assert store.get_status() == "INITIALIZING"

    telemetry = store.get_telemetry()
    assert telemetry["poll_count"] == 0
    assert telemetry["error_count"] == 0
    assert telemetry["pair_count"] == 0


@pytest.mark.asyncio
async def test_update_tickers_and_lockless_read(store: OrderBookStore):
    """Verifies atomic snapshot reference swap and lockless reads."""
    market = SyntheticMarketGenerator.generate_market("balanced")
    tickers = list(market.values())

    await store.update_tickers(tickers, latency_ms=10.5, used_weight_1m=4)

    assert store.is_stale() is False
    assert store.get_status() == "HEALTHY"

    # Verify lookups
    btc = store.get("BTCUSDT")
    assert btc is not None
    assert btc.symbol == "BTCUSDT"
    assert btc.bid_price == 80000.00

    # Verify case-insensitive lookup
    assert store.get("btcusdt") is not None
    assert store.get("btcusdt").symbol == "BTCUSDT"

    # Verify telemetry
    telemetry = store.get_telemetry()
    assert telemetry["poll_count"] == 1
    assert telemetry["last_latency_ms"] == 10.5
    assert telemetry["used_weight_1m"] == 4
    assert telemetry["pair_count"] == 7


def test_canonical_ordering(store: OrderBookStore):
    """Verifies that get_all(ordered=True) maintains deterministic canonical sequence."""
    market = SyntheticMarketGenerator.generate_market("balanced")
    # Feed in reverse order
    reversed_tickers = list(reversed(list(market.values())))
    store.update_tickers_sync(reversed_tickers)

    ordered = store.get_all(ordered=True)
    ordered_symbols = [t.symbol for t in ordered]
    assert ordered_symbols == CORE_SYMBOLS

    # Extra non-core symbol should be appended at the end
    extra_ticker = create_level1_ticker(
        symbol="DOGEUSDT",
        bid_price=0.20,
        ask_price=0.21,
        bid_qty=1000.0,
        ask_qty=1000.0,
    )
    store.update_tickers_sync([extra_ticker])

    ordered_with_extra = store.get_all(ordered=True)
    assert [t.symbol for t in ordered_with_extra] == CORE_SYMBOLS + ["DOGEUSDT"]


def test_staleness_detection(store: OrderBookStore):
    """Verifies is_stale detection with custom age thresholds."""
    market = SyntheticMarketGenerator.generate_market("balanced")
    # Update with past timestamp (10 seconds ago)
    past_ms = int(time.time() * 1000) - 10000
    store.update_tickers_sync(list(market.values()), update_time_ms=past_ms)

    assert store.is_stale(max_age_sec=5.0) is True
    assert store.is_stale(max_age_ms=5000) is True
    assert store.is_stale(max_age_sec=15.0) is False
    assert store.get_status(max_age_sec=5.0) == "STALE"


def test_health_status_transitions(store: OrderBookStore):
    """Verifies state transitions: INITIALIZING -> HEALTHY -> DEGRADED -> STALE."""
    # 1. INITIALIZING
    assert store.get_status() == "INITIALIZING"

    # 2. HEALTHY (all 7 core pairs fresh)
    market = SyntheticMarketGenerator.generate_market("balanced")
    store.update_tickers_sync(list(market.values()))
    assert store.get_status() == "HEALTHY"

    # 3. DEGRADED (missing pair)
    store.reset()
    market_missing = SyntheticMarketGenerator.generate_market("missing_pair")
    store.update_tickers_sync(list(market_missing.values()))
    assert store.get_status() == "DEGRADED"

    # 4. DEGRADED (crossed book)
    store.reset()
    market_crossed = SyntheticMarketGenerator.generate_market("crossed_book")
    store.update_tickers_sync(list(market_crossed.values()))
    assert store.get_status() == "DEGRADED"

    # 5. DEGRADED (error passed)
    store.reset()
    store.update_tickers_sync(list(market.values()), error="Connection reset by peer")
    assert store.get_status() == "DEGRADED"

    # 6. STALE (time exceeded)
    store.reset()
    past_ms = int(time.time() * 1000) - 20000
    store.update_tickers_sync(list(market.values()), update_time_ms=past_ms)
    assert store.get_status(max_age_sec=5.0) == "STALE"


@pytest.mark.asyncio
async def test_update_backward_compatibility(store: OrderBookStore):
    """Verifies update() handles raw dict items."""
    raw_dicts = [
        {
            "symbol": "BTCUSDT",
            "bidPrice": "80000.00",
            "bidQty": "4.5",
            "askPrice": "80001.00",
            "askQty": "3.2",
        },
        {
            "symbol": "ETHUSDT",
            "bid_price": 2400.0,
            "bid_qty": 20.0,
            "ask_price": 2400.2,
            "ask_qty": 18.0,
        },
    ]
    await store.update(raw_dicts)
    assert len(store.get_all()) == 2
    assert store.get("BTCUSDT").ask_price == 80001.00
    assert store.get("ETHUSDT").bid_price == 2400.00


def test_get_matrix_response(store: OrderBookStore):
    """Verifies get_matrix_response builds valid OrderBookMatrixResponse."""
    market = SyntheticMarketGenerator.generate_market("balanced")
    store.update_tickers_sync(list(market.values()), latency_ms=14.2, used_weight_1m=4)

    resp = store.get_matrix_response()
    assert resp.status == "HEALTHY"
    assert resp.pairs_count == 7
    assert len(resp.pairs) == 7
    assert len(resp.matrix) == 7
    assert resp.latency_ms == 14.2
    assert resp.used_weight_1m == 4
    assert resp.poll_count == 1
    assert resp.error_count == 0


def test_reset(store: OrderBookStore):
    """Verifies reset cleans up all state."""
    market = SyntheticMarketGenerator.generate_market("balanced")
    store.update_tickers_sync(list(market.values()))
    assert len(store.get_all()) == 7

    store.reset()
    assert len(store.get_all()) == 0
    assert store.get_status() == "INITIALIZING"
    assert store.get_telemetry()["poll_count"] == 0


@pytest.mark.asyncio
async def test_concurrency_stress(store: OrderBookStore):
    """Verifies concurrent readers and writers without race conditions or locks contention."""
    market = SyntheticMarketGenerator.generate_market("balanced")
    tickers = list(market.values())

    stop_event = asyncio.Event()

    async def writer():
        for i in range(50):
            await store.update_tickers(tickers, latency_ms=float(i))
            await asyncio.sleep(0.001)
        stop_event.set()

    async def reader():
        reads = 0
        while not stop_event.is_set():
            all_tickers = store.get_all(ordered=True)
            matrix = store.get_matrix()
            status = store.get_status()
            reads += 1
            await asyncio.sleep(0.0005)
        return reads

    writer_task = asyncio.create_task(writer())
    reader_tasks = [asyncio.create_task(reader()) for _ in range(5)]

    await writer_task
    read_counts = await asyncio.gather(*reader_tasks)

    for count in read_counts:
        assert count > 0
    assert store.get_telemetry()["poll_count"] == 50
