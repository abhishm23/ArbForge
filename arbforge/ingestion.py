"""
arbforge/ingestion.py
Asynchronous Binance Level-1 bookTicker REST poller with zero-whitespace
parameter serialization, resilient exponential backoff, and graceful lifecycles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from arbforge.models import (
    CrossedBookError,
    EmptyBookError,
    InvalidPayloadError,
    InvalidPriceError,
    Level1Ticker,
)
from arbforge.store import CORE_SYMBOLS, OrderBookStore

logger = logging.getLogger("arbforge.ingestion")

BINANCE_BOOK_TICKER_URL = "https://api.binance.com/api/v3/ticker/bookTicker"


class BinanceBookTickerPoller:
    """
    Asynchronous poller fetching Level-1 top-of-book depth from Binance REST API.
    Updates an OrderBookStore atomically.
    """

    def __init__(
        self,
        store: OrderBookStore,
        poll_interval: float = 2.0,
        symbols: Optional[List[str]] = None,
        base_url: str = BINANCE_BOOK_TICKER_URL,
        mock_mode: bool = False,
    ) -> None:
        self.store: OrderBookStore = store
        self.poll_interval: float = poll_interval
        self.symbols: List[str] = list(symbols or CORE_SYMBOLS)
        self.base_url: str = base_url
        self.mock_mode: bool = mock_mode

        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._backoff: float = 1.0

    # -------------------------------------------------------------------------
    # Lifecycle Management
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """Starts the background polling loop."""
        if self._running:
            logger.warning("Poller is already running.")
            return

        self._running = True
        if not self.mock_mode:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=5.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                headers={"User-Agent": "ArbForge/1.0.0 Quantitative Engine"},
            )

        self._task = asyncio.create_task(self._poll_loop(), name="arbforge-bookticker-poller")
        logger.info(
            "BinanceBookTickerPoller started (interval=%ss, mock=%s).",
            self.poll_interval,
            self.mock_mode,
        )

    async def stop(self) -> None:
        """Gracefully stops the background polling loop and closes the HTTP client."""
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._client:
            await self._client.aclose()
            self._client = None

        logger.info("BinanceBookTickerPoller stopped.")

    # -------------------------------------------------------------------------
    # Core Polling Loop & Single Poll
    # -------------------------------------------------------------------------

    async def poll_once(self) -> List[Level1Ticker]:
        """
        Executes a single fetch-and-update cycle.
        Returns the parsed Level1Ticker list.
        Useful for testing, startup verification, and manual triggers.
        """
        if self.mock_mode:
            return await self._poll_mock()

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=5.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                headers={"User-Agent": "ArbForge/1.0.0 Quantitative Engine"},
            )

        t0 = time.perf_counter()
        # Enforce zero-whitespace JSON parameter serialization
        compact_symbols = json.dumps(self.symbols, separators=(",", ":"))
        params = {"symbols": compact_symbols}

        try:
            resp = await self._client.get(self.base_url, params=params)
            latency_ms = (time.perf_counter() - t0) * 1000.0

            if resp.status_code == 200:
                weight_1m = int(resp.headers.get("x-mbx-used-weight-1m", 0))
                raw_items = resp.json()
                tickers: List[Level1Ticker] = []

                if isinstance(raw_items, dict):
                    raw_items = [raw_items]

                for item in raw_items:
                    sym = item.get("symbol")
                    if not sym or sym not in self.symbols:
                        continue
                    try:
                        ticker = Level1Ticker.from_binance_raw(item, strict=False)
                        tickers.append(ticker)
                    except (InvalidPayloadError, InvalidPriceError, EmptyBookError, CrossedBookError) as parse_err:
                        logger.warning("Dropped invalid ticker %s: %s", sym, parse_err)

                await self.store.update_tickers(
                    tickers=tickers,
                    latency_ms=latency_ms,
                    used_weight_1m=weight_1m,
                )
                self._backoff = 1.0  # Reset backoff on success
                return tickers

            elif resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 10))
                err_msg = f"HTTP 429 Rate Limit Exceeded. Waiting {retry_after}s"
                logger.warning(err_msg)
                await self.store.update_tickers([], latency_ms=latency_ms, error=err_msg)
                await asyncio.sleep(retry_after)
                return []

            elif resp.status_code == 418:
                err_msg = "HTTP 418 IP Ban Detected. Cooling down for 120s"
                logger.critical(err_msg)
                await self.store.update_tickers([], latency_ms=latency_ms, error=err_msg)
                await asyncio.sleep(120.0)
                return []

            else:
                err_msg = f"HTTP {resp.status_code}: {resp.text}"
                logger.error(err_msg)
                await self.store.update_tickers([], latency_ms=latency_ms, error=err_msg)
                self._backoff = min(self._backoff * 2.0, 30.0)
                return []

        except httpx.RequestError as req_err:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            err_msg = f"Network transport error: {req_err}"
            logger.warning(err_msg)
            await self.store.update_tickers([], latency_ms=latency_ms, error=err_msg)
            self._backoff = min(self._backoff * 1.5, 15.0)
            return []

    async def _poll_loop(self) -> None:
        """Continuous background execution loop with exponential backoff on failures."""
        while self._running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Unexpected exception in poller loop: %s", exc)
                await self.store.update_tickers([], error=str(exc))
                self._backoff = min(self._backoff * 1.5, 15.0)

            sleep_duration = max(
                self.poll_interval,
                self._backoff if self._backoff > 1.0 else self.poll_interval,
            )
            try:
                await asyncio.sleep(sleep_duration)
            except asyncio.CancelledError:
                break

    # -------------------------------------------------------------------------
    # Deterministic Mock Provider for Offline CI / Unit Testing
    # -------------------------------------------------------------------------

    async def _poll_mock(self) -> List[Level1Ticker]:
        """Generates realistic synthetic Level-1 ticks without network I/O."""
        from arbforge.mock import SyntheticMarketGenerator

        t0 = time.perf_counter()
        market = SyntheticMarketGenerator.generate_market(scenario="balanced")
        tickers = [market[s] for s in self.symbols if s in market]

        latency_ms = (time.perf_counter() - t0) * 1000.0
        await self.store.update_tickers(
            tickers=tickers,
            latency_ms=latency_ms,
            used_weight_1m=4,
        )
        return tickers


# Backward compatibility alias
BinanceIngestionService = BinanceBookTickerPoller
