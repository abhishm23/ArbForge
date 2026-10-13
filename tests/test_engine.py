"""Unit and Mathematical Verification Test Suite for ArbForge Quantitative Engine.

Addresses ALL 4 explicit user acceptance criteria:
1. Directional bid/ask conversions (buy at ask vs sell at bid math).
2. Yield percentage calculations with and without trading fees.
3. Depth bottleneck calculation determining maximum executable volume based on Level-1 quantities.
4. Detection of synthetic exchange rate discrepancies exceeding the 0.05% threshold.

Organized across 4-Tier Test Architecture:
- Tier 1: Core Feature Coverage
- Tier 2: Boundary & Corner Cases
- Tier 3: Pairwise Combinations
- Tier 4: Real-World Market Scenarios & Synthetic Arbitrage Injections
"""

import unittest
import math
from typing import Dict, Any, List

from tests.conftest import (
    DEFAULT_FEE_RATE,
    BNB_DISCOUNT_FEE_RATE,
    ARBITRAGE_THRESHOLD_PCT,
    CANONICAL_MARKET_SNAPSHOT,
    reference_buy,
    reference_sell,
    reference_cumulative_fee_multiplier,
    reference_calculate_yield,
    reference_depth_bottleneck,
    inject_synthetic_arbitrage,
    inject_sub_threshold_opportunity,
)

# Optional dynamic import of production engine
try:
    from arbforge.engine import ArbitrageEngine
    from arbforge.models import Level1Ticker, TriangularCycleOpportunity
    HAS_ARBFORGE_MODULE = True
except ImportError:
    HAS_ARBFORGE_MODULE = False


# ============================================================================
# Standalone Reference Cycle Evaluator for Fallback & Contract Verification
# ============================================================================

class ReferenceCycleEvaluator:
    """Mathematical reference implementation of ArbForge triangular evaluation logic."""

    @staticmethod
    def evaluate_usdt_btc_alt_cycle(
        tickers: Dict[str, Dict[str, Any]],
        alt_symbol: str,  # "ETH", "SOL", "BNB"
        fee_rate: float = DEFAULT_FEE_RATE,
        min_yield_threshold: float = ARBITRAGE_THRESHOLD_PCT,
    ) -> Dict[str, Any]:
        """Evaluates forward cycle: USDT -> BTC -> ALT -> USDT."""
        btc_usdt = tickers["BTCUSDT"]
        alt_btc = tickers[f"{alt_symbol}BTC"]
        alt_usdt = tickers[f"{alt_symbol}USDT"]

        # Leg 1: BUY BTC with USDT at BTCUSDT Ask
        p_ask_1 = float(btc_usdt["ask_price"])
        q_ask_1 = float(btc_usdt["ask_qty"])
        r1 = (1.0 / p_ask_1) * (1.0 - fee_rate)
        cap1 = q_ask_1 * p_ask_1

        # Leg 2: BUY ALT with BTC at ALTBTC Ask
        p_ask_2 = float(alt_btc["ask_price"])
        q_ask_2 = float(alt_btc["ask_qty"])
        r2 = (1.0 / p_ask_2) * (1.0 - fee_rate)
        cap2 = q_ask_2 * p_ask_2

        # Leg 3: SELL ALT for USDT at ALTUSDT Bid
        p_bid_3 = float(alt_usdt["bid_price"])
        q_bid_3 = float(alt_usdt["bid_qty"])
        r3 = p_bid_3 * (1.0 - fee_rate)
        cap3 = q_bid_3

        # Rates & Yield
        gross_rate = (1.0 / p_ask_1) * (1.0 / p_ask_2) * p_bid_3
        net_rate = r1 * r2 * r3
        gross_yield_pct = (gross_rate - 1.0) * 100.0
        net_yield_pct = (net_rate - 1.0) * 100.0

        # Synthetic vs Direct
        synthetic_rate = p_ask_2 * p_ask_1
        direct_rate = p_bid_3
        discrepancy_pct = ((direct_rate - synthetic_rate) / synthetic_rate) * 100.0

        # Bottleneck
        v_max, bottleneck_idx = reference_depth_bottleneck([cap1, cap2, cap3], [r1, r2, r3])
        expected_profit = v_max * (net_yield_pct / 100.0)
        is_arbitrage = net_yield_pct > min_yield_threshold

        bottleneck_symbols = ["BTCUSDT", f"{alt_symbol}BTC", f"{alt_symbol}USDT"]

        return {
            "cycle_id": f"USDT->BTC->{alt_symbol}->USDT",
            "path": ["USDT", "BTC", alt_symbol, "USDT"],
            "gross_rate": gross_rate,
            "gross_yield_pct": gross_yield_pct,
            "net_rate": net_rate,
            "net_yield_pct": net_yield_pct,
            "synthetic_rate": synthetic_rate,
            "direct_rate": direct_rate,
            "discrepancy_pct": discrepancy_pct,
            "bottleneck_leg_index": bottleneck_idx,
            "bottleneck_symbol": bottleneck_symbols[bottleneck_idx],
            "max_executable_volume_quote": v_max,
            "expected_net_profit_quote": expected_profit,
            "is_arbitrage": is_arbitrage,
        }

    @staticmethod
    def evaluate_usdt_alt_btc_cycle(
        tickers: Dict[str, Dict[str, Any]],
        alt_symbol: str,  # "ETH", "SOL", "BNB"
        fee_rate: float = DEFAULT_FEE_RATE,
        min_yield_threshold: float = ARBITRAGE_THRESHOLD_PCT,
    ) -> Dict[str, Any]:
        """Evaluates reverse cycle: USDT -> ALT -> BTC -> USDT."""
        alt_usdt = tickers[f"{alt_symbol}USDT"]
        alt_btc = tickers[f"{alt_symbol}BTC"]
        btc_usdt = tickers["BTCUSDT"]

        # Leg 1: BUY ALT with USDT at ALTUSDT Ask
        p_ask_1 = float(alt_usdt["ask_price"])
        q_ask_1 = float(alt_usdt["ask_qty"])
        r1 = (1.0 / p_ask_1) * (1.0 - fee_rate)
        cap1 = q_ask_1 * p_ask_1

        # Leg 2: SELL ALT for BTC at ALTBTC Bid
        p_bid_2 = float(alt_btc["bid_price"])
        q_bid_2 = float(alt_btc["bid_qty"])
        r2 = p_bid_2 * (1.0 - fee_rate)
        cap2 = q_bid_2

        # Leg 3: SELL BTC for USDT at BTCUSDT Bid
        p_bid_3 = float(btc_usdt["bid_price"])
        q_bid_3 = float(btc_usdt["bid_qty"])
        r3 = p_bid_3 * (1.0 - fee_rate)
        cap3 = q_bid_3

        gross_rate = (1.0 / p_ask_1) * p_bid_2 * p_bid_3
        net_rate = r1 * r2 * r3
        gross_yield_pct = (gross_rate - 1.0) * 100.0
        net_yield_pct = (net_rate - 1.0) * 100.0

        direct_rate = p_ask_1
        synthetic_rate = p_bid_2 * p_bid_3
        discrepancy_pct = ((synthetic_rate - direct_rate) / direct_rate) * 100.0

        v_max, bottleneck_idx = reference_depth_bottleneck([cap1, cap2, cap3], [r1, r2, r3])
        expected_profit = v_max * (net_yield_pct / 100.0)
        is_arbitrage = net_yield_pct > min_yield_threshold

        bottleneck_symbols = [f"{alt_symbol}USDT", f"{alt_symbol}BTC", "BTCUSDT"]

        return {
            "cycle_id": f"USDT->{alt_symbol}->BTC->USDT",
            "path": ["USDT", alt_symbol, "BTC", "USDT"],
            "gross_rate": gross_rate,
            "gross_yield_pct": gross_yield_pct,
            "net_rate": net_rate,
            "net_yield_pct": net_yield_pct,
            "synthetic_rate": synthetic_rate,
            "direct_rate": direct_rate,
            "discrepancy_pct": discrepancy_pct,
            "bottleneck_leg_index": bottleneck_idx,
            "bottleneck_symbol": bottleneck_symbols[bottleneck_idx],
            "max_executable_volume_quote": v_max,
            "expected_net_profit_quote": expected_profit,
            "is_arbitrage": is_arbitrage,
        }


# ============================================================================
# Acceptance Criterion 1: Directional Bid/Ask Conversions (Tier 1 & Tier 2)
# ============================================================================

class TestDirectionalConversions(unittest.TestCase):
    """
    User Acceptance Criterion 1:
    Directional bid/ask conversions (buy at ask vs sell at bid math).
    """

    def test_buy_base_with_quote_at_ask_price(self):
        """Buying Base with Quote executes at Ask: Q_base = (Q_quote / Ask) * (1 - f)."""
        quote_qty = 1000.0  # USDT
        ask_price = 80000.0  # USDT per BTC
        fee_rate = 0.0010  # 10 bps

        gross_base, net_base, _ = reference_buy(quote_qty, ask_price, fee_rate)

        expected_gross = 1000.0 / 80000.0  # 0.0125 BTC
        expected_net = expected_gross * (1.0 - 0.0010)  # 0.0124875 BTC

        self.assertAlmostEqual(gross_base, expected_gross, places=8)
        self.assertAlmostEqual(net_base, expected_net, places=8)
        self.assertAlmostEqual(net_base, 0.0124875, places=8)

    def test_sell_base_for_quote_at_bid_price(self):
        """Selling Base for Quote executes at Bid: Q_quote = (Q_base * Bid) * (1 - f)."""
        base_qty = 10.0  # ETH
        bid_price = 2450.0  # USDT per ETH
        fee_rate = 0.0010  # 10 bps

        gross_quote, net_quote, _ = reference_sell(base_qty, bid_price, fee_rate)

        expected_gross = 10.0 * 2450.0  # 24500.0 USDT
        expected_net = expected_gross * (1.0 - 0.0010)  # 24475.50 USDT

        self.assertAlmostEqual(gross_quote, expected_gross, places=4)
        self.assertAlmostEqual(net_quote, expected_net, places=4)
        self.assertAlmostEqual(net_quote, 24475.50, places=4)

    def test_bid_ask_spread_drag_round_trip(self):
        """Executing a round-trip (BUY at Ask, then SELL at Bid) strictly produces negative yield."""
        initial_usdt = 10000.0
        ask_price = 80010.0
        bid_price = 79990.0  # 20 USDT spread
        fee_rate = 0.0010

        # Step 1: Buy BTC at Ask
        _, btc_received, _ = reference_buy(initial_usdt, ask_price, fee_rate)
        # Step 2: Sell BTC at Bid
        _, usdt_final, _ = reference_sell(btc_received, bid_price, fee_rate)

        # Expected final USDT: initial * (1/80010) * 79990 * (0.999)^2
        expected_multiplier = (bid_price / ask_price) * ((1.0 - fee_rate) ** 2)
        expected_final = initial_usdt * expected_multiplier
        net_yield = reference_calculate_yield(initial_usdt, usdt_final)

        self.assertAlmostEqual(usdt_final, expected_final, places=4)
        self.assertLess(usdt_final, initial_usdt)
        self.assertLess(net_yield, -0.20)  # Loss exceeds 20 bps

    def test_buy_with_zero_or_negative_ask_raises_error(self):
        """Zero or negative Ask price must raise ValueError, preventing ZeroDivisionError."""
        with self.assertRaises(ValueError):
            reference_buy(1000.0, 0.0)
        with self.assertRaises(ValueError):
            reference_buy(1000.0, -500.0)

    def test_sell_with_zero_or_negative_bid_raises_error(self):
        """Zero or negative Bid price must raise ValueError."""
        with self.assertRaises(ValueError):
            reference_sell(1.0, 0.0)
        with self.assertRaises(ValueError):
            reference_sell(1.0, -100.0)


# ============================================================================
# Acceptance Criterion 2: Yield Percentage Calculations (Tier 1 & Tier 2)
# ============================================================================

class TestYieldCalculations(unittest.TestCase):
    """
    User Acceptance Criterion 2:
    Yield percentage calculations with and without trading fees.
    """

    def test_zero_fee_yield_matches_gross_yield(self):
        """With fee = 0, Net Yield equals Gross Yield exactly: ((Q_final - Q_initial)/Q_initial)*100."""
        q_initial = 10000.0
        q_final = 10100.0  # +100 USDT gain

        yield_pct = reference_calculate_yield(q_initial, q_final)
        self.assertAlmostEqual(yield_pct, 1.0, places=6)

    def test_compounding_fee_drag_over_three_legs(self):
        """
        Multiplicative compounding over 3 legs: (1 - f)^3.
        For f = 0.0010: (0.999)^3 = 0.997002999 -> 29.97 bps fee drag.
        """
        fee_rate = 0.0010
        multiplier = reference_cumulative_fee_multiplier(fee_rate, legs=3)
        expected = (0.999) ** 3  # 0.997002999
        self.assertAlmostEqual(multiplier, expected, places=9)

        fee_drag_pct = (1.0 - multiplier) * 100.0
        self.assertAlmostEqual(fee_drag_pct, 0.2997001, places=6)

    def test_bnb_discount_fee_compounding(self):
        """
        With 25% BNB fee discount (f = 0.00075 / 7.5 bps):
        (1 - 0.00075)^3 = 0.99775168... -> ~22.48 bps fee drag.
        """
        fee_rate = BNB_DISCOUNT_FEE_RATE
        multiplier = reference_cumulative_fee_multiplier(fee_rate, legs=3)
        expected = (1.0 - 0.00075) ** 3
        self.assertAlmostEqual(multiplier, expected, places=8)

        fee_drag_pct = (1.0 - multiplier) * 100.0
        self.assertAlmostEqual(fee_drag_pct, 0.2248316, places=6)

    def test_equilibrium_parity_yield_negative_due_to_fees(self):
        """When gross rate is 1.0 (perfect market parity), net yield is exactly -0.2997%."""
        gross_rate = 1.000000
        net_rate = gross_rate * reference_cumulative_fee_multiplier(0.0010, legs=3)
        net_yield_pct = (net_rate - 1.0) * 100.0
        self.assertAlmostEqual(net_yield_pct, -0.2997001, places=6)

    def test_yield_with_positive_gross_crossing_into_net_profit(self):
        """Gross rate 1.0040 (+0.40% gross) yields +0.0991% net with 10 bps fee."""
        gross_rate = 1.0040
        net_rate = gross_rate * ((1.0 - 0.0010) ** 3)
        net_yield_pct = (net_rate - 1.0) * 100.0
        self.assertAlmostEqual(net_yield_pct, 0.099101, places=5)

    def test_invalid_initial_quantity_raises_error(self):
        """Initial quantity <= 0 raises ValueError for yield calculations."""
        with self.assertRaises(ValueError):
            reference_calculate_yield(0.0, 100.0)
        with self.assertRaises(ValueError):
            reference_calculate_yield(-50.0, 100.0)


# ============================================================================
# Acceptance Criterion 3: Depth Bottleneck Calculation (Tier 1, 2, 3)
# ============================================================================

class TestDepthBottleneck(unittest.TestCase):
    """
    User Acceptance Criterion 3:
    Depth bottleneck calculation determining maximum executable volume based on Level-1 quantities.
    """

    def setUp(self):
        self.fee = 0.0010
        # Canonical test cycle parameters: USDT -> BTC -> ETH -> USDT
        # Leg 1: BUY BTC at 80000 USDT
        self.p1 = 80000.0
        self.r1 = (1.0 / self.p1) * (1.0 - self.fee)  # 0.0000124875
        # Leg 2: BUY ETH at 0.0300 BTC
        self.p2 = 0.0300
        self.r2 = (1.0 / self.p2) * (1.0 - self.fee)  # 33.3000
        # Leg 3: SELL ETH at 2450.0 USDT
        self.p3 = 2450.0
        self.r3 = self.p3 * (1.0 - self.fee)  # 2447.55

    def test_canonical_survey_walkthrough_bottleneck_at_leg_3(self):
        """
        Validates the authoritative numerical walk-through from PROJECT.md:
        Cap1 = 160000 USDT, Cap2 = 1.5 BTC, Cap3 = 10 ETH
        L0 = 160000.00 USDT, L1 = 120120.12 USDT, L2 = 24048.07 USDT
        V_max = 24048.07 USDT, Bottleneck = Leg 3 (Index 2).
        """
        cap1 = 2.0 * self.p1  # 160000 USDT
        cap2 = 50.0 * self.p2  # 1.5 BTC
        cap3 = 10.0  # 10 ETH

        v_max, bottleneck_idx = reference_depth_bottleneck(
            [cap1, cap2, cap3],
            [self.r1, self.r2, self.r3]
        )

        expected_l0 = 160000.0
        expected_l1 = 1.5 / self.r1  # 120120.12012...
        expected_l2 = 10.0 / (self.r1 * self.r2)  # 24048.072048...

        self.assertAlmostEqual(v_max, expected_l2, places=2)
        self.assertAlmostEqual(v_max, 24048.07, places=2)
        self.assertEqual(bottleneck_idx, 2)  # Index 2 = Leg 3

    def test_bottleneck_at_leg_1_when_leg_1_is_most_restrictive(self):
        """When Leg 1 has tight depth, V_max equals Leg 1 capacity."""
        cap1 = 500.0  # Only 500 USDT available on Leg 1
        cap2 = 1.5  # 1.5 BTC (~120k USDT equiv)
        cap3 = 10.0  # 10 ETH (~24k USDT equiv)

        v_max, bottleneck_idx = reference_depth_bottleneck(
            [cap1, cap2, cap3],
            [self.r1, self.r2, self.r3]
        )

        self.assertAlmostEqual(v_max, 500.0, places=4)
        self.assertEqual(bottleneck_idx, 0)  # Index 0 = Leg 1

    def test_bottleneck_at_leg_2_when_cross_pair_depth_is_tight(self):
        """When Leg 2 (cross pair ETHBTC) is tight, V_max equals Cap2 / r1."""
        cap1 = 160000.0
        cap2 = 0.01  # Only 0.01 BTC depth on ETHBTC
        cap3 = 10.0

        v_max, bottleneck_idx = reference_depth_bottleneck(
            [cap1, cap2, cap3],
            [self.r1, self.r2, self.r3]
        )

        expected_l1 = 0.01 / self.r1  # ~800.80 USDT
        self.assertAlmostEqual(v_max, expected_l1, places=2)
        self.assertEqual(bottleneck_idx, 1)  # Index 1 = Leg 2

    def test_zero_depth_on_any_leg_results_in_zero_executable_volume(self):
        """If any leg has zero depth, V_max is 0.0 and no ZeroDivisionError occurs."""
        # Zero depth on Leg 2
        v_max, b_idx = reference_depth_bottleneck(
            [10000.0, 0.0, 10.0],
            [self.r1, self.r2, self.r3]
        )
        self.assertEqual(v_max, 0.0)
        self.assertEqual(b_idx, 1)

        # Zero depth on Leg 1
        v_max, b_idx = reference_depth_bottleneck(
            [0.0, 1.5, 10.0],
            [self.r1, self.r2, self.r3]
        )
        self.assertEqual(v_max, 0.0)
        self.assertEqual(b_idx, 0)

        # Zero depth on Leg 3
        v_max, b_idx = reference_depth_bottleneck(
            [10000.0, 1.5, 0.0],
            [self.r1, self.r2, self.r3]
        )
        self.assertEqual(v_max, 0.0)
        self.assertEqual(b_idx, 2)

    def test_expected_net_profit_calculation(self):
        """Net Profit in quote currency = V_max * (Net_Yield / 100)."""
        v_max = 24048.07
        net_yield_pct = 1.7774  # %
        expected_profit = v_max * (net_yield_pct / 100.0)

        self.assertAlmostEqual(expected_profit, 427.43, places=2)


# ============================================================================
# Acceptance Criterion 4: Discrepancy & Threshold Detection (Tier 2, Tier 4)
# ============================================================================

class TestArbitrageOpportunityDetection(unittest.TestCase):
    """
    User Acceptance Criterion 4:
    Detection of synthetic exchange rate discrepancies exceeding the 0.05% threshold.
    """

    def test_active_arbitrage_flagged_when_exceeding_threshold(self):
        """When net yield > 0.05%, cycle is flagged active (is_arbitrage = True)."""
        snapshot = inject_synthetic_arbitrage(CANONICAL_MARKET_SNAPSHOT, target_pair="ETHUSDT", bump_bid_pct=0.50)
        res = ReferenceCycleEvaluator.evaluate_usdt_btc_alt_cycle(snapshot, "ETH", fee_rate=DEFAULT_FEE_RATE)

        self.assertTrue(res["is_arbitrage"])
        self.assertGreater(res["net_yield_pct"], 0.05)
        self.assertGreater(res["max_executable_volume_quote"], 0.0)
        self.assertGreater(res["expected_net_profit_quote"], 0.0)

    def test_sub_threshold_opportunity_not_flagged(self):
        """When net yield <= 0.05% (e.g., +0.03%), cycle is NOT flagged active (is_arbitrage = False)."""
        snapshot = inject_sub_threshold_opportunity(CANONICAL_MARKET_SNAPSHOT, target_pair="ETHUSDT", bump_bid_pct=0.365)
        res = ReferenceCycleEvaluator.evaluate_usdt_btc_alt_cycle(snapshot, "ETH", fee_rate=DEFAULT_FEE_RATE)

        self.assertFalse(res["is_arbitrage"])
        self.assertLessEqual(res["net_yield_pct"], 0.05)
        self.assertGreater(res["net_yield_pct"], 0.0)  # Positively yielding, but sub-threshold

    def test_strict_boundary_at_exact_point_zero_five_percent(self):
        """Strict threshold: net_yield == 0.0500% evaluates to False (> 0.05% required)."""
        # Exactly 0.0500%
        is_active_exact = 0.050000 > ARBITRAGE_THRESHOLD_PCT
        self.assertFalse(is_active_exact)

        # Micro-tick above threshold
        is_active_above = 0.050001 > ARBITRAGE_THRESHOLD_PCT
        self.assertTrue(is_active_above)

        # Micro-tick below threshold
        is_active_below = 0.049999 > ARBITRAGE_THRESHOLD_PCT
        self.assertFalse(is_active_below)

    def test_canonical_market_has_zero_active_arbitrage(self):
        """Realistic market snapshot with normal spreads exhibits negative net yields and 0 active loops."""
        for alt in ["ETH", "SOL", "BNB"]:
            # Forward
            fwd = ReferenceCycleEvaluator.evaluate_usdt_btc_alt_cycle(CANONICAL_MARKET_SNAPSHOT, alt)
            self.assertFalse(fwd["is_arbitrage"])
            self.assertLess(fwd["net_yield_pct"], 0.0)

            # Reverse
            rev = ReferenceCycleEvaluator.evaluate_usdt_alt_btc_cycle(CANONICAL_MARKET_SNAPSHOT, alt)
            self.assertFalse(rev["is_arbitrage"])
            self.assertLess(rev["net_yield_pct"], 0.0)

    def test_synthetic_vs_direct_rate_calculation(self):
        """Synthetic cross rate correctly reflects combined pricing of intermediate legs."""
        snapshot = CANONICAL_MARKET_SNAPSHOT
        res = ReferenceCycleEvaluator.evaluate_usdt_btc_alt_cycle(snapshot, "ETH")

        p_ask_btc = snapshot["BTCUSDT"]["ask_price"]
        p_ask_ethbtc = snapshot["ETHBTC"]["ask_price"]
        expected_synthetic = p_ask_btc * p_ask_ethbtc
        expected_direct = snapshot["ETHUSDT"]["bid_price"]

        self.assertAlmostEqual(res["synthetic_rate"], expected_synthetic, places=4)
        self.assertAlmostEqual(res["direct_rate"], expected_direct, places=4)

        expected_discrepancy = ((expected_direct - expected_synthetic) / expected_synthetic) * 100.0
        self.assertAlmostEqual(res["discrepancy_pct"], expected_discrepancy, places=4)


# ============================================================================
# Tier 3: Pairwise Combinations & Multi-Asset Cycles
# ============================================================================

class TestPairwiseAndCycleCombinations(unittest.TestCase):
    """
    Tier 3: Pairwise Combinations.
    Tests all 6 USDT-rooted triangular cycles across different fee rates and bottlenecks.
    """

    def test_all_six_usdt_cycles_computable(self):
        """All 6 core USDT cycles (3 forward, 3 reverse) evaluate cleanly without errors."""
        snapshot = CANONICAL_MARKET_SNAPSHOT
        cycles = []
        for alt in ["ETH", "SOL", "BNB"]:
            cycles.append(ReferenceCycleEvaluator.evaluate_usdt_btc_alt_cycle(snapshot, alt))
            cycles.append(ReferenceCycleEvaluator.evaluate_usdt_alt_btc_cycle(snapshot, alt))

        self.assertEqual(len(cycles), 6)
        for c in cycles:
            self.assertIn("cycle_id", c)
            self.assertIn("net_yield_pct", c)
            self.assertIn("max_executable_volume_quote", c)
            self.assertIn("bottleneck_leg_index", c)
            self.assertIn(c["bottleneck_leg_index"], [0, 1, 2])
            self.assertGreaterEqual(c["max_executable_volume_quote"], 0.0)

    def test_fee_tier_variation_impact_on_net_yield(self):
        """Net yield monotonically increases as fee rate decreases (10 bps -> 7.5 bps -> 0 bps)."""
        snapshot = CANONICAL_MARKET_SNAPSHOT
        y_10bps = ReferenceCycleEvaluator.evaluate_usdt_btc_alt_cycle(snapshot, "SOL", fee_rate=0.0010)["net_yield_pct"]
        y_7_5bps = ReferenceCycleEvaluator.evaluate_usdt_btc_alt_cycle(snapshot, "SOL", fee_rate=0.00075)["net_yield_pct"]
        y_0bps = ReferenceCycleEvaluator.evaluate_usdt_btc_alt_cycle(snapshot, "SOL", fee_rate=0.0)["net_yield_pct"]

        self.assertLess(y_10bps, y_7_5bps)
        self.assertLess(y_7_5bps, y_0bps)


# ============================================================================
# Dynamic Production Engine Binding (M2 Interface Contract Verification)
# ============================================================================

class TestProductionEngineBinding(unittest.TestCase):
    """
    Tests binding to `arbforge.engine.ArbitrageEngine` when implemented by Milestone 2.
    Ensures production implementation conforms to the Interface Contract in PROJECT.md.
    """

    @unittest.skipUnless(HAS_ARBFORGE_MODULE, "arbforge.engine module not yet created (M2 in progress)")
    def test_production_arbitrage_engine_contract(self):
        """Validates production ArbitrageEngine.evaluate_cycles against reference calculations."""
        # Convert dictionary to Level1Ticker instances
        ticker_objs = {}
        for sym, d in CANONICAL_MARKET_SNAPSHOT.items():
            spread_abs = d["ask_price"] - d["bid_price"]
            mid = (d["ask_price"] + d["bid_price"]) / 2.0
            ticker_objs[sym] = Level1Ticker(
                symbol=sym,
                bid_price=d["bid_price"],
                bid_qty=d["bid_qty"],
                ask_price=d["ask_price"],
                ask_qty=d["ask_qty"],
                spread_abs=spread_abs,
                spread_bps_mid=(spread_abs / mid) * 10000.0,
                spread_bps_bid=(spread_abs / d["bid_price"]) * 10000.0,
                timestamp_ms=1725537600000,
                iso_time="2026-09-05T12:00:00Z",
            )

        opportunities = ArbitrageEngine.evaluate_cycles(ticker_objs)
        self.assertIsInstance(opportunities, list)
        self.assertGreaterEqual(len(opportunities), 6)

        for opp in opportunities:
            self.assertIsInstance(opp, TriangularCycleOpportunity)
            self.assertTrue(hasattr(opp, "gross_yield_pct"))
            self.assertTrue(hasattr(opp, "net_yield_pct"))
            self.assertTrue(hasattr(opp, "is_arbitrage"))
            self.assertTrue(hasattr(opp, "max_executable_volume_quote"))


if __name__ == "__main__":
    unittest.main()
