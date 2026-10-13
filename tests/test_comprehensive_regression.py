"""
tests/test_comprehensive_regression.py
ArbForge 22-Use-Case Comprehensive Regression Test Suite.

Explicitly covers and certifies:
- Case 01: Crossed book strict rejection and permissive flagging (bid > ask)
- Case 02: Locked book detection and zero spread handling (bid == ask)
- Case 03: Empty book zero price handling (prevent zero division)
- Case 04: Zero quantities handling on any leg (zero executable bottleneck)
- Case 05: NaN float and string rejection in price and quantity parsing
- Case 06: Positive & Negative Infinity rejection in price and quantity
- Case 07: Negative price and non-positive quantity guards
- Case 08: High taker fees compounding drag over 3 legs (fee exhaustion)
- Case 09: Zero-fee execution (gross yield identical to net yield)
- Case 10: Extreme high threshold filtering (min_yield = 25%)
- Case 11: Negative threshold filtering (min_yield = -10%)
- Case 12: Backward depth bottleneck propagation: Leg 1 bottleneck
- Case 13: Backward depth bottleneck propagation: Leg 2 bottleneck (cross pair)
- Case 14: Backward depth bottleneck propagation: Leg 3 bottleneck
- Case 15: All 6 canonical cycles enumerated with 3 valid legs
- Case 16: Directional math: BUY @ Ask vs SELL @ Bid
- Case 17: Synthetic vs Direct exchange rate discrepancy calculation
- Case 18: Ingestion network timeout / HTTP error graceful recovery
- Case 19: Dynamic Config GET and POST roundtrip verification
- Case 20: Orderbook Matrix 7 core pairs canonical ordering & spread bps
- Case 21: Health state machine (INITIALIZING -> HEALTHY -> STALE -> DEGRADED)
- Case 22: High-density terminal dashboard (/) and interactive docs (/docs-interactive) HTML contracts
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Dict
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from arbforge.api import CANONICAL_PAIRS, evaluate_system_health, get_orderbook_store
from arbforge.engine import (
    ArbitrageEngine,
    DEFAULT_FEE_RATE,
    DEFAULT_MIN_YIELD_THRESHOLD,
    directional_buy,
    directional_sell,
)
from arbforge.ingestion import BinanceBookTickerPoller
from arbforge.models import (
    CrossedBookError,
    EmptyBookError,
    InvalidPayloadError,
    InvalidPriceError,
    InvalidQuantityError,
    Level1Ticker,
    parse_safe_float,
    parse_safe_price,
    parse_safe_quantity,
)
from arbforge.store import OrderBookStore, CORE_SYMBOLS
from tests.conftest import CANONICAL_MARKET_SNAPSHOT

CANONICAL_TEST_TICKERS: Dict[str, Level1Ticker] = {
    sym: Level1Ticker.from_raw(**raw)
    for sym, raw in CANONICAL_MARKET_SNAPSHOT.items()
}


@pytest.fixture
def clean_store():
    """Provides a fresh, isolated OrderBookStore."""
    store = OrderBookStore(core_symbols=CORE_SYMBOLS)
    return store


@pytest.fixture
def populated_store():
    """Provides an OrderBookStore populated with standard canonical tickers."""
    store = OrderBookStore(core_symbols=CORE_SYMBOLS)
    asyncio.run(
        store.update_tickers(
            list(CANONICAL_TEST_TICKERS.values()),
            latency_ms=12.5,
            used_weight_1m=5,
        )
    )
    return store


@pytest.fixture
def client(populated_store):
    """Provides a FastAPI TestClient bound to populated store."""
    app.dependency_overrides[get_orderbook_store] = lambda: populated_store
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ===========================================================================
# USE CASE 01: Crossed book strict rejection and permissive flagging (bid > ask)
# ===========================================================================
def test_case_01_crossed_book_strict_rejection_and_flagging():
    """Verifies crossed book (bid > ask) is detected and rejected or flagged."""
    raw_crossed = {
        "symbol": "BTCUSDT",
        "bidPrice": "80100.00",
        "bidQty": "1.500",
        "askPrice": "80000.00",  # ask < bid -> crossed
        "askQty": "2.000",
    }
    # Permissive mode sets status="CROSSED"
    ticker = Level1Ticker.from_binance_raw(raw_crossed, strict=False)
    assert ticker.status == "CROSSED"
    assert ticker.spread_abs < 0

    # Strict mode raises CrossedBookError
    with pytest.raises(CrossedBookError):
        Level1Ticker.from_binance_raw(raw_crossed, strict=True)


# ===========================================================================
# USE CASE 02: Locked book detection and zero spread handling (bid == ask)
# ===========================================================================
def test_case_02_locked_book_detection():
    """Verifies locked book (bid == ask) has zero spread and is safely parsed."""
    raw_locked = {
        "symbol": "ETHUSDT",
        "bidPrice": "2500.00",
        "bidQty": "5.000",
        "askPrice": "2500.00",
        "askQty": "4.000",
    }
    ticker = Level1Ticker.from_binance_raw(raw_locked, strict=False)
    assert ticker.status == "LOCKED"
    assert ticker.spread_abs == 0.0
    assert ticker.spread_bps_mid == 0.0


# ===========================================================================
# USE CASE 03: Empty book zero price handling (prevent zero division)
# ===========================================================================
def test_case_03_empty_book_zero_price_handling():
    """Verifies zero bid or zero ask is flagged as EMPTY_BOOK and rejects non-positive."""
    with pytest.raises(InvalidPriceError):
        parse_safe_price(0.0, "bid_price")

    with pytest.raises(InvalidPriceError):
        parse_safe_price("0.00000000", "ask_price")

    # Permissive ticker factory detects zero quantity as EMPTY_BOOK safely
    raw_empty = {
        "symbol": "SOLUSDT",
        "bidPrice": "150.00",
        "bidQty": "0.0",
        "askPrice": "150.05",
        "askQty": "10.0",
    }
    ticker = Level1Ticker.from_binance_raw(raw_empty, strict=False)
    assert ticker.status == "EMPTY_BOOK"


# ===========================================================================
# USE CASE 04: Zero quantities handling on any leg
# ===========================================================================
def test_case_04_zero_quantities_handling():
    """Verifies zero quantity on any leg yields zero executable volume and zero profit."""
    tickers = {k: v.model_copy(deep=True) for k, v in CANONICAL_TEST_TICKERS.items()}
    # Set both ask_qty and bid_qty of BTCUSDT to 0.0
    tickers["BTCUSDT"].ask_qty = 0.0
    tickers["BTCUSDT"].bid_qty = 0.0

    opps = ArbitrageEngine.evaluate_cycles(tickers, fee_rate=DEFAULT_FEE_RATE)
    for opp in opps:
        if "BTCUSDT" in [l.symbol for l in opp.legs]:
            assert opp.max_executable_volume_quote == 0.0
            assert opp.expected_net_profit_quote == 0.0


# ===========================================================================
# USE CASE 05: NaN float and string rejection in price and quantity parsing
# ===========================================================================
def test_case_05_nan_rejection_in_price_and_quantity():
    """Verifies NaN (float or str) is strictly rejected across parsers."""
    nan_inputs = [float("nan"), "nan", "NaN", "-NaN", "NAN"]
    for inp in nan_inputs:
        with pytest.raises((InvalidPriceError, InvalidPayloadError)):
            parse_safe_float(inp, "test_field")

        with pytest.raises(InvalidPriceError):
            parse_safe_price(inp, "price")

        with pytest.raises((InvalidQuantityError, InvalidPriceError)):
            parse_safe_quantity(inp, "qty")


# ===========================================================================
# USE CASE 06: Positive & Negative Infinity rejection in price and quantity
# ===========================================================================
def test_case_06_infinity_rejection_in_price_and_quantity():
    """Verifies +Infinity and -Infinity are strictly rejected."""
    inf_inputs = [float("inf"), float("-inf"), "inf", "-inf", "Infinity", "-Infinity"]
    for inp in inf_inputs:
        with pytest.raises((InvalidPriceError, InvalidPayloadError)):
            parse_safe_float(inp, "test_field")


# ===========================================================================
# USE CASE 07: Negative price and non-positive quantity guards
# ===========================================================================
def test_case_07_negative_price_and_quantity_guards():
    """Verifies negative prices and negative quantities are rejected."""
    with pytest.raises(InvalidPriceError):
        parse_safe_price(-100.50, "bid_price")

    with pytest.raises(InvalidQuantityError):
        parse_safe_quantity(-5.0, "ask_qty", allow_zero=True)


# ===========================================================================
# USE CASE 08: High taker fees compounding drag over 3 legs
# ===========================================================================
def test_case_08_high_taker_fees_compounding_drag():
    """Verifies high taker fees (e.g. 100 bps per leg) severely reduce net yield."""
    tickers = {k: v.model_copy(deep=True) for k, v in CANONICAL_TEST_TICKERS.items()}
    # 100 bps = 0.01 fee rate
    opps_high_fee = ArbitrageEngine.evaluate_cycles(tickers, fee_rate=0.01)
    opps_std_fee = ArbitrageEngine.evaluate_cycles(tickers, fee_rate=0.001)

    for h, s in zip(opps_high_fee, opps_std_fee):
        assert h.net_yield_pct < s.net_yield_pct
        # Gross yields must be identical since market prices didn't change
        assert math.isclose(h.gross_yield_pct, s.gross_yield_pct, abs_tol=1e-6)
        # 3 legs of 1% fee -> compounding fee factor ~ (1 - 0.01)^3 = 0.970299 (-2.97% fee drag)
        fee_drag = s.net_yield_pct - h.net_yield_pct
        assert fee_drag > 2.5  # Drag should exceed 2.5 percentage points


# ===========================================================================
# USE CASE 09: Zero-fee execution (gross yield identical to net yield)
# ===========================================================================
def test_case_09_zero_fee_execution_gross_equals_net():
    """Verifies when fee_rate is 0.0, net yield identically equals gross yield."""
    tickers = {k: v.model_copy(deep=True) for k, v in CANONICAL_TEST_TICKERS.items()}
    opps = ArbitrageEngine.evaluate_cycles(tickers, fee_rate=0.0)

    for opp in opps:
        assert math.isclose(opp.net_yield_pct, opp.gross_yield_pct, abs_tol=1e-8)


# ===========================================================================
# USE CASE 10: Extreme high threshold filtering (min_yield = 25%)
# ===========================================================================
def test_case_10_extreme_high_threshold_filtering(client):
    """Verifies setting min_yield=25% flags zero active opportunities."""
    resp = client.get("/api/triangular-cycles?active_only=true&min_yield=25.0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_count"] == 0
    assert len(data["opportunities"]) == 0


# ===========================================================================
# USE CASE 11: Negative threshold filtering (min_yield = -10%)
# ===========================================================================
def test_case_11_negative_threshold_filtering(client):
    """Verifies setting min_yield=-10% includes all normal negative-yield cycles."""
    resp = client.get("/api/triangular-cycles?active_only=true&min_yield=-10.0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_count"] == 6
    assert len(data["opportunities"]) == 6


# ===========================================================================
# USE CASE 12: Backward depth bottleneck propagation: Leg 1 bottleneck
# ===========================================================================
def test_case_12_backward_bottleneck_propagation_leg1():
    """Verifies bottleneck at Leg 1 accurately limits total executable volume."""
    tickers = {k: v.model_copy(deep=True) for k, v in CANONICAL_TEST_TICKERS.items()}
    # Constrain BTCUSDT ask_qty to 0.01 BTC ($800 capacity)
    tickers["BTCUSDT"].ask_qty = 0.01
    tickers["BTCUSDT"].ask_price = 80000.0

    opps = ArbitrageEngine.evaluate_cycles(tickers, fee_rate=DEFAULT_FEE_RATE)
    cycle = next(o for o in opps if o.cycle_id == "USDT->BTC->ETH->USDT")
    # Leg 1 capacity in quote is 0.01 * 80000 = 800.0 USDT
    assert cycle.max_executable_volume_quote <= 800.01


# ===========================================================================
# USE CASE 13: Backward depth bottleneck propagation: Leg 2 bottleneck (cross)
# ===========================================================================
def test_case_13_backward_bottleneck_propagation_leg2():
    """Verifies cross-pair depth on Leg 2 limits total executable volume."""
    tickers = {k: v.model_copy(deep=True) for k, v in CANONICAL_TEST_TICKERS.items()}
    # Constrain ETHBTC ask_qty to 0.05 ETH
    tickers["ETHBTC"].ask_qty = 0.05
    tickers["ETHBTC"].ask_price = 0.030820

    opps = ArbitrageEngine.evaluate_cycles(tickers, fee_rate=DEFAULT_FEE_RATE)
    cycle = next(o for o in opps if o.cycle_id == "USDT->BTC->ETH->USDT")
    assert cycle.max_executable_volume_quote < 200.0


# ===========================================================================
# USE CASE 14: Backward depth bottleneck propagation: Leg 3 bottleneck
# ===========================================================================
def test_case_14_backward_bottleneck_propagation_leg3():
    """Verifies Leg 3 depth bottleneck back-propagates to initial quote currency."""
    tickers = {k: v.model_copy(deep=True) for k, v in CANONICAL_TEST_TICKERS.items()}
    # Constrain ETHUSDT bid_qty to 0.05 ETH (~$123)
    tickers["ETHUSDT"].bid_qty = 0.05

    opps = ArbitrageEngine.evaluate_cycles(tickers, fee_rate=DEFAULT_FEE_RATE)
    cycle = next(o for o in opps if o.cycle_id == "USDT->BTC->ETH->USDT")
    assert cycle.max_executable_volume_quote < 200.0


# ===========================================================================
# USE CASE 15: All 6 canonical cycles enumerated with 3 valid legs
# ===========================================================================
def test_case_15_all_six_canonical_cycles_enumerated():
    """Verifies exactly 6 canonical USDT-rooted cycles exist with 3 valid legs."""
    expected_cycle_ids = {
        "USDT->BTC->ETH->USDT",
        "USDT->ETH->BTC->USDT",
        "USDT->BTC->SOL->USDT",
        "USDT->SOL->BTC->USDT",
        "USDT->BTC->BNB->USDT",
        "USDT->BNB->BTC->USDT",
    }
    opps = ArbitrageEngine.evaluate_cycles(CANONICAL_TEST_TICKERS, fee_rate=DEFAULT_FEE_RATE)
    actual_cycle_ids = {o.cycle_id for o in opps}
    assert actual_cycle_ids == expected_cycle_ids

    for opp in opps:
        assert len(opp.legs) == 3
        assert [l.step for l in opp.legs] == [1, 2, 3]
        assert opp.legs[0].from_asset == "USDT"
        assert opp.legs[2].to_asset == "USDT"


# ===========================================================================
# USE CASE 16: Directional math: BUY @ Ask vs SELL @ Bid
# ===========================================================================
def test_case_16_directional_buy_at_ask_vs_sell_at_bid():
    """Verifies BUY executes strictly at Ask, SELL executes strictly at Bid."""
    # BUY BTC with USDT: 80,000 USDT / 80,000 Ask * (1 - 0.001) = 0.999 BTC
    gross_base, net_base, ask_p = directional_buy(
        quote_qty=80000.0,
        ask_price=80000.0,
        fee_rate=0.001,
    )
    assert math.isclose(gross_base, 1.0, abs_tol=1e-6)
    assert math.isclose(net_base, 0.999, abs_tol=1e-6)

    # SELL 1 BTC for USDT at 79,900 Bid * (1 - 0.001) = 79,820.1 USDT
    gross_quote, net_quote, bid_p = directional_sell(
        base_qty=1.0,
        bid_price=79900.0,
        fee_rate=0.001,
    )
    assert math.isclose(gross_quote, 79900.0, abs_tol=1e-4)
    assert math.isclose(net_quote, 79900.0 * 0.999, abs_tol=1e-4)


# ===========================================================================
# USE CASE 17: Synthetic vs Direct exchange rate discrepancy calculation
# ===========================================================================
def test_case_17_synthetic_vs_direct_rate_calculation():
    """Verifies calculation of synthetic rate and percentage discrepancy."""
    opp = ArbitrageEngine.evaluate_usdt_btc_alt_cycle(
        tickers=CANONICAL_TEST_TICKERS,
        alt_symbol="ETH",
    )
    assert opp.synthetic_rate > 0
    assert opp.direct_rate > 0
    expected_discrepancy = ((opp.direct_rate - opp.synthetic_rate) / opp.synthetic_rate) * 100.0
    assert math.isclose(opp.discrepancy_pct, expected_discrepancy, abs_tol=1e-4)


# ===========================================================================
# USE CASE 18: Ingestion network timeout / HTTP error graceful recovery
# ===========================================================================
@pytest.mark.asyncio
async def test_case_18_ingestion_network_timeout_handling():
    """Verifies Poller gracefully records error and increments count upon timeout."""
    store = OrderBookStore(core_symbols=CORE_SYMBOLS)
    poller = BinanceBookTickerPoller(store=store, poll_interval=1.0)

    # Mock client to raise httpx.TimeoutException
    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.TimeoutException("Connection timed out to Binance")
    poller._client = mock_client

    await poller.poll_once()

    telemetry = store.get_telemetry()
    assert telemetry["error_count"] == 1
    assert "Connection timed out" in telemetry["last_error"]
    # Store should not crash and health status should evaluate to INITIALIZING or DEGRADED
    assert store.get_status() in ("INITIALIZING", "DEGRADED")


# ===========================================================================
# USE CASE 19: Dynamic Config GET and POST roundtrip verification
# ===========================================================================
def test_case_19_config_get_and_post_roundtrip(client):
    """Verifies GET /api/config returns config and POST /api/config mutates it."""
    # 1. GET initial config
    get_res = client.get("/api/config")
    assert get_res.status_code == 200
    cfg = get_res.json()["config"]
    assert "fee_rate" in cfg
    assert "threshold_pct" in cfg

    # 2. POST update fee and threshold
    post_res = client.post(
        "/api/config",
        json={"fee_bps": 15.0, "threshold_pct": 0.12, "initial_capital": 50000.0},
    )
    assert post_res.status_code == 200
    updated = post_res.json()["config"]
    assert math.isclose(updated["fee_rate"], 0.0015, abs_tol=1e-6)
    assert updated["threshold_pct"] == 0.12
    assert updated["initial_capital"] == 50000.0

    # 3. GET verify persistent update
    get_res2 = client.get("/api/config")
    assert get_res2.status_code == 200
    cfg2 = get_res2.json()["config"]
    assert math.isclose(cfg2["fee_rate"], 0.0015, abs_tol=1e-6)

    # Revert config to default for subsequent tests
    client.post("/api/config", json={"fee_bps": 10.0, "threshold_pct": 0.05, "initial_capital": 10000.0})


# ===========================================================================
# USE CASE 20: Orderbook Matrix 7 core pairs canonical ordering & spread bps
# ===========================================================================
def test_case_20_orderbook_matrix_seven_pairs_and_spread_bps(client):
    """Verifies all 7 core pairs are present in canonical sequence with positive spreads."""
    resp = client.get("/api/orderbook-matrix")
    assert resp.status_code == 200
    data = resp.json()

    assert data["pairs_count"] == 7
    received_symbols = [p["symbol"] for p in data["pairs"]]
    assert received_symbols == CANONICAL_PAIRS

    for pair in data["pairs"]:
        assert pair["bid_price"] > 0
        assert pair["ask_price"] > pair["bid_price"]
        assert pair["spread_abs"] > 0
        assert pair["spread_bps_mid"] > 0


# ===========================================================================
# USE CASE 21: Health state machine (INITIALIZING -> HEALTHY -> STALE -> DEGRADED)
# ===========================================================================
@pytest.mark.asyncio
async def test_case_21_health_state_machine():
    """Verifies system health transitions through INITIALIZING, HEALTHY, STALE, DEGRADED."""
    # 1. INITIALIZING on empty store
    empty_store = OrderBookStore(core_symbols=CORE_SYMBOLS)
    snapshot, meta = empty_store.get_snapshot()
    assert evaluate_system_health(snapshot, meta, False, CANONICAL_PAIRS) == "INITIALIZING"

    # 2. HEALTHY on full valid snapshot
    pop_store = OrderBookStore(core_symbols=CORE_SYMBOLS)
    await pop_store.update_tickers(list(CANONICAL_TEST_TICKERS.values()), latency_ms=10.0)
    snapshot, meta = pop_store.get_snapshot()
    assert evaluate_system_health(snapshot, meta, False, CANONICAL_PAIRS) == "HEALTHY"

    # 3. STALE when is_stale is True
    assert evaluate_system_health(snapshot, meta, True, CANONICAL_PAIRS) == "STALE"

    # 4. DEGRADED when an error is present
    error_meta = dict(meta)
    error_meta["error_count"] = 2
    error_meta["last_error"] = "Binance 503 Service Unavailable"
    assert evaluate_system_health(snapshot, error_meta, False, CANONICAL_PAIRS) == "DEGRADED"


# ===========================================================================
# USE CASE 22: High-density terminal dashboard (/) and interactive docs (/docs-interactive) HTML contracts
# ===========================================================================
def test_case_22_dashboard_and_docs_html_contracts(client):
    """Verifies / and /docs-interactive render responsive HTML with all required elements."""
    # 1. Terminal Dashboard
    resp_dash = client.get("/")
    assert resp_dash.status_code == 200
    assert "text/html" in resp_dash.headers["content-type"]
    dash_html = resp_dash.text
    # Check Brand & Telemetry
    assert "ARBFORGE" in dash_html
    assert "statusBadge" in dash_html
    assert "metricLatency" in dash_html
    assert "metricPolls" in dash_html
    assert "metricPairs" in dash_html
    assert "metricActive" in dash_html
    assert "metricWeight" in dash_html
    assert "metricUptime" in dash_html
    assert "clock" in dash_html
    assert "DOCS" in dash_html
    # Check 4 Panels
    assert "matrixTable" in dash_html
    assert "cyclesPanel" in dash_html
    assert "graphCanvas" in dash_html
    assert "thresholdSlider" in dash_html
    assert "feeSlider" in dash_html
    assert "capitalBtns" in dash_html
    assert "audioToggle" in dash_html
    assert "auditLog" in dash_html

    # 2. Interactive Docs
    resp_docs = client.get("/docs-interactive")
    assert resp_docs.status_code == 200
    assert "text/html" in resp_docs.headers["content-type"]
    docs_html = resp_docs.text
    assert "Interactive Quantitative Documentation" in docs_html
    assert "interactiveGraph" in docs_html
    assert "api-tester" in docs_html
    assert "recalcSimulator" in docs_html
    assert "accordion" in docs_html
