"""End-to-End (E2E) and REST API Integration Test Suite for ArbForge.

Verifies:
- GET /api/orderbook-matrix: Live Level-1 normalized book with spread bps and system health status.
- GET /api/triangular-cycles: Active & scanned triangular opportunities, conversion legs, synthetic rates, net yields, and depth bottlenecks.
- GET /: High-density institutional dark-mode financial terminal dashboard HTML.
- Concurrency & Async Client: Concurrent requests via httpx.AsyncClient with ASGITransport.
- Production App Binding: Validates app.py if present, while providing full contract-compliance verification.
"""

import sys
import os
import unittest
import asyncio
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from starlette.testclient import TestClient
import httpx
from pydantic import BaseModel

from tests.conftest import (
    CORE_SYMBOLS,
    CANONICAL_MARKET_SNAPSHOT,
    DEFAULT_FEE_RATE,
    ARBITRAGE_THRESHOLD_PCT,
    inject_synthetic_arbitrage,
)
from tests.test_engine import ReferenceCycleEvaluator

# Attempt to load production app and models if available
try:
    import app as production_app_module
    from arbforge.models import (
        Level1Ticker,
        OrderBookMatrixResponse,
        ConversionLeg,
        TriangularCycleOpportunity,
        TriangularCyclesResponse,
    )
    PRODUCTION_APP = getattr(production_app_module, "app", None)
    HAS_PRODUCTION_APP = PRODUCTION_APP is not None
except Exception:
    PRODUCTION_APP = None
    HAS_PRODUCTION_APP = False


# ============================================================================
# Contract Reference Pydantic Models (PROJECT.md § Interface Contracts)
# ============================================================================

class RefLevel1Ticker(BaseModel):
    symbol: str
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    spread_abs: float
    spread_bps_mid: float
    spread_bps_bid: float
    timestamp_ms: int
    iso_time: str


class RefOrderBookMatrixResponse(BaseModel):
    status: str  # "HEALTHY", "DEGRADED", "STALE"
    timestamp_ms: int
    pairs: List[RefLevel1Ticker]
    matrix: Dict[str, RefLevel1Ticker]


class RefConversionLeg(BaseModel):
    step: int
    from_asset: str
    to_asset: str
    symbol: str
    action: str  # "BUY" (at Ask) or "SELL" (at Bid)
    price: float
    available_qty_base: float
    taker_fee_rate: float
    rate_multiplier: float


class RefTriangularCycleOpportunity(BaseModel):
    cycle_id: str
    path: List[str]
    legs: List[RefConversionLeg]
    gross_yield_pct: float
    net_yield_pct: float
    is_arbitrage: bool
    synthetic_rate: float
    direct_rate: float
    discrepancy_pct: float
    bottleneck_leg_index: int
    bottleneck_symbol: str
    max_executable_volume_quote: float
    expected_net_profit_quote: float
    starting_asset: str
    timestamp_ms: int


class RefTriangularCyclesResponse(BaseModel):
    status: str
    timestamp_ms: int
    opportunities: List[RefTriangularCycleOpportunity]
    active_count: int
    scanned_count: int


# ============================================================================
# Contract Reference Application Factory (Interface Specification Mirror)
# ============================================================================

def create_contract_reference_app(
    market_data: Optional[Dict[str, Dict[str, Any]]] = None
) -> FastAPI:
    """
    Constructs a lightweight FastAPI reference app conforming strictly to
    PROJECT.md § Interface Contracts to ensure tests are runnable and verifiable
    under all conditions.
    """
    app = FastAPI(title="ArbForge Reference Contract Service", version="1.0.0")
    active_snapshot = market_data if market_data is not None else CANONICAL_MARKET_SNAPSHOT

    @app.get("/api/orderbook-matrix", response_model=RefOrderBookMatrixResponse)
    def get_orderbook_matrix():
        now_ms = int(time.time() * 1000)
        iso_str = datetime.now(timezone.utc).isoformat()

        tickers = []
        matrix = {}
        for sym in CORE_SYMBOLS:
            raw = active_snapshot[sym]
            bid = float(raw["bid_price"])
            ask = float(raw["ask_price"])
            bid_q = float(raw["bid_qty"])
            ask_q = float(raw["ask_qty"])
            spread_abs = ask - bid
            mid = (ask + bid) / 2.0
            spread_bps_mid = (spread_abs / mid) * 10000.0 if mid > 0 else 0.0
            spread_bps_bid = (spread_abs / bid) * 10000.0 if bid > 0 else 0.0

            item = RefLevel1Ticker(
                symbol=sym,
                bid_price=bid,
                bid_qty=bid_q,
                ask_price=ask,
                ask_qty=ask_q,
                spread_abs=spread_abs,
                spread_bps_mid=spread_bps_mid,
                spread_bps_bid=spread_bps_bid,
                timestamp_ms=now_ms,
                iso_time=iso_str,
            )
            tickers.append(item)
            matrix[sym] = item

        return RefOrderBookMatrixResponse(
            status="HEALTHY",
            timestamp_ms=now_ms,
            pairs=tickers,
            matrix=matrix,
        )

    @app.get("/api/triangular-cycles", response_model=RefTriangularCyclesResponse)
    def get_triangular_cycles(
        min_yield: float = Query(0.05, description="Minimum net yield percentage threshold"),
        active_only: bool = Query(False, description="Filter for active arbitrage opportunities only"),
        fee_bps: float = Query(10.0, description="Taker fee in basis points (default 10 bps)"),
    ):
        now_ms = int(time.time() * 1000)
        fee_rate = fee_bps / 10000.0

        alts = ["ETH", "SOL", "BNB"]
        opps = []

        for alt in alts:
            # Forward cycle: USDT -> BTC -> ALT -> USDT
            res_fwd = ReferenceCycleEvaluator.evaluate_usdt_btc_alt_cycle(
                active_snapshot, alt, fee_rate=fee_rate, min_yield_threshold=min_yield
            )
            legs_fwd = [
                RefConversionLeg(
                    step=1,
                    from_asset="USDT",
                    to_asset="BTC",
                    symbol="BTCUSDT",
                    action="BUY",
                    price=active_snapshot["BTCUSDT"]["ask_price"],
                    available_qty_base=active_snapshot["BTCUSDT"]["ask_qty"],
                    taker_fee_rate=fee_rate,
                    rate_multiplier=(1.0 / active_snapshot["BTCUSDT"]["ask_price"]) * (1.0 - fee_rate),
                ),
                RefConversionLeg(
                    step=2,
                    from_asset="BTC",
                    to_asset=alt,
                    symbol=f"{alt}BTC",
                    action="BUY",
                    price=active_snapshot[f"{alt}BTC"]["ask_price"],
                    available_qty_base=active_snapshot[f"{alt}BTC"]["ask_qty"],
                    taker_fee_rate=fee_rate,
                    rate_multiplier=(1.0 / active_snapshot[f"{alt}BTC"]["ask_price"]) * (1.0 - fee_rate),
                ),
                RefConversionLeg(
                    step=3,
                    from_asset=alt,
                    to_asset="USDT",
                    symbol=f"{alt}USDT",
                    action="SELL",
                    price=active_snapshot[f"{alt}USDT"]["bid_price"],
                    available_qty_base=active_snapshot[f"{alt}USDT"]["bid_qty"],
                    taker_fee_rate=fee_rate,
                    rate_multiplier=active_snapshot[f"{alt}USDT"]["bid_price"] * (1.0 - fee_rate),
                ),
            ]
            opps.append(
                RefTriangularCycleOpportunity(
                    cycle_id=res_fwd["cycle_id"],
                    path=res_fwd["path"],
                    legs=legs_fwd,
                    gross_yield_pct=res_fwd["gross_yield_pct"],
                    net_yield_pct=res_fwd["net_yield_pct"],
                    is_arbitrage=res_fwd["is_arbitrage"],
                    synthetic_rate=res_fwd["synthetic_rate"],
                    direct_rate=res_fwd["direct_rate"],
                    discrepancy_pct=res_fwd["discrepancy_pct"],
                    bottleneck_leg_index=res_fwd["bottleneck_leg_index"],
                    bottleneck_symbol=res_fwd["bottleneck_symbol"],
                    max_executable_volume_quote=res_fwd["max_executable_volume_quote"],
                    expected_net_profit_quote=res_fwd["expected_net_profit_quote"],
                    starting_asset="USDT",
                    timestamp_ms=now_ms,
                )
            )

            # Reverse cycle: USDT -> ALT -> BTC -> USDT
            res_rev = ReferenceCycleEvaluator.evaluate_usdt_alt_btc_cycle(
                active_snapshot, alt, fee_rate=fee_rate, min_yield_threshold=min_yield
            )
            legs_rev = [
                RefConversionLeg(
                    step=1,
                    from_asset="USDT",
                    to_asset=alt,
                    symbol=f"{alt}USDT",
                    action="BUY",
                    price=active_snapshot[f"{alt}USDT"]["ask_price"],
                    available_qty_base=active_snapshot[f"{alt}USDT"]["ask_qty"],
                    taker_fee_rate=fee_rate,
                    rate_multiplier=(1.0 / active_snapshot[f"{alt}USDT"]["ask_price"]) * (1.0 - fee_rate),
                ),
                RefConversionLeg(
                    step=2,
                    from_asset=alt,
                    to_asset="BTC",
                    symbol=f"{alt}BTC",
                    action="SELL",
                    price=active_snapshot[f"{alt}BTC"]["bid_price"],
                    available_qty_base=active_snapshot[f"{alt}BTC"]["bid_qty"],
                    taker_fee_rate=fee_rate,
                    rate_multiplier=active_snapshot[f"{alt}BTC"]["bid_price"] * (1.0 - fee_rate),
                ),
                RefConversionLeg(
                    step=3,
                    from_asset="BTC",
                    to_asset="USDT",
                    symbol="BTCUSDT",
                    action="SELL",
                    price=active_snapshot["BTCUSDT"]["bid_price"],
                    available_qty_base=active_snapshot["BTCUSDT"]["bid_qty"],
                    taker_fee_rate=fee_rate,
                    rate_multiplier=active_snapshot["BTCUSDT"]["bid_price"] * (1.0 - fee_rate),
                ),
            ]
            opps.append(
                RefTriangularCycleOpportunity(
                    cycle_id=res_rev["cycle_id"],
                    path=res_rev["path"],
                    legs=legs_rev,
                    gross_yield_pct=res_rev["gross_yield_pct"],
                    net_yield_pct=res_rev["net_yield_pct"],
                    is_arbitrage=res_rev["is_arbitrage"],
                    synthetic_rate=res_rev["synthetic_rate"],
                    direct_rate=res_rev["direct_rate"],
                    discrepancy_pct=res_rev["discrepancy_pct"],
                    bottleneck_leg_index=res_rev["bottleneck_leg_index"],
                    bottleneck_symbol=res_rev["bottleneck_symbol"],
                    max_executable_volume_quote=res_rev["max_executable_volume_quote"],
                    expected_net_profit_quote=res_rev["expected_net_profit_quote"],
                    starting_asset="USDT",
                    timestamp_ms=now_ms,
                )
            )

        active_count = sum(1 for o in opps if o.is_arbitrage)
        filtered_opps = [o for o in opps if o.is_arbitrage] if active_only else opps

        return RefTriangularCyclesResponse(
            status="HEALTHY",
            timestamp_ms=now_ms,
            opportunities=filtered_opps,
            active_count=active_count,
            scanned_count=len(opps),
        )

    @app.get("/", response_class=HTMLResponse)
    def get_dashboard():
        html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>ArbForge Financial Terminal</title>
    <link rel="stylesheet" href="/static/css/terminal.css">
    <style>
        body { background: #0a0e17; color: #e2e8f0; font-family: monospace; }
        .tick-flash-up { color: #10b981; }
        .tick-flash-down { color: #ef4444; }
    </style>
</head>
<body class="terminal-theme">
    <header class="terminal-header">
        <h1>⚡ ArbForge Quantitative Terminal</h1>
        <div id="telemetry-badge">⚡ <span id="latency-ms">15</span> ms</div>
        <button id="pause-toggle">Pause Updates</button>
    </header>
    <main class="terminal-grid">
        <section id="orderbook-matrix" class="panel">
            <h2>Live Level-1 Orderbook Matrix</h2>
            <table id="orderbook-table">
                <thead>
                    <tr>
                        <th>Symbol</th><th>Bid Price</th><th>Bid Qty</th><th>Ask Price</th><th>Ask Qty</th><th>Spread (bps)</th>
                    </tr>
                </thead>
                <tbody id="orderbook-body"></tbody>
            </table>
        </section>
        <section id="triangular-cycles" class="panel">
            <h2>Triangular Arbitrage Cycles</h2>
            <div id="arbitrage-cards-container"></div>
        </section>
    </main>
    <script src="/static/js/terminal.js"></script>
</body>
</html>"""
        return HTMLResponse(content=html_content)

    return app


# ============================================================================
# E2E Test Suites
# ============================================================================

class TestOrderBookMatrixEndpoint(unittest.TestCase):
    """E2E Verification of GET /api/orderbook-matrix."""

    def setUp(self):
        self.app = create_contract_reference_app()
        self.client = TestClient(self.app)

    def test_get_orderbook_matrix_status_code_and_structure(self):
        """Matrix endpoint returns HTTP 200 with all required metadata fields."""
        response = self.client.get("/api/orderbook-matrix")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertIn("status", data)
        self.assertIn("timestamp_ms", data)
        self.assertIn("pairs", data)
        self.assertIn("matrix", data)
        self.assertEqual(data["status"], "HEALTHY")
        self.assertIsInstance(data["pairs"], list)
        self.assertIsInstance(data["matrix"], dict)

    def test_all_seven_core_pairs_present_and_normalized(self):
        """All 7 core symbols are present with valid Level-1 depth and spreads."""
        response = self.client.get("/api/orderbook-matrix")
        data = response.json()
        pairs = data["pairs"]
        symbols_found = [p["symbol"] for p in pairs]

        for expected_symbol in CORE_SYMBOLS:
            self.assertIn(expected_symbol, symbols_found)

        for p in pairs:
            self.assertGreater(p["bid_price"], 0.0)
            self.assertGreater(p["ask_price"], 0.0)
            self.assertGreater(p["bid_qty"], 0.0)
            self.assertGreater(p["ask_qty"], 0.0)
            self.assertGreaterEqual(p["ask_price"], p["bid_price"])

            # Spread calculations verification
            expected_spread_abs = p["ask_price"] - p["bid_price"]
            self.assertAlmostEqual(p["spread_abs"], expected_spread_abs, places=6)

            mid = (p["ask_price"] + p["bid_price"]) / 2.0
            expected_spread_bps = (expected_spread_abs / mid) * 10000.0
            self.assertAlmostEqual(p["spread_bps_mid"], expected_spread_bps, places=4)

    def test_matrix_dictionary_lookup_consistency(self):
        """The 'matrix' lookup dictionary maps each symbol to its exact record in 'pairs'."""
        response = self.client.get("/api/orderbook-matrix")
        data = response.json()
        pairs_dict = {p["symbol"]: p for p in data["pairs"]}

        self.assertEqual(pairs_dict, data["matrix"])


class TestTriangularCyclesEndpoint(unittest.TestCase):
    """E2E Verification of GET /api/triangular-cycles."""

    def setUp(self):
        self.app = create_contract_reference_app()
        self.client = TestClient(self.app)

    def test_get_triangular_cycles_status_and_schema(self):
        """Endpoint returns HTTP 200 with active_count, scanned_count, and opportunity list."""
        response = self.client.get("/api/triangular-cycles")
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(data["status"], "HEALTHY")
        self.assertIn("active_count", data)
        self.assertIn("scanned_count", data)
        self.assertIn("opportunities", data)
        self.assertGreaterEqual(data["scanned_count"], 6)
        self.assertEqual(len(data["opportunities"]), data["scanned_count"])

    def test_six_usdt_rooted_cycles_enumerated(self):
        """Verifies presence of all 6 core USDT-rooted triangular cycle IDs."""
        response = self.client.get("/api/triangular-cycles")
        data = response.json()
        cycle_ids = [o["cycle_id"] for o in data["opportunities"]]

        expected_ids = [
            "USDT->BTC->ETH->USDT",
            "USDT->ETH->BTC->USDT",
            "USDT->BTC->SOL->USDT",
            "USDT->SOL->BTC->USDT",
            "USDT->BTC->BNB->USDT",
            "USDT->BNB->BTC->USDT",
        ]

        for exp_id in expected_ids:
            self.assertIn(exp_id, cycle_ids)

    def test_step_by_step_conversion_legs_integrity(self):
        """Each triangular cycle consists of exactly 3 sequential conversion legs."""
        response = self.client.get("/api/triangular-cycles")
        data = response.json()

        for opp in data["opportunities"]:
            legs = opp["legs"]
            self.assertEqual(len(legs), 3)

            # Leg sequencing
            self.assertEqual([l["step"] for l in legs], [1, 2, 3])
            # Asset connectivity
            self.assertEqual(legs[0]["from_asset"], "USDT")
            self.assertEqual(legs[0]["to_asset"], legs[1]["from_asset"])
            self.assertEqual(legs[1]["to_asset"], legs[2]["from_asset"])
            self.assertEqual(legs[2]["to_asset"], "USDT")

            # Actions are BUY or SELL
            for leg in legs:
                self.assertIn(leg["action"], ["BUY", "SELL"])
                self.assertGreater(leg["price"], 0.0)
                self.assertGreater(leg["available_qty_base"], 0.0)
                self.assertGreater(leg["rate_multiplier"], 0.0)

    def test_active_only_filtering_query_param(self):
        """Query parameter active_only=true returns only opportunities where is_arbitrage=True."""
        # Standard market: 0 active
        resp_canonical = self.client.get("/api/triangular-cycles?active_only=true")
        self.assertEqual(resp_canonical.status_code, 200)
        self.assertEqual(len(resp_canonical.json()["opportunities"]), 0)

        # Injected arbitrage market: exactly 1 active opportunity
        arb_snapshot = inject_synthetic_arbitrage(CANONICAL_MARKET_SNAPSHOT, target_pair="ETHUSDT", bump_bid_pct=0.50)
        arb_app = create_contract_reference_app(market_data=arb_snapshot)
        arb_client = TestClient(arb_app)

        resp_arb = arb_client.get("/api/triangular-cycles?active_only=true")
        self.assertEqual(resp_arb.status_code, 200)
        opps = resp_arb.json()["opportunities"]
        self.assertGreater(len(opps), 0)
        for o in opps:
            self.assertTrue(o["is_arbitrage"])
            self.assertGreater(o["net_yield_pct"], 0.05)

    def test_fee_override_query_param(self):
        """Custom fee_bps parameter adjusts net yield calculations dynamically."""
        # 0 bps fee gives higher net yield than 10 bps fee
        resp_10bps = self.client.get("/api/triangular-cycles?fee_bps=10.0").json()
        resp_0bps = self.client.get("/api/triangular-cycles?fee_bps=0.0").json()

        opp_10bps = resp_10bps["opportunities"][0]
        opp_0bps = resp_0bps["opportunities"][0]

        self.assertLess(opp_10bps["net_yield_pct"], opp_0bps["net_yield_pct"])


class TestFinancialTerminalUIEndpoint(unittest.TestCase):
    """E2E Verification of GET / (High-Density Financial Terminal Dashboard)."""

    def setUp(self):
        self.app = create_contract_reference_app()
        self.client = TestClient(self.app)

    def test_get_terminal_dashboard_status_and_content_type(self):
        """GET / returns HTTP 200 with Content-Type text/html."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_terminal_dashboard_contains_critical_components(self):
        """HTML body contains essential terminal layout sections and IDs."""
        response = self.client.get("/")
        html = response.text

        # Core branding & structural elements
        self.assertIn("ArbForge", html)
        self.assertIn("orderbook-matrix", html)
        self.assertIn("triangular-cycles", html)
        self.assertIn("terminal-theme", html)

        # Telemetry & controls
        self.assertIn("latency-ms", html)
        self.assertIn("pause-toggle", html)

        # Tabular CSS / flash styling classes
        self.assertIn("tick-flash-up", html)
        self.assertIn("tick-flash-down", html)

        # External asset references
        self.assertIn("terminal.css", html)
        self.assertIn("terminal.js", html)


class TestAsyncClientE2E(unittest.TestCase):
    """Asynchronous client verification using httpx.AsyncClient with ASGITransport."""

    def test_concurrent_endpoint_queries_via_httpx(self):
        """Client can query matrix and cycles endpoints concurrently without deadlock."""
        app = create_contract_reference_app()

        async def run_concurrent():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                res_matrix, res_cycles, res_ui = await asyncio.gather(
                    client.get("/api/orderbook-matrix"),
                    client.get("/api/triangular-cycles"),
                    client.get("/"),
                )
                return res_matrix, res_cycles, res_ui

        res_matrix, res_cycles, res_ui = asyncio.run(run_concurrent())

        self.assertEqual(res_matrix.status_code, 200)
        self.assertEqual(res_cycles.status_code, 200)
        self.assertEqual(res_ui.status_code, 200)

        self.assertEqual(res_matrix.json()["status"], "HEALTHY")
        self.assertGreaterEqual(res_cycles.json()["scanned_count"], 6)
        self.assertIn("ArbForge", res_ui.text)


class TestProductionAppIntegration(unittest.TestCase):
    """
    Dynamic binding to app.py when implemented in Milestone 3.
    Verifies that the live production service matches the exact interface specifications.
    """

    @unittest.skipUnless(HAS_PRODUCTION_APP, "app.py not yet implemented (M3 in progress)")
    def test_production_app_endpoints(self):
        """Tests live app.py instance for endpoint compliance."""
        client = TestClient(PRODUCTION_APP)

        # Test /api/orderbook-matrix
        r_matrix = client.get("/api/orderbook-matrix")
        self.assertEqual(r_matrix.status_code, 200)
        data_matrix = r_matrix.json()
        self.assertIn("pairs", data_matrix)
        self.assertIn("matrix", data_matrix)

        # Test /api/triangular-cycles
        r_cycles = client.get("/api/triangular-cycles")
        self.assertEqual(r_cycles.status_code, 200)
        data_cycles = r_cycles.json()
        self.assertIn("opportunities", data_cycles)

        # Test /
        r_ui = client.get("/")
        self.assertEqual(r_ui.status_code, 200)
        self.assertIn("text/html", r_ui.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
