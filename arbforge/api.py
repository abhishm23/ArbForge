"""
arbforge/api.py
FastAPI REST API router exposing the live normalized Level-1 orderbook matrix.
Provides dependency injection, anti-caching headers, and deterministic canonical ordering.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from arbforge.engine import (
    ArbitrageEngine,
    DEFAULT_FEE_RATE,
    DEFAULT_MIN_YIELD_THRESHOLD,
)
from arbforge.models import (
    Level1Ticker,
    OrderBookMatrixResponse,
    TriangularCycleOpportunity,
    TriangularCyclesResponse,
)
from arbforge.store import CORE_SYMBOLS, OrderBookStore

router = APIRouter(tags=["Market Data"])

CANONICAL_PAIRS: List[str] = list(CORE_SYMBOLS)

_default_store: Optional[OrderBookStore] = None


def get_default_store() -> OrderBookStore:
    """Returns or lazily creates a default store singleton."""
    global _default_store
    if _default_store is None:
        _default_store = OrderBookStore()
    return _default_store


def get_orderbook_store(request: Request) -> OrderBookStore:
    """
    FastAPI dependency retrieving the OrderBookStore from request.app.state.
    Falls back to default store if not configured on application state.
    Enables clean unit test mocking via app.dependency_overrides[get_orderbook_store].
    """
    if hasattr(request.app.state, "store") and request.app.state.store is not None:
        return request.app.state.store
    return get_default_store()


def evaluate_system_health(
    snapshot: Dict[str, Level1Ticker],
    metadata: Dict[str, Any],
    is_stale: bool,
    core_symbols: Optional[List[str]] = None,
) -> str:
    """
    Evaluates system status based on snapshot completeness, staleness, and error states.
    Returns: 'HEALTHY' | 'DEGRADED' | 'STALE' | 'INITIALIZING'
    """
    if not snapshot:
        return "INITIALIZING"

    if is_stale:
        return "STALE"

    symbols = core_symbols or CANONICAL_PAIRS
    missing_pairs = set(symbols) - set(snapshot.keys())
    if missing_pairs:
        return "DEGRADED"

    for ticker in snapshot.values():
        if ticker.status in ("CROSSED", "EMPTY_BOOK") or ticker.bid_price >= ticker.ask_price:
            return "DEGRADED"

    if metadata.get("error_count", 0) > 0 and metadata.get("last_error"):
        return "DEGRADED"

    return "HEALTHY"


@router.get(
    "/orderbook-matrix",
    response_model=OrderBookMatrixResponse,
    summary="Get Level-1 Orderbook Matrix",
    description="Returns live Level-1 top-of-book market depth for core pairs, calculated spreads, and system health.",
)
async def get_orderbook_matrix(
    response: Response,
    store: OrderBookStore = Depends(get_orderbook_store),
) -> OrderBookMatrixResponse:
    """
    Exposes the current in-memory normalized Level-1 market data snapshot.
    Executes in O(1) lockless time.
    """
    # Enforce strict anti-caching HTTP headers for financial market data
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    now_epoch = time.time()
    now_ms = int(now_epoch * 1000)
    now_iso = datetime.now(timezone.utc).isoformat()

    snapshot, metadata = store.get_snapshot()
    is_stale = store.is_stale(max_age_ms=5000)
    system_status = evaluate_system_health(snapshot, metadata, is_stale, CANONICAL_PAIRS)

    # Deterministic canonical pair list
    pairs_list: List[Level1Ticker] = []
    for sym in CANONICAL_PAIRS:
        if sym in snapshot:
            pairs_list.append(snapshot[sym])

    for sym, ticker in snapshot.items():
        if sym not in CANONICAL_PAIRS:
            pairs_list.append(ticker)

    return OrderBookMatrixResponse(
        status=system_status,
        timestamp_ms=now_ms,
        iso_time=now_iso,
        pairs_count=len(pairs_list),
        pairs=pairs_list,
        matrix=snapshot,
        latency_ms=metadata.get("latency_ms"),
        poll_count=metadata.get("poll_count"),
        error_count=metadata.get("error_count"),
        last_error=metadata.get("last_error"),
        used_weight_1m=metadata.get("used_weight_1m"),
    )


@router.get(
    "/triangular-cycles",
    response_model=TriangularCyclesResponse,
    summary="Get Triangular Arbitrage Cycles",
    description=(
        "Returns evaluated closed triangular arbitrage cycles, execution legs, "
        "synthetic exchange rates, net yields, and Level-1 depth bottleneck constraints."
    ),
    tags=["Arbitrage", "Market Data"],
)
async def get_triangular_cycles(
    response: Response,
    active_only: bool = Query(
        default=False,
        description="Filter for active arbitrage opportunities only (is_arbitrage == True).",
    ),
    fee: Optional[float] = Query(
        default=None,
        description="Custom taker fee rate override (fraction, e.g. 0.0010 for 10 bps). Default: 0.0010.",
        ge=0.0,
        le=1.0,
    ),
    fee_bps: Optional[float] = Query(
        default=None,
        description="Custom taker fee rate in basis points (e.g. 10.0 for 10 bps). Overrides 'fee' if provided.",
        ge=0.0,
    ),
    min_yield: Optional[float] = Query(
        default=None,
        description="Custom net yield percentage threshold override (e.g. 0.05 for 0.05%). Default: 0.05.",
    ),
    store: OrderBookStore = Depends(get_orderbook_store),
) -> TriangularCyclesResponse:
    """
    Exposes evaluated triangular arbitrage cycles evaluated from the live OrderBookStore snapshot.
    Executes in lockless non-blocking time.
    """
    # Enforce strict anti-caching HTTP headers for financial market data
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    now_ms = int(time.time() * 1000)

    # Determine effective fee rate and yield threshold
    if fee_bps is not None:
        effective_fee: float = fee_bps / 10000.0
    elif fee is not None:
        effective_fee = float(fee)
    else:
        effective_fee = DEFAULT_FEE_RATE

    effective_min_yield: float = (
        float(min_yield) if min_yield is not None else DEFAULT_MIN_YIELD_THRESHOLD
    )

    # Lockless atomic snapshot retrieval from OrderBookStore
    matrix = store.get_matrix()
    snapshot, metadata = store.get_snapshot()
    is_stale = store.is_stale(max_age_ms=5000)
    system_status = evaluate_system_health(snapshot, metadata, is_stale, CANONICAL_PAIRS)

    if not matrix or ArbitrageEngine is None:
        return TriangularCyclesResponse(
            status=system_status,
            timestamp_ms=now_ms,
            opportunities=[],
            active_count=0,
            scanned_count=0,
        )

    all_opportunities: List[TriangularCycleOpportunity] = ArbitrageEngine.evaluate_cycles(
        tickers=matrix,
        fee_rate=effective_fee,
        min_yield_threshold=effective_min_yield,
    )

    scanned_count = len(all_opportunities)
    active_count = sum(1 for opp in all_opportunities if opp.is_arbitrage)

    if active_only:
        filtered_opportunities = [opp for opp in all_opportunities if opp.is_arbitrage]
    else:
        filtered_opportunities = all_opportunities

    return TriangularCyclesResponse(
        status=system_status,
        timestamp_ms=now_ms,
        opportunities=filtered_opportunities,
        active_count=active_count,
        scanned_count=scanned_count,
    )
