"""Unit tests for ArbForge Directed Asset Conversion Graph (arbforge/graph.py).

Verifies:
1. Asset graph initialization, nodes (USDT, BTC, ETH, SOL, BNB), pairs, and dual directed edges (14 edges).
2. Directional edge execution semantics (BUY Quote->Base at Ask, SELL Base->Quote at Bid).
3. Leg input capacity, rate multipliers, fee deductions, and ConversionLeg model generation.
4. Guaranteed 6 canonical USDT-rooted triangular loops.
5. Guaranteed 18 total directional paths across 7 core trading pairs.
6. Dynamic cycle enumeration and bounded DFS traversal.
7. Synthetic vs. direct rate calculations on graph cycles.
"""

import math
import unittest
from typing import Dict, Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from arbforge.api import router as api_router
from arbforge.graph import (
    CORE_ASSETS,
    CORE_PAIRS,
    PAIR_DEFINITIONS,
    AssetConversionGraph,
    AssetGraph,
    DirectedEdge,
    TriangularCyclePath,
)
from arbforge.mock import SyntheticMarketGenerator
from arbforge.models import ConversionLeg, Level1Ticker
from arbforge.store import OrderBookStore
from tests.conftest import CANONICAL_MARKET_SNAPSHOT, DEFAULT_FEE_RATE


class TestAssetGraphInitialization(unittest.TestCase):
    """Verifies graph topology, node initialization, and edge construction."""

    def setUp(self):
        self.graph = AssetGraph()

    def test_core_assets_present_as_nodes(self):
        """All 5 core assets are registered as graph nodes."""
        nodes = self.graph.nodes
        self.assertEqual(len(nodes), 5)
        for asset in ("USDT", "BTC", "ETH", "SOL", "BNB"):
            self.assertIn(asset, nodes)

    def test_core_trading_pairs_registered(self):
        """All 7 core pairs are registered in graph pair definitions."""
        pairs = self.graph.pairs
        self.assertEqual(len(pairs), 7)
        for symbol in CORE_PAIRS:
            self.assertIn(symbol, pairs)
            base, quote = PAIR_DEFINITIONS[symbol]
            self.assertEqual(pairs[symbol], (base, quote))

    def test_fourteen_dual_directed_edges_created(self):
        """7 trading pairs yield exactly 14 directed edges."""
        edge_count = 0
        for from_asset in self.graph.nodes:
            for to_asset in self.graph.get_neighbors(from_asset):
                edge = self.graph.get_edge(from_asset, to_asset)
                self.assertIsNotNone(edge)
                edge_count += 1
        self.assertEqual(edge_count, 14)

    def test_backward_compatibility_alias(self):
        """AssetConversionGraph is a functional alias for AssetGraph."""
        self.assertIs(AssetConversionGraph, AssetGraph)
        g = AssetConversionGraph()
        self.assertEqual(len(g.nodes), 5)

    def test_dynamic_add_pair_creates_bidirectional_edges(self):
        """Adding a new pair dynamically creates both BUY and SELL directed edges."""
        custom_graph = AssetGraph(pairs={})
        self.assertEqual(len(custom_graph.nodes), 0)

        custom_graph.add_pair("XRPUSDT", "XRP", "USDT")
        self.assertIn("XRP", custom_graph.nodes)
        self.assertIn("USDT", custom_graph.nodes)

        # Quote -> Base (BUY at Ask)
        edge_buy = custom_graph.get_edge("USDT", "XRP")
        self.assertIsNotNone(edge_buy)
        self.assertEqual(edge_buy.action, "BUY")
        self.assertEqual(edge_buy.symbol, "XRPUSDT")

        # Base -> Quote (SELL at Bid)
        edge_sell = custom_graph.get_edge("XRP", "USDT")
        self.assertIsNotNone(edge_sell)
        self.assertEqual(edge_sell.action, "SELL")
        self.assertEqual(edge_sell.symbol, "XRPUSDT")


class TestDirectedEdgeMechanics(unittest.TestCase):
    """Verifies edge execution prices, capacities, and leg construction."""

    def setUp(self):
        self.graph = AssetGraph()
        self.snapshot = CANONICAL_MARKET_SNAPSHOT

    def test_quote_to_base_buy_at_ask_mechanics(self):
        """Quote -> Base is a BUY order at Ask price."""
        edge = self.graph.get_edge("USDT", "BTC")
        self.assertIsNotNone(edge)
        self.assertEqual(edge.action, "BUY")
        self.assertEqual(edge.symbol, "BTCUSDT")

        btc_ticker = self.snapshot["BTCUSDT"]
        price = edge.get_price(btc_ticker)
        avail_qty = edge.get_available_qty_base(btc_ticker)

        self.assertEqual(price, btc_ticker["ask_price"])
        self.assertEqual(avail_qty, btc_ticker["ask_qty"])

        # Input capacity for BUY: ask_qty * ask_price (in USDT)
        expected_cap = btc_ticker["ask_qty"] * btc_ticker["ask_price"]
        self.assertAlmostEqual(edge.get_input_capacity(price, avail_qty), expected_cap, places=6)

        # Rate multiplier: (1.0 / price) * (1 - fee)
        fee = 0.0010
        multiplier = edge.get_rate_multiplier(price, fee)
        expected_mult = (1.0 / price) * (1.0 - fee)
        self.assertAlmostEqual(multiplier, expected_mult, places=10)

    def test_base_to_quote_sell_at_bid_mechanics(self):
        """Base -> Quote is a SELL order at Bid price."""
        edge = self.graph.get_edge("ETH", "USDT")
        self.assertIsNotNone(edge)
        self.assertEqual(edge.action, "SELL")
        self.assertEqual(edge.symbol, "ETHUSDT")

        eth_ticker = self.snapshot["ETHUSDT"]
        price = edge.get_price(eth_ticker)
        avail_qty = edge.get_available_qty_base(eth_ticker)

        self.assertEqual(price, eth_ticker["bid_price"])
        self.assertEqual(avail_qty, eth_ticker["bid_qty"])

        # Input capacity for SELL: bid_qty (in ETH)
        self.assertEqual(edge.get_input_capacity(price, avail_qty), eth_ticker["bid_qty"])

        # Rate multiplier: price * (1 - fee)
        fee = 0.0010
        multiplier = edge.get_rate_multiplier(price, fee)
        expected_mult = price * (1.0 - fee)
        self.assertAlmostEqual(multiplier, expected_mult, places=6)

    def test_invalid_price_raises_error(self):
        """Non-positive or non-finite price raises ValueError."""
        edge_buy = self.graph.get_edge("USDT", "BTC")
        with self.assertRaises(ValueError):
            edge_buy.get_price({"ask_price": 0.0, "bid_price": 79000.0})
        with self.assertRaises(ValueError):
            edge_buy.get_price({"ask_price": -100.0, "bid_price": 79000.0})
        with self.assertRaises(ValueError):
            edge_buy.get_price({"ask_price": float("nan"), "bid_price": 79000.0})

        edge_sell = self.graph.get_edge("BTC", "USDT")
        with self.assertRaises(ValueError):
            edge_sell.get_price({"ask_price": 80000.0, "bid_price": 0.0})
        with self.assertRaises(ValueError):
            edge_sell.get_price({"ask_price": 80000.0, "bid_price": -50.0})

    def test_build_conversion_leg_pydantic_model(self):
        """build_conversion_leg outputs a valid ConversionLeg instance."""
        edge = self.graph.get_edge("USDT", "BTC")
        ticker = self.snapshot["BTCUSDT"]
        leg = edge.build_conversion_leg(ticker, step=1, taker_fee_rate=0.0010)

        self.assertIsInstance(leg, ConversionLeg)
        self.assertEqual(leg.step, 1)
        self.assertEqual(leg.from_asset, "USDT")
        self.assertEqual(leg.to_asset, "BTC")
        self.assertEqual(leg.symbol, "BTCUSDT")
        self.assertEqual(leg.action, "BUY")
        self.assertEqual(leg.price, ticker["ask_price"])
        self.assertEqual(leg.available_qty_base, ticker["ask_qty"])
        self.assertEqual(leg.taker_fee_rate, 0.0010)
        self.assertAlmostEqual(leg.rate_multiplier, (1.0 / ticker["ask_price"]) * 0.999, places=10)


class TestTriangularCycleEnumeration(unittest.TestCase):
    """Verifies deterministic 6 USDT-rooted cycles and 18 total core paths."""

    def setUp(self):
        self.graph = AssetGraph()

    def test_six_canonical_usdt_rooted_triangular_cycles(self):
        """get_usdt_triangular_cycles returns exactly 6 closed USDT-rooted loops in canonical order."""
        cycles = self.graph.get_usdt_triangular_cycles()
        self.assertEqual(len(cycles), 6)

        expected_cycle_ids = [
            "USDT->BTC->ETH->USDT",
            "USDT->ETH->BTC->USDT",
            "USDT->BTC->SOL->USDT",
            "USDT->SOL->BTC->USDT",
            "USDT->BTC->BNB->USDT",
            "USDT->BNB->BTC->USDT",
        ]

        for i, expected_id in enumerate(expected_cycle_ids):
            c = cycles[i]
            self.assertEqual(c.cycle_id, expected_id)
            self.assertEqual(c.starting_asset, "USDT")
            self.assertEqual(c.path[0], "USDT")
            self.assertEqual(c.path[-1], "USDT")
            self.assertEqual(len(c.edges), 3)

            # Check continuous asset path connectivity
            e1, e2, e3 = c.edges
            self.assertEqual(e1.from_asset, "USDT")
            self.assertEqual(e1.to_asset, e2.from_asset)
            self.assertEqual(e2.to_asset, e3.from_asset)
            self.assertEqual(e3.to_asset, "USDT")

    def test_eighteen_total_directional_paths_across_core_pairs(self):
        """
        get_all_core_cycles returns exactly 18 directional paths:
        3 triangles (ETH, SOL, BNB) x 2 orientations (Forward, Reverse) x 3 starting roots = 18.
        """
        all_cycles = self.graph.get_all_core_cycles()
        self.assertEqual(len(all_cycles), 18)

        # Verify all cycles have exactly 3 legs and valid connectivity
        roots_count: Dict[str, int] = {}
        for c in all_cycles:
            self.assertEqual(len(c.path), 4)
            self.assertEqual(c.path[0], c.path[3])
            root = c.starting_asset
            roots_count[root] = roots_count.get(root, 0) + 1

            e1, e2, e3 = c.edges
            self.assertEqual(e1.from_asset, root)
            self.assertEqual(e1.to_asset, e2.from_asset)
            self.assertEqual(e2.to_asset, e3.from_asset)
            self.assertEqual(e3.to_asset, root)

        # 3 triangles x 2 orientations = 6 USDT roots, 6 BTC roots, 2 ETH roots, 2 SOL roots, 2 BNB roots
        self.assertEqual(roots_count["USDT"], 6)
        self.assertEqual(roots_count["BTC"], 6)
        self.assertEqual(roots_count["ETH"], 2)
        self.assertEqual(roots_count["SOL"], 2)
        self.assertEqual(roots_count["BNB"], 2)

    def test_dynamic_cycle_enumeration_with_usdt_root(self):
        """enumerate_triangular_cycles(root_asset='USDT') returns the 6 USDT-rooted loops."""
        cycles = self.graph.enumerate_triangular_cycles(root_asset="USDT")
        self.assertEqual(len(cycles), 6)
        for c in cycles:
            self.assertEqual(c.starting_asset, "USDT")
            self.assertEqual(c.path[0], "USDT")
            self.assertEqual(c.path[3], "USDT")

    def test_dynamic_cycle_enumeration_all(self):
        """enumerate_triangular_cycles() without root discovers all closed 3-asset loops."""
        cycles = self.graph.enumerate_triangular_cycles()
        # Each undirected triangle has 2 directed loops. With 3 triangles, there are 6 directed loops (independent of shift)
        # or 18 cyclic permutations. Our DFS avoids duplicates with visited set.
        self.assertGreaterEqual(len(cycles), 6)


class TestCyclePathExecutionAndRates(unittest.TestCase):
    """Verifies leg building and rate calculations from TriangularCyclePath."""

    def setUp(self):
        self.graph = AssetGraph()
        self.snapshot = CANONICAL_MARKET_SNAPSHOT

    def test_build_legs_for_canonical_forward_cycle(self):
        """build_legs correctly resolves all 3 legs from snapshot."""
        cycles = self.graph.get_usdt_triangular_cycles()
        fwd_eth = cycles[0]  # USDT -> BTC -> ETH -> USDT

        legs = fwd_eth.build_legs(self.snapshot, taker_fee_rate=0.0010)
        self.assertEqual(len(legs), 3)

        self.assertEqual(legs[0].symbol, "BTCUSDT")
        self.assertEqual(legs[0].action, "BUY")
        self.assertEqual(legs[1].symbol, "ETHBTC")
        self.assertEqual(legs[1].action, "BUY")
        self.assertEqual(legs[2].symbol, "ETHUSDT")
        self.assertEqual(legs[2].action, "SELL")

    def test_build_legs_missing_ticker_raises_key_error(self):
        """build_legs raises KeyError when a required ticker is missing."""
        incomplete_snapshot = dict(self.snapshot)
        del incomplete_snapshot["ETHBTC"]

        fwd_eth = self.graph.get_usdt_triangular_cycles()[0]
        with self.assertRaises(KeyError):
            fwd_eth.build_legs(incomplete_snapshot)

    def test_calculate_synthetic_vs_direct_rates_forward(self):
        """Forward cycle synthetic rate is p_ask_altbtc * p_ask_btcusdt."""
        fwd_eth = self.graph.get_usdt_triangular_cycles()[0]
        syn, direct, disc = fwd_eth.calculate_synthetic_vs_direct_rates(self.snapshot)

        p_btc_ask = self.snapshot["BTCUSDT"]["ask_price"]
        p_ethbtc_ask = self.snapshot["ETHBTC"]["ask_price"]
        expected_syn = p_ethbtc_ask * p_btc_ask
        expected_dir = self.snapshot["ETHUSDT"]["bid_price"]
        expected_disc = ((expected_dir - expected_syn) / expected_syn) * 100.0

        self.assertAlmostEqual(syn, expected_syn, places=4)
        self.assertAlmostEqual(direct, expected_dir, places=4)
        self.assertAlmostEqual(disc, expected_disc, places=4)

    def test_calculate_synthetic_vs_direct_rates_reverse(self):
        """Reverse cycle synthetic rate is p_bid_altbtc * p_bid_btcusdt."""
        rev_eth = self.graph.get_usdt_triangular_cycles()[1]  # USDT -> ETH -> BTC -> USDT
        syn, direct, disc = rev_eth.calculate_synthetic_vs_direct_rates(self.snapshot)

        p_eth_ask = self.snapshot["ETHUSDT"]["ask_price"]
        p_ethbtc_bid = self.snapshot["ETHBTC"]["bid_price"]
        p_btc_bid = self.snapshot["BTCUSDT"]["bid_price"]

        expected_dir = p_eth_ask
        expected_syn = p_ethbtc_bid * p_btc_bid
        expected_disc = ((expected_syn - expected_dir) / expected_dir) * 100.0

        self.assertAlmostEqual(syn, expected_syn, places=4)
        self.assertAlmostEqual(direct, expected_dir, places=4)
        self.assertAlmostEqual(disc, expected_disc, places=4)

    def test_validate_tickers_available(self):
        """validate_tickers_available correctly tests for required pair presence."""
        fwd_eth = self.graph.get_usdt_triangular_cycles()[0]
        self.assertTrue(self.graph.validate_tickers_available(fwd_eth, self.snapshot.keys()))
        self.assertFalse(self.graph.validate_tickers_available(fwd_eth, ["BTCUSDT", "ETHUSDT"]))


class TestTriangularCyclesAPIEndpoint(unittest.TestCase):
    """Hermetic unit tests for GET /api/triangular-cycles route in arbforge/api.py."""

    def setUp(self):
        self.store = OrderBookStore()
        self.app = FastAPI()
        self.app.state.store = self.store
        self.app.include_router(api_router, prefix="/api")
        self.client = TestClient(self.app)

    def test_endpoint_empty_store_returns_initializing(self):
        """Unpopulated store returns HTTP 200 with INITIALIZING status."""
        response = self.client.get("/api/triangular-cycles")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "INITIALIZING")
        self.assertEqual(data["opportunities"], [])
        self.assertEqual(data["active_count"], 0)
        self.assertEqual(data["scanned_count"], 0)

    def test_endpoint_balanced_market_returns_healthy(self):
        """Balanced market returns HTTP 200 with HEALTHY status and 6 scanned loops."""
        market = SyntheticMarketGenerator.generate_market("balanced")
        self.store.update_tickers_sync(list(market.values()))

        response = self.client.get("/api/triangular-cycles")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "HEALTHY")
        self.assertEqual(data["scanned_count"], 6)
        self.assertEqual(data["active_count"], 0)
        self.assertEqual(len(data["opportunities"]), 6)

    def test_endpoint_arbitrage_market_detects_opportunity(self):
        """Injected arbitrage generates active opportunities."""
        market = SyntheticMarketGenerator.generate_market("arbitrage_eth")
        self.store.update_tickers_sync(list(market.values()))

        response = self.client.get("/api/triangular-cycles")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(data["active_count"], 1)

        active_opps = [o for o in data["opportunities"] if o["is_arbitrage"]]
        self.assertGreaterEqual(len(active_opps), 1)
        self.assertGreater(active_opps[0]["net_yield_pct"], 0.05)
        self.assertGreater(active_opps[0]["max_executable_volume_quote"], 0.0)

    def test_endpoint_active_only_filtering(self):
        """active_only=true returns only active opportunities while scanned_count is 6."""
        market = SyntheticMarketGenerator.generate_market("arbitrage_eth")
        self.store.update_tickers_sync(list(market.values()))

        response = self.client.get("/api/triangular-cycles?active_only=true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["scanned_count"], 6)
        self.assertEqual(len(data["opportunities"]), data["active_count"])
        for o in data["opportunities"]:
            self.assertTrue(o["is_arbitrage"])

    def test_endpoint_fee_overrides(self):
        """fee and fee_bps parameters adjust net yields."""
        market = SyntheticMarketGenerator.generate_market("balanced")
        self.store.update_tickers_sync(list(market.values()))

        resp_10bps = self.client.get("/api/triangular-cycles?fee_bps=10.0").json()
        resp_0bps = self.client.get("/api/triangular-cycles?fee_bps=0.0").json()

        y_10 = resp_10bps["opportunities"][0]["net_yield_pct"]
        y_0 = resp_0bps["opportunities"][0]["net_yield_pct"]
        self.assertLess(y_10, y_0)

        # Fraction override
        resp_frac = self.client.get("/api/triangular-cycles?fee=0.00075").json()
        y_frac = resp_frac["opportunities"][0]["net_yield_pct"]
        self.assertGreater(y_frac, y_10)

    def test_endpoint_min_yield_override(self):
        """min_yield query parameter raises bar for active classification."""
        market = SyntheticMarketGenerator.generate_market("arbitrage_eth")
        self.store.update_tickers_sync(list(market.values()))

        response = self.client.get("/api/triangular-cycles?min_yield=100.0")
        data = response.json()
        self.assertEqual(data["active_count"], 0)

    def test_endpoint_anti_caching_headers(self):
        """Response includes strict anti-caching HTTP headers."""
        response = self.client.get("/api/triangular-cycles")
        self.assertIn("no-cache", response.headers.get("Cache-Control", ""))
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        self.assertEqual(response.headers.get("Expires"), "0")


if __name__ == "__main__":
    unittest.main()
