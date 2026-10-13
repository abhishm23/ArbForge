"""
tests/test_api_matrix.py
Hermetic unit and integration tests for ArbForge GET /api/orderbook-matrix endpoint
and SyntheticMarketGenerator. Completely offline, zero network access required.
"""

import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arbforge.api import CANONICAL_PAIRS, get_orderbook_store, router as api_router
from arbforge.ingestion import BinanceBookTickerPoller
from arbforge.mock import (
    MockBinanceIngestionService,
    SyntheticMarketGenerator,
    create_level1_ticker,
)
from arbforge.store import OrderBookStore


@pytest.fixture
def test_store() -> OrderBookStore:
    """Provides a fresh, isolated OrderBookStore for each test."""
    return OrderBookStore()


@pytest.fixture
def app(test_store: OrderBookStore) -> FastAPI:
    """Creates a FastAPI test application with injected OrderBookStore."""
    test_app = FastAPI()
    test_app.state.store = test_store
    test_app.include_router(api_router, prefix="/api")
    return test_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Provides a TestClient connected to the test application."""
    return TestClient(app)


def test_orderbook_matrix_endpoint_healthy(client: TestClient, test_store: OrderBookStore):
    """Verifies that /api/orderbook-matrix returns HTTP 200 with HEALTHY status and valid timestamps."""
    market = SyntheticMarketGenerator.generate_market(scenario="balanced")
    test_store.update_tickers_sync(list(market.values()), latency_ms=15.0, used_weight_1m=4)

    response = client.get("/api/orderbook-matrix")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    data = response.json()
    assert data["status"] == "HEALTHY"
    assert "timestamp_ms" in data
    assert "iso_time" in data
    assert data["timestamp_ms"] > 0
    assert data["latency_ms"] == 15.0
    assert data["used_weight_1m"] == 4


def test_orderbook_matrix_contains_all_7_pairs(client: TestClient, test_store: OrderBookStore):
    """Verifies that /api/orderbook-matrix returns all 7 monitored cryptocurrency pairs."""
    market = SyntheticMarketGenerator.generate_market(scenario="balanced")
    test_store.update_tickers_sync(list(market.values()))

    response = client.get("/api/orderbook-matrix")
    assert response.status_code == 200
    data = response.json()

    assert data["pairs_count"] == 7
    assert len(data["pairs"]) == 7
    assert len(data["matrix"]) == 7

    returned_symbols = [p["symbol"] for p in data["pairs"]]
    for expected_symbol in CANONICAL_PAIRS:
        assert expected_symbol in returned_symbols
        assert expected_symbol in data["matrix"]


def test_orderbook_matrix_canonical_order(client: TestClient, test_store: OrderBookStore):
    """Verifies that pairs list preserves deterministic canonical sequence."""
    market = SyntheticMarketGenerator.generate_market(scenario="balanced")
    test_store.update_tickers_sync(list(market.values()))

    response = client.get("/api/orderbook-matrix")
    data = response.json()

    returned_symbols = [p["symbol"] for p in data["pairs"]]
    assert returned_symbols == CANONICAL_PAIRS


def test_orderbook_matrix_spread_math(client: TestClient, test_store: OrderBookStore):
    """Verifies precision of spread_abs, spread_bps_mid, and spread_bps_bid calculations."""
    ticker = create_level1_ticker(
        symbol="BTCUSDT",
        bid_price=80000.0,
        ask_price=80008.0,
        bid_qty=1.0,
        ask_qty=1.0,
    )
    test_store.update_tickers_sync([ticker])

    response = client.get("/api/orderbook-matrix")
    data = response.json()
    btc_entry = data["matrix"]["BTCUSDT"]

    # spread_abs = 80008.0 - 80000.0 = 8.0
    assert btc_entry["spread_abs"] == pytest.approx(8.0, abs=1e-5)

    # mid = 80004.0, spread_bps_mid = (8.0 / 80004.0) * 10000 = 0.99995
    assert btc_entry["spread_bps_mid"] == pytest.approx(0.99995, abs=1e-3)

    # spread_bps_bid = (8.0 / 80000.0) * 10000 = 1.00000
    assert btc_entry["spread_bps_bid"] == pytest.approx(1.0, abs=1e-3)


def test_orderbook_matrix_cache_headers(client: TestClient, test_store: OrderBookStore):
    """Verifies anti-caching HTTP response headers."""
    market = SyntheticMarketGenerator.generate_market()
    test_store.update_tickers_sync(list(market.values()))

    response = client.get("/api/orderbook-matrix")
    assert "no-cache" in response.headers.get("Cache-Control", "")
    assert "no-store" in response.headers.get("Cache-Control", "")
    assert response.headers.get("Pragma") == "no-cache"
    assert response.headers.get("Expires") == "0"


def test_orderbook_matrix_stale_status(client: TestClient, test_store: OrderBookStore):
    """Verifies system marks status STALE when snapshot is older than 5000ms."""
    past_ms = int(time.time() * 1000) - 15000  # 15s ago
    market = SyntheticMarketGenerator.generate_market(timestamp_ms=past_ms)
    test_store.update_tickers_sync(list(market.values()), update_time_ms=past_ms)

    response = client.get("/api/orderbook-matrix")
    data = response.json()
    assert data["status"] == "STALE"


def test_orderbook_matrix_degraded_missing_pair(client: TestClient, test_store: OrderBookStore):
    """Verifies system marks status DEGRADED when only 6 of 7 pairs are available."""
    market = SyntheticMarketGenerator.generate_market(scenario="missing_pair")
    test_store.update_tickers_sync(list(market.values()))

    response = client.get("/api/orderbook-matrix")
    data = response.json()
    assert data["status"] == "DEGRADED"
    assert data["pairs_count"] == 6


def test_orderbook_matrix_degraded_crossed_book(client: TestClient, test_store: OrderBookStore):
    """Verifies system marks status DEGRADED when a pair has a crossed book (bid >= ask)."""
    market = SyntheticMarketGenerator.generate_market(scenario="crossed_book")
    test_store.update_tickers_sync(list(market.values()))

    response = client.get("/api/orderbook-matrix")
    data = response.json()
    assert data["status"] == "DEGRADED"


def test_orderbook_matrix_initializing_empty_store(client: TestClient):
    """Verifies empty store returns HTTP 200 with INITIALIZING status without throwing 500 error."""
    response = client.get("/api/orderbook-matrix")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "INITIALIZING"
    assert data["pairs_count"] == 0
    assert data["pairs"] == []
    assert data["matrix"] == {}


def test_dependency_injection_override(app: FastAPI):
    """Verifies FastAPI dependency override mechanism on get_orderbook_store."""
    custom_store = OrderBookStore()
    ticker = create_level1_ticker("BTCUSDT", 75000.0, 75001.0, 5.0, 5.0)
    custom_store.update_tickers_sync([ticker])

    app.dependency_overrides[get_orderbook_store] = lambda: custom_store
    client = TestClient(app)

    response = client.get("/api/orderbook-matrix")
    data = response.json()
    assert "BTCUSDT" in data["matrix"]
    assert data["matrix"]["BTCUSDT"]["bid_price"] == 75000.0
    app.dependency_overrides.clear()


def test_synthetic_generator_scenarios():
    """Verifies synthetic generator generates all scenarios accurately."""
    # Balanced
    m_bal = SyntheticMarketGenerator.generate_market("balanced")
    assert len(m_bal) == 7
    assert m_bal["BTCUSDT"].status == "ACTIVE"

    # Arbitrage scenarios
    m_eth = SyntheticMarketGenerator.generate_market("arbitrage_eth")
    assert m_eth["ETHBTC"].ask_price == 0.029400

    m_sol = SyntheticMarketGenerator.generate_market("arbitrage_sol")
    assert m_sol["SOLBTC"].ask_price == 0.0018400

    m_bnb = SyntheticMarketGenerator.generate_market("arbitrage_bnb")
    assert m_bnb["BNBBTC"].ask_price == 0.007350

    # Sub-threshold
    m_sub = SyntheticMarketGenerator.generate_market("sub_threshold")
    assert m_sub["ETHBTC"].ask_price == 0.029880

    # Locked
    m_lock = SyntheticMarketGenerator.generate_market("locked_book")
    assert m_lock["ETHUSDT"].status == "LOCKED"

    # Crossed
    m_cross = SyntheticMarketGenerator.generate_market("crossed_book")
    assert m_cross["BTCUSDT"].status == "CROSSED"


def test_synthetic_generator_raw_payload_structure():
    """Verifies raw Binance bookTicker dictionary generator for ingestion testing."""
    raw = SyntheticMarketGenerator.generate_raw_binance_payload()
    assert len(raw) == 7
    first = raw[0]
    assert "symbol" in first
    assert "bidPrice" in first
    assert "bidQty" in first
    assert "askPrice" in first
    assert "askQty" in first
    assert isinstance(first["bidPrice"], str)
    assert "." in first["bidPrice"]


@pytest.mark.asyncio
async def test_poller_mock_mode():
    """Verifies BinanceBookTickerPoller executes cleanly in mock mode without network."""
    store = OrderBookStore()
    poller = BinanceBookTickerPoller(store=store, mock_mode=True)
    tickers = await poller.poll_once()
    assert len(tickers) == 7
    assert len(store.get_all()) == 7
    assert store.get_status() == "HEALTHY"


@pytest.mark.asyncio
async def test_mock_ingestion_service():
    """Verifies MockBinanceIngestionService seeds store with tick."""
    store = OrderBookStore()
    mock_service = MockBinanceIngestionService(store=store)
    await mock_service.start()
    assert len(store.get_all()) == 7
    await mock_service.stop()
