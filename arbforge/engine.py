"""
arbforge/engine.py
Quantitative Triangular Arbitrage Engine, Compounding Fee Math & Depth Bottleneck.

Authoritative implementation conforming strictly to ORIGINAL_REQUEST.md,
PROJECT.md, and tests/conftest.py oracle.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from arbforge.models import ConversionLeg, Level1Ticker, TriangularCycleOpportunity

# Canonical Defaults
DEFAULT_FEE_RATE: float = 0.0010  # 10 bps
BNB_DISCOUNT_FEE_RATE: float = 0.00075  # 7.5 bps
ARBITRAGE_THRESHOLD_PCT: float = 0.05  # 5 bps
DEFAULT_MIN_YIELD_THRESHOLD: float = 0.05  # 5 bps
CORE_ALTS: List[str] = ["ETH", "SOL", "BNB"]


# ============================================================================
# Pure Mathematical Core Functions
# ============================================================================

def directional_buy(
    quote_qty: float,
    ask_price: float,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> Tuple[float, float, float]:
    """
    Directional BUY Base with Quote at Ask:
    Q_base_gross = quote_qty / ask_price
    Q_base_net = Q_base_gross * (1 - fee_rate)
    Returns (gross_base, net_base, ask_price)
    """
    if ask_price <= 0.0:
        raise ValueError(f"Ask price must be strictly positive, got {ask_price}")
    if quote_qty < 0.0:
        raise ValueError(f"Quote quantity cannot be negative, got {quote_qty}")

    gross_base = quote_qty / ask_price
    net_base = gross_base * (1.0 - fee_rate)
    return gross_base, net_base, ask_price


def directional_sell(
    base_qty: float,
    bid_price: float,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> Tuple[float, float, float]:
    """
    Directional SELL Base for Quote at Bid:
    Q_quote_gross = base_qty * bid_price
    Q_quote_net = Q_quote_gross * (1 - fee_rate)
    Returns (gross_quote, net_quote, bid_price)
    """
    if bid_price <= 0.0:
        raise ValueError(f"Bid price must be strictly positive, got {bid_price}")
    if base_qty < 0.0:
        raise ValueError(f"Base quantity cannot be negative, got {base_qty}")

    gross_quote = base_qty * bid_price
    net_quote = gross_quote * (1.0 - fee_rate)
    return gross_quote, net_quote, bid_price


def cumulative_fee_multiplier(fee_rate: float = DEFAULT_FEE_RATE, legs: int = 3) -> float:
    """Returns (1.0 - fee_rate) ** legs."""
    return (1.0 - fee_rate) ** legs


def calculate_yield_pct(initial_qty: float, final_qty: float) -> float:
    """Yield % = ((final_qty - initial_qty) / initial_qty) * 100.0."""
    if initial_qty <= 0.0:
        raise ValueError(f"Initial quantity must be strictly positive, got {initial_qty}")
    return ((final_qty - initial_qty) / initial_qty) * 100.0


# Backward compatibility alias
calculate_yield = calculate_yield_pct


def calculate_depth_bottleneck(
    capacities: List[float],
    net_rates: List[float],
) -> Tuple[float, int]:
    """
    Backward depth bottleneck propagation for 3 sequential legs.
    capacities: [Cap_1 (in quote), Cap_2 (in asset 1), Cap_3 (in asset 2)]
    net_rates: [r1, r2, r3]
    Returns (v_max, bottleneck_index)
    """
    if len(capacities) != 3 or len(net_rates) != 3:
        raise ValueError("Exactly 3 legs required for triangular bottleneck calculation")

    if any(c < 0.0 for c in capacities):
        raise ValueError("Capacities cannot be negative")

    # If any leg has zero depth, executable volume is 0
    if any(c == 0.0 for c in capacities):
        min_idx = next(i for i, c in enumerate(capacities) if c == 0.0)
        return 0.0, min_idx

    r1, r2, _ = net_rates
    c1, c2, c3 = capacities

    l0 = c1
    l1 = c2 / r1
    l2 = c3 / (r1 * r2)

    limits = [l0, l1, l2]
    v_max = min(limits)
    bottleneck_idx = limits.index(v_max)
    return v_max, bottleneck_idx


# Backward compatibility alias
depth_bottleneck = calculate_depth_bottleneck


# ============================================================================
# Extraction Helper (Supports Level1Ticker and dict)
# ============================================================================

def _extract_ticker(
    tickers: Dict[str, Any],
    symbol: str,
) -> Dict[str, float]:
    """Extracts bid/ask prices and quantities with duck-typing support."""
    if symbol not in tickers:
        raise KeyError(f"Missing required ticker symbol: {symbol}")

    t = tickers[symbol]
    if isinstance(t, dict):
        p_bid = float(t.get("bid_price", t.get("bidPrice", 0.0)))
        q_bid = float(t.get("bid_qty", t.get("bidQty", 0.0)))
        p_ask = float(t.get("ask_price", t.get("askPrice", 0.0)))
        q_ask = float(t.get("ask_qty", t.get("askQty", 0.0)))
    else:
        p_bid = float(t.bid_price)
        q_bid = float(t.bid_qty)
        p_ask = float(t.ask_price)
        q_ask = float(t.ask_qty)

    if p_ask <= 0.0 or not math.isfinite(p_ask):
        raise ValueError(f"Ask price must be strictly positive, got {p_ask} for {symbol}")
    if p_bid <= 0.0 or not math.isfinite(p_bid):
        raise ValueError(f"Bid price must be strictly positive, got {p_bid} for {symbol}")

    return {
        "bid_price": p_bid,
        "bid_qty": max(0.0, q_bid),
        "ask_price": p_ask,
        "ask_qty": max(0.0, q_ask),
    }


# ============================================================================
# ArbitrageEngine Class Implementation
# ============================================================================

class ArbitrageEngine:
    """Quantitative triangular arbitrage scanner and depth bottleneck evaluator."""

    @classmethod
    def evaluate_usdt_btc_alt_cycle(
        cls,
        tickers: Dict[str, Any],
        alt_symbol: str,
        fee_rate: float = DEFAULT_FEE_RATE,
        min_yield_threshold: float = DEFAULT_MIN_YIELD_THRESHOLD,
        timestamp_ms: Optional[int] = None,
    ) -> TriangularCycleOpportunity:
        """
        Evaluates forward cycle: USDT -> BTC -> ALT -> USDT.
        Leg 1: BUY BTC with USDT at BTCUSDT Ask
        Leg 2: BUY ALT with BTC at ALTBTC Ask
        Leg 3: SELL ALT for USDT at ALTUSDT Bid
        """
        now_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)

        btc_usdt = _extract_ticker(tickers, "BTCUSDT")
        alt_btc = _extract_ticker(tickers, f"{alt_symbol}BTC")
        alt_usdt = _extract_ticker(tickers, f"{alt_symbol}USDT")

        p_ask_1 = btc_usdt["ask_price"]
        q_ask_1 = btc_usdt["ask_qty"]
        r1 = (1.0 / p_ask_1) * (1.0 - fee_rate)
        cap1 = q_ask_1 * p_ask_1

        p_ask_2 = alt_btc["ask_price"]
        q_ask_2 = alt_btc["ask_qty"]
        r2 = (1.0 / p_ask_2) * (1.0 - fee_rate)
        cap2 = q_ask_2 * p_ask_2

        p_bid_3 = alt_usdt["bid_price"]
        q_bid_3 = alt_usdt["bid_qty"]
        r3 = p_bid_3 * (1.0 - fee_rate)
        cap3 = q_bid_3

        # Rates & Yields
        gross_rate = (1.0 / p_ask_1) * (1.0 / p_ask_2) * p_bid_3
        net_rate = r1 * r2 * r3
        gross_yield_pct = (gross_rate - 1.0) * 100.0
        net_yield_pct = (net_rate - 1.0) * 100.0

        # Synthetic vs Direct
        synthetic_rate = p_ask_2 * p_ask_1
        direct_rate = p_bid_3
        discrepancy_pct = ((direct_rate - synthetic_rate) / synthetic_rate) * 100.0

        # Bottleneck
        v_max, bottleneck_idx = calculate_depth_bottleneck([cap1, cap2, cap3], [r1, r2, r3])
        expected_profit = v_max * (net_yield_pct / 100.0)
        is_arbitrage = net_yield_pct > min_yield_threshold

        bottleneck_symbols = ["BTCUSDT", f"{alt_symbol}BTC", f"{alt_symbol}USDT"]

        legs = [
            ConversionLeg(
                step=1,
                from_asset="USDT",
                to_asset="BTC",
                symbol="BTCUSDT",
                action="BUY",
                price=p_ask_1,
                available_qty_base=q_ask_1,
                taker_fee_rate=fee_rate,
                rate_multiplier=r1,
            ),
            ConversionLeg(
                step=2,
                from_asset="BTC",
                to_asset=alt_symbol,
                symbol=f"{alt_symbol}BTC",
                action="BUY",
                price=p_ask_2,
                available_qty_base=q_ask_2,
                taker_fee_rate=fee_rate,
                rate_multiplier=r2,
            ),
            ConversionLeg(
                step=3,
                from_asset=alt_symbol,
                to_asset="USDT",
                symbol=f"{alt_symbol}USDT",
                action="SELL",
                price=p_bid_3,
                available_qty_base=q_bid_3,
                taker_fee_rate=fee_rate,
                rate_multiplier=r3,
            ),
        ]

        return TriangularCycleOpportunity(
            cycle_id=f"USDT->BTC->{alt_symbol}->USDT",
            path=["USDT", "BTC", alt_symbol, "USDT"],
            legs=legs,
            gross_yield_pct=gross_yield_pct,
            net_yield_pct=net_yield_pct,
            is_arbitrage=is_arbitrage,
            synthetic_rate=synthetic_rate,
            direct_rate=direct_rate,
            discrepancy_pct=discrepancy_pct,
            bottleneck_leg_index=bottleneck_idx,
            bottleneck_symbol=bottleneck_symbols[bottleneck_idx],
            max_executable_volume_quote=v_max,
            expected_net_profit_quote=expected_profit,
            starting_asset="USDT",
            timestamp_ms=now_ms,
        )

    @classmethod
    def evaluate_usdt_alt_btc_cycle(
        cls,
        tickers: Dict[str, Any],
        alt_symbol: str,
        fee_rate: float = DEFAULT_FEE_RATE,
        min_yield_threshold: float = DEFAULT_MIN_YIELD_THRESHOLD,
        timestamp_ms: Optional[int] = None,
    ) -> TriangularCycleOpportunity:
        """
        Evaluates reverse cycle: USDT -> ALT -> BTC -> USDT.
        Leg 1: BUY ALT with USDT at ALTUSDT Ask
        Leg 2: SELL ALT for BTC at ALTBTC Bid
        Leg 3: SELL BTC for USDT at BTCUSDT Bid
        """
        now_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)

        alt_usdt = _extract_ticker(tickers, f"{alt_symbol}USDT")
        alt_btc = _extract_ticker(tickers, f"{alt_symbol}BTC")
        btc_usdt = _extract_ticker(tickers, "BTCUSDT")

        p_ask_1 = alt_usdt["ask_price"]
        q_ask_1 = alt_usdt["ask_qty"]
        r1 = (1.0 / p_ask_1) * (1.0 - fee_rate)
        cap1 = q_ask_1 * p_ask_1

        p_bid_2 = alt_btc["bid_price"]
        q_bid_2 = alt_btc["bid_qty"]
        r2 = p_bid_2 * (1.0 - fee_rate)
        cap2 = q_bid_2

        p_bid_3 = btc_usdt["bid_price"]
        q_bid_3 = btc_usdt["bid_qty"]
        r3 = p_bid_3 * (1.0 - fee_rate)
        cap3 = q_bid_3

        # Rates & Yields
        gross_rate = (1.0 / p_ask_1) * p_bid_2 * p_bid_3
        net_rate = r1 * r2 * r3
        gross_yield_pct = (gross_rate - 1.0) * 100.0
        net_yield_pct = (net_rate - 1.0) * 100.0

        # Synthetic vs Direct
        direct_rate = p_ask_1
        synthetic_rate = p_bid_2 * p_bid_3
        discrepancy_pct = ((synthetic_rate - direct_rate) / direct_rate) * 100.0

        # Bottleneck
        v_max, bottleneck_idx = calculate_depth_bottleneck([cap1, cap2, cap3], [r1, r2, r3])
        expected_profit = v_max * (net_yield_pct / 100.0)
        is_arbitrage = net_yield_pct > min_yield_threshold

        bottleneck_symbols = [f"{alt_symbol}USDT", f"{alt_symbol}BTC", "BTCUSDT"]

        legs = [
            ConversionLeg(
                step=1,
                from_asset="USDT",
                to_asset=alt_symbol,
                symbol=f"{alt_symbol}USDT",
                action="BUY",
                price=p_ask_1,
                available_qty_base=q_ask_1,
                taker_fee_rate=fee_rate,
                rate_multiplier=r1,
            ),
            ConversionLeg(
                step=2,
                from_asset=alt_symbol,
                to_asset="BTC",
                symbol=f"{alt_symbol}BTC",
                action="SELL",
                price=p_bid_2,
                available_qty_base=q_bid_2,
                taker_fee_rate=fee_rate,
                rate_multiplier=r2,
            ),
            ConversionLeg(
                step=3,
                from_asset="BTC",
                to_asset="USDT",
                symbol="BTCUSDT",
                action="SELL",
                price=p_bid_3,
                available_qty_base=q_bid_3,
                taker_fee_rate=fee_rate,
                rate_multiplier=r3,
            ),
        ]

        return TriangularCycleOpportunity(
            cycle_id=f"USDT->{alt_symbol}->BTC->USDT",
            path=["USDT", alt_symbol, "BTC", "USDT"],
            legs=legs,
            gross_yield_pct=gross_yield_pct,
            net_yield_pct=net_yield_pct,
            is_arbitrage=is_arbitrage,
            synthetic_rate=synthetic_rate,
            direct_rate=direct_rate,
            discrepancy_pct=discrepancy_pct,
            bottleneck_leg_index=bottleneck_idx,
            bottleneck_symbol=bottleneck_symbols[bottleneck_idx],
            max_executable_volume_quote=v_max,
            expected_net_profit_quote=expected_profit,
            starting_asset="USDT",
            timestamp_ms=now_ms,
        )

    @classmethod
    def evaluate_cycles(
        cls,
        tickers: Dict[str, Any],
        fee_rate: float = DEFAULT_FEE_RATE,
        min_yield_threshold: float = DEFAULT_MIN_YIELD_THRESHOLD,
        active_only: bool = False,
    ) -> List[TriangularCycleOpportunity]:
        """
        Evaluates all 6 core USDT-rooted triangular arbitrage cycles.
        If active_only is True, filters to opportunities where is_arbitrage == True.
        """
        opportunities: List[TriangularCycleOpportunity] = []
        now_ms = int(time.time() * 1000)

        for alt in CORE_ALTS:
            # Check if required pairs exist for this alt
            req_fwd = ["BTCUSDT", f"{alt}BTC", f"{alt}USDT"]
            if all(sym in tickers for sym in req_fwd):
                fwd = cls.evaluate_usdt_btc_alt_cycle(
                    tickers, alt, fee_rate=fee_rate, min_yield_threshold=min_yield_threshold, timestamp_ms=now_ms
                )
                opportunities.append(fwd)

            req_rev = [f"{alt}USDT", f"{alt}BTC", "BTCUSDT"]
            if all(sym in tickers for sym in req_rev):
                rev = cls.evaluate_usdt_alt_btc_cycle(
                    tickers, alt, fee_rate=fee_rate, min_yield_threshold=min_yield_threshold, timestamp_ms=now_ms
                )
                opportunities.append(rev)

        if active_only:
            return [o for o in opportunities if o.is_arbitrage]
        return opportunities
