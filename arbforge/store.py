"""
arbforge/store.py
High-performance, thread-safe and asyncio-safe in-memory orderbook store
utilizing the Atomic Snapshot Reference Swap pattern.
Guarantees lockless O(1) reads for high-frequency readers and serializes writes.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from arbforge.models import Level1Ticker, OrderBookMatrixResponse

CORE_SYMBOLS: List[str] = [
    "BTCUSDT",
    "ETHUSDT",
    "ETHBTC",
    "SOLUSDT",
    "SOLBTC",
    "BNBUSDT",
    "BNBBTC",
]


class OrderBookStore:
    """
    In-memory store providing atomic snapshot reference swap.
    Guarantees non-blocking lockless reads for concurrent readers
    and serializes updates under asyncio.Lock.
    """

    def __init__(self, core_symbols: Optional[List[str]] = None) -> None:
        self._core_symbols: List[str] = list(core_symbols or CORE_SYMBOLS)
        self._lock: asyncio.Lock = asyncio.Lock()
        self._tickers: Dict[str, Level1Ticker] = {}
        self._last_updated: float = 0.0

        # Telemetry & Diagnostics
        self._poll_count: int = 0
        self._error_count: int = 0
        self._last_error: Optional[str] = None
        self._last_latency_ms: float = 0.0
        self._used_weight_1m: int = 0

    # -------------------------------------------------------------------------
    # Write Methods (Serialized by asyncio.Lock)
    # -------------------------------------------------------------------------

    async def update_tickers(
        self,
        tickers: List[Level1Ticker],
        latency_ms: float = 0.0,
        used_weight_1m: int = 0,
        error: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> None:
        """
        Atomically updates tickers in the in-memory store.
        Performs a full snapshot copy, updates values, and performs an atomic pointer swap.
        """
        async with self._lock:
            now = timestamp if timestamp is not None else time.time()

            new_map = dict(self._tickers)
            for ticker in tickers:
                new_map[ticker.symbol.upper()] = ticker

            # Atomic reference swap
            self._tickers = new_map

            if tickers:
                self._last_updated = now

            self._last_latency_ms = round(latency_ms, 2)
            self._used_weight_1m = used_weight_1m
            self._poll_count += 1

            if error:
                self._error_count += 1
                self._last_error = error
            else:
                self._last_error = None

    async def update(self, tickers: List[Any], **kwargs: Any) -> None:
        """
        Generic update method for backward compatibility with PROJECT.md.
        Accepts Level1Ticker objects or raw dicts.
        """
        converted: List[Level1Ticker] = []
        for item in tickers:
            if isinstance(item, Level1Ticker):
                converted.append(item)
            elif isinstance(item, dict):
                if "bidPrice" in item:
                    converted.append(Level1Ticker.from_binance_raw(item, strict=False))
                elif "bid_price" in item:
                    converted.append(Level1Ticker.from_raw(
                        symbol=item["symbol"],
                        bid_price=item["bid_price"],
                        bid_qty=item["bid_qty"],
                        ask_price=item["ask_price"],
                        ask_qty=item["ask_qty"],
                        timestamp_ms=item.get("timestamp_ms"),
                    ))
        await self.update_tickers(converted, **kwargs)

    def update_tickers_sync(
        self,
        tickers: List[Level1Ticker],
        latency_ms: float = 0.0,
        used_weight_1m: int = 0,
        error: Optional[str] = None,
        update_time_ms: Optional[int] = None,
    ) -> None:
        """
        Synchronous update helper for offline tests and fixture seeding.
        Performs the same atomic reference swap synchronously.
        """
        now = (update_time_ms / 1000.0) if update_time_ms is not None else time.time()

        new_map = dict(self._tickers)
        for ticker in tickers:
            new_map[ticker.symbol.upper()] = ticker

        self._tickers = new_map

        if tickers:
            self._last_updated = now

        self._last_latency_ms = round(latency_ms, 2)
        self._used_weight_1m = used_weight_1m
        self._poll_count += 1

        if error:
            self._error_count += 1
            self._last_error = error
        else:
            self._last_error = None

    # -------------------------------------------------------------------------
    # Read Methods (Completely Lockless O(1) Access)
    # -------------------------------------------------------------------------

    def get_all(self, ordered: bool = True) -> List[Level1Ticker]:
        """
        Lockless atomic snapshot read of all tickers.
        If ordered is True, returns tickers ordered deterministically by core_symbols first.
        """
        current_map = self._tickers

        if not ordered:
            return list(current_map.values())

        ordered_list: List[Level1Ticker] = []
        for sym in self._core_symbols:
            if sym in current_map:
                ordered_list.append(current_map[sym])

        for sym, ticker in current_map.items():
            if sym not in self._core_symbols:
                ordered_list.append(ticker)

        return ordered_list

    def get_ticker(self, symbol: str) -> Optional[Level1Ticker]:
        """Lockless O(1) lookup of a single ticker by symbol."""
        return self._tickers.get(symbol.upper())

    def get(self, symbol: str) -> Optional[Level1Ticker]:
        """Alias for get_ticker to adhere to PROJECT.md interface contract."""
        return self.get_ticker(symbol)

    def get_matrix(self) -> Dict[str, Level1Ticker]:
        """Lockless shallow copy of the symbol -> ticker lookup dictionary."""
        return dict(self._tickers)

    def get_snapshot(self) -> Tuple[Dict[str, Level1Ticker], Dict[str, Any]]:
        """Returns snapshot map and diagnostic telemetry dictionary."""
        return dict(self._tickers), self.get_telemetry()

    def is_stale(
        self,
        max_age_sec: Optional[float] = None,
        max_age_ms: Optional[int] = None,
    ) -> bool:
        """
        Returns True if the orderbook has not been updated within the maximum age threshold.
        Supports both max_age_sec (float) and max_age_ms (int). Default threshold: 5.0 seconds.
        """
        if self._last_updated <= 0.0:
            return True

        if max_age_ms is not None:
            threshold_sec = max_age_ms / 1000.0
        elif max_age_sec is not None:
            threshold_sec = max_age_sec
        else:
            threshold_sec = 5.0

        return (time.time() - self._last_updated) > threshold_sec

    def get_status(self, max_age_sec: float = 5.0) -> str:
        """
        Computes the current system health status:
        - "INITIALIZING": No data received yet.
        - "STALE": Data age exceeds max_age_sec.
        - "DEGRADED": Missing core pairs, anomaly status (CROSSED/EMPTY_BOOK), or recent errors.
        - "HEALTHY": Fresh data received, all core pairs present, uncrossed books.
        """
        if self._last_updated <= 0.0 or not self._tickers:
            return "INITIALIZING"

        if self.is_stale(max_age_sec=max_age_sec):
            return "STALE"

        if self._last_error is not None:
            return "DEGRADED"

        # Check core symbols presence
        for sym in self._core_symbols:
            if sym not in self._tickers:
                return "DEGRADED"

        # Check ticker sanity
        for ticker in self._tickers.values():
            if ticker.status in ("CROSSED", "EMPTY_BOOK") or ticker.bid_price >= ticker.ask_price:
                return "DEGRADED"

        return "HEALTHY"

    def get_matrix_response(self, max_age_sec: float = 5.0) -> OrderBookMatrixResponse:
        """Constructs an OrderBookMatrixResponse directly from the current atomic snapshot."""
        current_map = self._tickers
        now = time.time()
        ts_ms = int(self._last_updated * 1000) if self._last_updated > 0 else int(now * 1000)
        iso_time = (
            datetime.fromtimestamp(self._last_updated, tz=timezone.utc).isoformat()
            if self._last_updated > 0
            else datetime.now(timezone.utc).isoformat()
        )
        pairs = self.get_all(ordered=True)

        return OrderBookMatrixResponse(
            status=self.get_status(max_age_sec=max_age_sec),
            timestamp_ms=ts_ms,
            pairs=pairs,
            matrix=dict(current_map),
            pairs_count=len(pairs),
            iso_time=iso_time,
            latency_ms=self._last_latency_ms,
            poll_count=self._poll_count,
            error_count=self._error_count,
            last_error=self._last_error,
            used_weight_1m=self._used_weight_1m,
        )

    def get_telemetry(self) -> Dict[str, Any]:
        """Returns diagnostic telemetry dictionary."""
        return {
            "last_updated": self._last_updated,
            "poll_count": self._poll_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "last_latency_ms": self._last_latency_ms,
            "latency_ms": self._last_latency_ms,
            "used_weight_1m": self._used_weight_1m,
            "pair_count": len(self._tickers),
        }

    def reset(self) -> None:
        """Reset the store state for test suites."""
        self._tickers = {}
        self._last_updated = 0.0
        self._poll_count = 0
        self._error_count = 0
        self._last_error = None
        self._last_latency_ms = 0.0
        self._used_weight_1m = 0
