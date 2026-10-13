"""
arbforge/models.py
Authoritative Pydantic V2 data models, validation guards, and spread normalization for ArbForge.
Matches specifications in ORIGINAL_REQUEST.md and PROJECT.md.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============================================================================
# Exception Hierarchy
# ============================================================================

class OrderBookError(Exception):
    """Base exception for orderbook and normalization errors."""
    pass


class InvalidPayloadError(OrderBookError):
    """Raised when the raw payload is missing keys or has unexpected format."""
    pass


class InvalidPriceError(OrderBookError):
    """Raised when price is non-positive, NaN, infinite, or unparseable."""
    pass


class InvalidQuantityError(OrderBookError):
    """Raised when quantity is negative, NaN, infinite, or unparseable."""
    pass


class EmptyBookError(OrderBookError):
    """Raised when a book has zero bids or asks (zero price or zero quantity)."""
    pass


class CrossedBookError(OrderBookError):
    """Raised when orderbook is crossed (bid_price > ask_price)."""
    pass


class LockedBookError(OrderBookError):
    """Raised when orderbook is locked (bid_price == ask_price) under strict validation."""
    pass


# ============================================================================
# Mathematical & Parsing Helpers
# ============================================================================

KNOWN_PAIRS: Dict[str, Tuple[str, str]] = {
    "BTCUSDT": ("BTC", "USDT"),
    "ETHUSDT": ("ETH", "USDT"),
    "ETHBTC": ("ETH", "BTC"),
    "SOLUSDT": ("SOL", "USDT"),
    "SOLBTC": ("SOL", "BTC"),
    "BNBUSDT": ("BNB", "USDT"),
    "BNBBTC": ("BNB", "BTC"),
}


def parse_symbol_pair(symbol: str) -> Tuple[str, str]:
    """
    Extracts (base_asset, quote_asset) from a trading pair symbol.
    Supports canonical crypto pairs and general heuristics.
    """
    sym = symbol.strip().upper()
    if sym in KNOWN_PAIRS:
        return KNOWN_PAIRS[sym]

    for quote in ("USDT", "BTC", "ETH", "BNB", "USDC", "USD", "EUR"):
        if sym.endswith(quote) and len(sym) > len(quote):
            return sym[:-len(quote)], quote

    return sym, "UNKNOWN"


def parse_safe_float(val: Any, field_name: str) -> float:
    """
    Safely converts an input string/number to a finite float.
    Rejects None, empty strings, whitespace, NaN, +Inf, -Inf, and unparseable values.
    """
    if val is None:
        raise InvalidPayloadError(f"Field '{field_name}' cannot be None.")

    if isinstance(val, str):
        val = val.strip()
        if not val:
            raise InvalidPayloadError(f"Field '{field_name}' cannot be an empty string.")

    try:
        num = float(val)
    except (ValueError, TypeError) as exc:
        raise InvalidPayloadError(
            f"Field '{field_name}' could not be converted to float: {val!r}"
        ) from exc

    if not math.isfinite(num):
        raise InvalidPriceError(
            f"Field '{field_name}' must be a finite real number, got {num!r}."
        )

    return num


def parse_safe_price(val: Any, field_name: str) -> float:
    """Parses and validates that a price is finite and strictly positive (> 0)."""
    num = parse_safe_float(val, field_name)
    if num <= 0.0:
        raise InvalidPriceError(
            f"Field '{field_name}' must be strictly positive (> 0), got {num}."
        )
    return num


def parse_safe_quantity(val: Any, field_name: str, allow_zero: bool = False) -> float:
    """Parses and validates that a quantity is finite and non-negative."""
    num = parse_safe_float(val, field_name)
    if allow_zero:
        if num < 0.0:
            raise InvalidQuantityError(
                f"Field '{field_name}' cannot be negative, got {num}."
            )
    else:
        if num <= 0.0:
            raise EmptyBookError(
                f"Field '{field_name}' must be strictly positive (> 0), got {num}."
            )
    return num


def calculate_spreads(
    bid_price: float,
    ask_price: float,
    round_output: bool = True,
) -> Tuple[float, float, float]:
    """
    Calculates absolute spread and basis points spreads (mid and bid relative).

    Formulas:
        spread_abs = ask_price - bid_price
        mid_price = (ask_price + bid_price) / 2.0
        spread_bps_mid = ((ask_price - bid_price) / mid_price) * 10000.0
        spread_bps_bid = ((ask_price - bid_price) / bid_price) * 10000.0

    Returns:
        Tuple of (spread_abs, spread_bps_mid, spread_bps_bid)
    """
    if not math.isfinite(bid_price) or bid_price <= 0.0:
        raise InvalidPriceError(f"Cannot calculate spread: bid_price must be finite > 0, got {bid_price}.")
    if not math.isfinite(ask_price) or ask_price <= 0.0:
        raise InvalidPriceError(f"Cannot calculate spread: ask_price must be finite > 0, got {ask_price}.")

    spread_abs = ask_price - bid_price
    mid_price = (ask_price + bid_price) / 2.0

    if mid_price <= 0.0:
        raise InvalidPriceError(f"Mid-market price must be > 0, got {mid_price}.")

    spread_bps_mid = (spread_abs / mid_price) * 10000.0
    spread_bps_bid = (spread_abs / bid_price) * 10000.0

    if round_output:
        return (
            round(spread_abs, 8),
            round(spread_bps_mid, 5),
            round(spread_bps_bid, 5),
        )
    return spread_abs, spread_bps_mid, spread_bps_bid


# ============================================================================
# Pydantic Models Matching Master Contract (.agents/PROJECT.md)
# ============================================================================

class Level1Ticker(BaseModel):
    """
    Normalized Level-1 Top-of-Book orderbook ticker.
    Matches the master contract in .agents/PROJECT.md.
    """
    model_config = ConfigDict(populate_by_name=True)

    symbol: str = Field(..., description="Trading pair symbol (e.g. BTCUSDT)")
    bid_price: float = Field(..., description="Best bid price resting on top-of-book")
    bid_qty: float = Field(..., description="Available quantity at best bid price")
    ask_price: float = Field(..., description="Best ask price resting on top-of-book")
    ask_qty: float = Field(..., description="Available quantity at best ask price")
    spread_abs: float = Field(..., description="Absolute spread: ask_price - bid_price")
    spread_bps_mid: float = Field(..., description="Spread in basis points relative to mid price")
    spread_bps_bid: float = Field(..., description="Spread in basis points relative to bid price")
    timestamp_ms: int = Field(..., description="Local ingestion epoch timestamp in milliseconds")
    iso_time: str = Field(..., description="ISO 8601 UTC timestamp string")

    # Optional metadata fields for downstream engine convenience
    base_asset: Optional[str] = Field(None, description="Base asset symbol (e.g. BTC)")
    quote_asset: Optional[str] = Field(None, description="Quote asset symbol (e.g. USDT)")
    mid_price: Optional[float] = Field(None, description="Mid-market price (ask + bid) / 2.0")
    spread_bps: Optional[float] = Field(None, description="Alias pointing to spread_bps_mid")
    status: str = Field("ACTIVE", description="Health status: ACTIVE, CROSSED, LOCKED, EMPTY_BOOK, SIMULATED")

    @field_validator("bid_price", "ask_price")
    @classmethod
    def validate_positive_prices(cls, v: float, info) -> float:
        if not math.isfinite(v) or v <= 0.0:
            raise ValueError(f"Field '{info.field_name}' must be a finite positive number, got {v}.")
        return v

    @field_validator("bid_qty", "ask_qty")
    @classmethod
    def validate_non_negative_quantities(cls, v: float, info) -> float:
        if not math.isfinite(v) or v < 0.0:
            raise ValueError(f"Field '{info.field_name}' must be a finite non-negative number, got {v}.")
        return v

    @model_validator(mode="after")
    def validate_book_sanity(self) -> "Level1Ticker":
        """Crossed book and status validation."""
        # Set spread_bps alias if not set
        if self.spread_bps is None:
            self.spread_bps = self.spread_bps_mid

        # Populate base/quote assets if missing
        if not self.base_asset or not self.quote_asset:
            b, q = parse_symbol_pair(self.symbol)
            if not self.base_asset:
                self.base_asset = b
            if not self.quote_asset:
                self.quote_asset = q

        # Populate mid_price if missing
        if self.mid_price is None and self.bid_price > 0 and self.ask_price > 0:
            self.mid_price = round((self.ask_price + self.bid_price) / 2.0, 8)

        # Check crossed book condition
        if self.bid_price > self.ask_price:
            if self.status not in ("CROSSED", "SIMULATED"):
                raise ValueError(
                    f"Crossed orderbook detected for {self.symbol}: "
                    f"bid_price ({self.bid_price}) > ask_price ({self.ask_price})."
                )
        return self

    @classmethod
    def from_raw(
        cls,
        symbol: str,
        bid_price: Any,
        bid_qty: Any,
        ask_price: Any,
        ask_qty: Any,
        timestamp_ms: Optional[int] = None,
        iso_time: Optional[str] = None,
        base_asset: Optional[str] = None,
        quote_asset: Optional[str] = None,
        status: Optional[str] = None,
        strict: bool = False,
    ) -> "Level1Ticker":
        """
        Factory method to construct a Level1Ticker from numeric or string inputs,
        validating numeric bounds, NaN/Inf checks, and computing spreads automatically.
        """
        sym = str(symbol).strip().upper()
        if not sym:
            raise InvalidPayloadError("Symbol cannot be empty.")

        p_bid = parse_safe_price(bid_price, "bid_price")
        p_ask = parse_safe_price(ask_price, "ask_price")
        q_bid = parse_safe_quantity(bid_qty, "bid_qty", allow_zero=not strict)
        q_ask = parse_safe_quantity(ask_qty, "ask_qty", allow_zero=not strict)

        # Status & Crossed book detection
        resolved_status = status or "ACTIVE"
        if p_bid > p_ask:
            if strict:
                raise CrossedBookError(
                    f"Crossed orderbook detected for {sym}: bid ({p_bid}) > ask ({p_ask})."
                )
            resolved_status = "CROSSED"
        elif p_bid == p_ask:
            resolved_status = "LOCKED"
        elif q_bid <= 0.0 or q_ask <= 0.0:
            resolved_status = "EMPTY_BOOK"

        spread_abs = round(p_ask - p_bid, 8)
        mid = (p_ask + p_bid) / 2.0

        if mid > 0:
            spread_bps_mid = round(((p_ask - p_bid) / mid) * 10000.0, 5)
        else:
            spread_bps_mid = 0.0

        if p_bid > 0:
            spread_bps_bid = round(((p_ask - p_bid) / p_bid) * 10000.0, 5)
        else:
            spread_bps_bid = 0.0

        now = time.time()
        ts_ms = timestamp_ms if timestamp_ms is not None else int(now * 1000)
        ts_iso = (
            iso_time
            if iso_time is not None
            else datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()
        )

        inferred_base, inferred_quote = parse_symbol_pair(sym)
        b_asset = base_asset or inferred_base
        q_asset = quote_asset or inferred_quote

        return cls(
            symbol=sym,
            bid_price=p_bid,
            bid_qty=q_bid,
            ask_price=p_ask,
            ask_qty=q_ask,
            spread_abs=spread_abs,
            spread_bps_mid=spread_bps_mid,
            spread_bps_bid=spread_bps_bid,
            spread_bps=spread_bps_mid,
            timestamp_ms=ts_ms,
            iso_time=ts_iso,
            base_asset=b_asset,
            quote_asset=q_asset,
            mid_price=round(mid, 8),
            status=resolved_status,
        )

    @classmethod
    def from_binance_raw(
        cls,
        raw: Dict[str, Any],
        timestamp_ms: Optional[int] = None,
        iso_time: Optional[str] = None,
        strict: bool = True,
    ) -> "Level1Ticker":
        """
        Constructs and normalizes a Level1Ticker from a raw Binance bookTicker dictionary.
        Keys expected: 'symbol', 'bidPrice', 'bidQty', 'askPrice', 'askQty'.
        """
        if not isinstance(raw, dict):
            raise InvalidPayloadError(f"Expected dict for raw ticker, got {type(raw).__name__}.")

        symbol = raw.get("symbol")
        if not symbol:
            raise InvalidPayloadError("Missing or empty 'symbol' in raw bookTicker payload.")

        return cls.from_raw(
            symbol=str(symbol),
            bid_price=raw.get("bidPrice"),
            bid_qty=raw.get("bidQty"),
            ask_price=raw.get("askPrice"),
            ask_qty=raw.get("askQty"),
            timestamp_ms=timestamp_ms,
            iso_time=iso_time,
            strict=strict,
        )


class OrderBookMatrixResponse(BaseModel):
    """
    Response model for GET /api/orderbook-matrix.
    Matches the master contract in .agents/PROJECT.md.
    """
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(..., description="System health status: HEALTHY, DEGRADED, STALE, INITIALIZING")
    timestamp_ms: int = Field(..., description="Server timestamp in epoch milliseconds")
    pairs: List[Level1Ticker] = Field(..., description="Ordered list of normalized Level-1 tickers")
    matrix: Dict[str, Level1Ticker] = Field(..., description="Symbol lookup map for O(1) access")

    # Diagnostic & convenience metadata fields
    pairs_count: Optional[int] = Field(None, description="Count of returned trading pairs")
    iso_time: Optional[str] = Field(None, description="ISO 8601 UTC timestamp string")
    latency_ms: Optional[float] = Field(None, description="Last polling latency in milliseconds")
    poll_count: Optional[int] = Field(None, description="Total successful polls since startup")
    error_count: Optional[int] = Field(None, description="Total polling errors encountered")
    last_error: Optional[str] = Field(None, description="Most recent error message if any")
    used_weight_1m: Optional[int] = Field(None, description="Binance used request weight in last minute")

    @model_validator(mode="after")
    def populate_defaults(self) -> "OrderBookMatrixResponse":
        if self.pairs_count is None:
            self.pairs_count = len(self.pairs)
        if self.iso_time is None:
            if self.timestamp_ms > 0:
                self.iso_time = datetime.fromtimestamp(
                    self.timestamp_ms / 1000.0, tz=timezone.utc
                ).isoformat()
            else:
                self.iso_time = datetime.now(timezone.utc).isoformat()
        return self


class ConversionLeg(BaseModel):
    """
    Individual execution leg in a triangular conversion cycle.
    Matches the master contract in .agents/PROJECT.md.
    """
    model_config = ConfigDict(populate_by_name=True)

    step: int = Field(..., description="Leg sequence number (1, 2, or 3)")
    from_asset: str = Field(..., description="Input asset for this step (e.g. USDT)")
    to_asset: str = Field(..., description="Output asset for this step (e.g. BTC)")
    symbol: str = Field(..., description="Market pair executed (e.g. BTCUSDT)")
    action: str = Field(..., description="Directional execution: BUY (at Ask) or SELL (at Bid)")
    price: float = Field(..., description="Execution price (ask_price for BUY, bid_price for SELL)")
    available_qty_base: float = Field(..., description="Available depth in base asset on Level-1 book")
    taker_fee_rate: float = Field(0.001, description="Taker fee fraction (default 0.001 = 0.10%)")
    rate_multiplier: float = Field(..., description="Effective conversion multiplier after fee deduction")


class TriangularCycleOpportunity(BaseModel):
    """
    Evaluated triangular arbitrage cycle path with bottleneck depth and net profit.
    Matches the master contract in .agents/PROJECT.md.
    """
    model_config = ConfigDict(populate_by_name=True)

    cycle_id: str = Field(..., description="Deterministic cycle identifier, e.g. USDT->BTC->ETH->USDT")
    path: List[str] = Field(..., description="Ordered asset path: ['USDT', 'BTC', 'ETH', 'USDT']")
    legs: List[ConversionLeg] = Field(..., description="Ordered conversion legs")
    gross_yield_pct: float = Field(..., description="Gross yield % before fees: ((Q_gross - Q_0)/Q_0)*100")
    net_yield_pct: float = Field(..., description="Net yield % after compound taker fees: ((Q_net - Q_0)/Q_0)*100")
    is_arbitrage: bool = Field(..., description="True if net_yield_pct strictly exceeds threshold (e.g. > 0.05%)")
    synthetic_rate: float = Field(..., description="Synthetic cross-rate evaluated via intermediate legs")
    direct_rate: float = Field(..., description="Direct market rate for the closing leg")
    discrepancy_pct: float = Field(..., description="Synthetic vs direct discrepancy percentage")
    bottleneck_leg_index: int = Field(..., description="0-indexed leg representing the tightest volume bottleneck")
    bottleneck_symbol: str = Field(..., description="Market pair symbol of the bottleneck leg")
    max_executable_volume_quote: float = Field(..., description="Maximum executable initial capital in quote asset")
    expected_net_profit_quote: float = Field(..., description="Expected net profit in quote currency: max_volume * (net_yield / 100)")
    starting_asset: str = Field(..., description="Starting and closing currency (e.g. USDT)")
    timestamp_ms: int = Field(..., description="Evaluation timestamp in milliseconds")


class TriangularCyclesResponse(BaseModel):
    """
    Response model for GET /api/triangular-cycles.
    Matches the master contract in .agents/PROJECT.md.
    """
    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(..., description="System health status: HEALTHY, DEGRADED, STALE")
    timestamp_ms: int = Field(..., description="Evaluation timestamp in epoch milliseconds")
    opportunities: List[TriangularCycleOpportunity] = Field(..., description="List of all evaluated cycles")
    active_count: int = Field(..., description="Count of cycles crossing the arbitrage threshold")
    scanned_count: int = Field(..., description="Total cycles scanned")
