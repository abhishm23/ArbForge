"""ArbForge Test Fixtures and Authoritative Reference Oracle.

Provides reusable fixtures, synthetic data generators, and mathematical reference
implementations for both pytest and unittest execution tracks.
"""

import copy
import pytest
from typing import Dict, Any, List, Tuple

CORE_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "ETHBTC",
    "SOLUSDT",
    "SOLBTC",
    "BNBUSDT",
    "BNBBTC",
]

DEFAULT_FEE_RATE = 0.0010  # 10 bps
BNB_DISCOUNT_FEE_RATE = 0.00075  # 7.5 bps
ARBITRAGE_THRESHOLD_PCT = 0.05  # 5 bps

# Canonical real-market snapshot observed from Binance bookTicker
CANONICAL_MARKET_SNAPSHOT: Dict[str, Dict[str, Any]] = {
    "BTCUSDT": {
        "symbol": "BTCUSDT",
        "bid_price": 79624.00,
        "bid_qty": 3.72656,
        "ask_price": 79624.01,
        "ask_qty": 2.59443,
    },
    "ETHUSDT": {
        "symbol": "ETHUSDT",
        "bid_price": 2453.16,
        "bid_qty": 20.69370,
        "ask_price": 2453.17,
        "ask_qty": 2.85550,
    },
    "ETHBTC": {
        "symbol": "ETHBTC",
        "bid_price": 0.030810,
        "bid_qty": 15.12890,
        "ask_price": 0.030820,
        "ask_qty": 57.93050,
    },
    "SOLUSDT": {
        "symbol": "SOLUSDT",
        "bid_price": 102.30,
        "bid_qty": 363.97600,
        "ask_price": 102.31,
        "ask_qty": 386.86500,
    },
    "SOLBTC": {
        "symbol": "SOLBTC",
        "bid_price": 0.0012847,
        "bid_qty": 18.10200,
        "ask_price": 0.0012848,
        "ask_qty": 4.49600,
    },
    "BNBUSDT": {
        "symbol": "BNBUSDT",
        "bid_price": 751.75,
        "bid_qty": 0.85800,
        "ask_price": 751.76,
        "ask_qty": 20.27100,
    },
    "BNBBTC": {
        "symbol": "BNBBTC",
        "bid_price": 0.009440,
        "bid_qty": 0.20200,
        "ask_price": 0.009441,
        "ask_qty": 4.11200,
    },
}


# ============================================================================
# Authoritative Mathematical Reference Oracle
# ============================================================================

def reference_buy(quote_qty: float, ask_price: float, fee_rate: float = DEFAULT_FEE_RATE) -> Tuple[float, float, float]:
    """
    Directional BUY Base with Quote at Ask:
    Returns (gross_base, net_base, max_quote_capacity)
    """
    if ask_price <= 0:
        raise ValueError("Ask price must be strictly positive")
    gross_base = quote_qty / ask_price
    net_base = gross_base * (1.0 - fee_rate)
    return gross_base, net_base, ask_price


def reference_sell(base_qty: float, bid_price: float, fee_rate: float = DEFAULT_FEE_RATE) -> Tuple[float, float, float]:
    """
    Directional SELL Base for Quote at Bid:
    Returns (gross_quote, net_quote, bid_price)
    """
    if bid_price <= 0:
        raise ValueError("Bid price must be strictly positive")
    gross_quote = base_qty * bid_price
    net_quote = gross_quote * (1.0 - fee_rate)
    return gross_quote, net_quote, bid_price


def reference_cumulative_fee_multiplier(fee_rate: float = DEFAULT_FEE_RATE, legs: int = 3) -> float:
    """Returns (1 - fee_rate) ^ legs."""
    return (1.0 - fee_rate) ** legs


def reference_calculate_yield(q_initial: float, q_final: float) -> float:
    """Yield % = ((Q_final - Q_initial) / Q_initial) * 100."""
    if q_initial <= 0:
        raise ValueError("Initial quantity must be strictly positive")
    return ((q_final - q_initial) / q_initial) * 100.0


def reference_depth_bottleneck(capacities: List[float], net_rates: List[float]) -> Tuple[float, int]:
    """
    Propagate depth backward from leg 3 to leg 1 to find V_max in quote currency.
    capacities: [Cap_1 (in quote), Cap_2 (in asset 1), Cap_3 (in asset 2)]
    net_rates: [r_1, r_2, r_3]
    Returns (v_max, bottleneck_index)
    """
    if len(capacities) != 3 or len(net_rates) != 3:
        raise ValueError("Exactly 3 legs required for triangular bottleneck")
    
    r1, r2, _ = net_rates
    c1, c2, c3 = capacities
    
    if any(c < 0 for c in capacities):
        raise ValueError("Capacities cannot be negative")
    
    if any(c == 0 for c in capacities):
        # If any leg has zero depth, executable volume is 0
        min_idx = next(i for i, c in enumerate(capacities) if c == 0)
        return 0.0, min_idx

    l0 = c1
    l1 = c2 / r1
    l2 = c3 / (r1 * r2)
    
    limits = [l0, l1, l2]
    v_max = min(limits)
    bottleneck_idx = limits.index(v_max)
    return v_max, bottleneck_idx


def inject_synthetic_arbitrage(
    base_snapshot: Dict[str, Dict[str, Any]],
    target_pair: str = "ETHUSDT",
    bump_bid_pct: float = 0.50
) -> Dict[str, Dict[str, Any]]:
    """
    Creates a pricing dislocation by increasing bid price on target_pair
    to guarantee > 0.05% net arbitrage yield.
    """
    snapshot = copy.deepcopy(base_snapshot)
    orig_bid = snapshot[target_pair]["bid_price"]
    bumped_bid = orig_bid * (1.0 + (bump_bid_pct / 100.0))
    snapshot[target_pair]["bid_price"] = bumped_bid
    # Keep ask consistent if needed
    if snapshot[target_pair]["ask_price"] <= bumped_bid:
        snapshot[target_pair]["ask_price"] = bumped_bid * 1.0001
    return snapshot


def inject_sub_threshold_opportunity(
    base_snapshot: Dict[str, Dict[str, Any]],
    target_pair: str = "ETHUSDT",
    bump_bid_pct: float = 0.365
) -> Dict[str, Dict[str, Any]]:
    """
    Creates a mild pricing dislocation yielding ~+0.03% (strictly between 0.0% and 0.05% threshold).
    """
    snapshot = copy.deepcopy(base_snapshot)
    orig_bid = snapshot[target_pair]["bid_price"]
    # 0.365% bump overcomes initial spread + fee drag to yield ~ +0.03%, strictly < 0.05%
    bumped_bid = orig_bid * (1.0 + (bump_bid_pct / 100.0))
    snapshot[target_pair]["bid_price"] = bumped_bid
    if snapshot[target_pair]["ask_price"] <= bumped_bid:
        snapshot[target_pair]["ask_price"] = bumped_bid * 1.0001
    return snapshot


# ============================================================================
# Pytest Fixtures
# ============================================================================

@pytest.fixture
def canonical_market() -> Dict[str, Dict[str, Any]]:
    return copy.deepcopy(CANONICAL_MARKET_SNAPSHOT)


@pytest.fixture
def arbitrage_market() -> Dict[str, Dict[str, Any]]:
    return inject_synthetic_arbitrage(CANONICAL_MARKET_SNAPSHOT, target_pair="ETHUSDT", bump_bid_pct=0.50)


@pytest.fixture
def sub_threshold_market() -> Dict[str, Dict[str, Any]]:
    return inject_sub_threshold_opportunity(CANONICAL_MARKET_SNAPSHOT, target_pair="ETHUSDT", bump_bid_pct=0.33)
