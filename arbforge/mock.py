"""
arbforge/mock.py
Fast, deterministic synthetic ticker generator and mock ingestion service for ArbForge.
Provides hermetic test fixtures, realistic multi-scenario market states, and offline simulation.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from arbforge.models import Level1Ticker
from arbforge.store import CORE_SYMBOLS, OrderBookStore

ASSET_PAIRS_MAP: Dict[str, Tuple[str, str]] = {
    "BTCUSDT": ("BTC", "USDT"),
    "ETHUSDT": ("ETH", "USDT"),
    "ETHBTC": ("ETH", "BTC"),
    "SOLUSDT": ("SOL", "USDT"),
    "SOLBTC": ("SOL", "BTC"),
    "BNBUSDT": ("BNB", "USDT"),
    "BNBBTC": ("BNB", "BTC"),
}

# Baseline realistic equilibrium orderbook (tight non-arbitrage market depth)
BASE_MARKET: Dict[str, Dict[str, float]] = {
    "BTCUSDT": {"bid": 80000.00, "ask": 80001.00, "bid_qty": 4.500, "ask_qty": 3.200},
    "ETHUSDT": {"bid": 2400.00, "ask": 2400.20, "bid_qty": 25.000, "ask_qty": 18.000},
    "ETHBTC":  {"bid": 0.030000, "ask": 0.030005, "bid_qty": 15.000, "ask_qty": 12.000},
    "SOLUSDT": {"bid": 150.00, "ask": 150.02, "bid_qty": 200.000, "ask_qty": 180.000},
    "SOLBTC":  {"bid": 0.0018750, "ask": 0.0018752, "bid_qty": 35.000, "ask_qty": 40.000},
    "BNBUSDT": {"bid": 600.00, "ask": 600.10, "bid_qty": 20.000, "ask_qty": 15.000},
    "BNBBTC":  {"bid": 0.007500, "ask": 0.007501, "bid_qty": 8.000, "ask_qty": 10.000},
}


def create_level1_ticker(
    symbol: str,
    bid_price: float,
    ask_price: float,
    bid_qty: float,
    ask_qty: float,
    timestamp_ms: Optional[int] = None,
    status: str = "ACTIVE",
) -> Level1Ticker:
    """Constructs a fully normalized Level1Ticker with calculated spreads and timestamps."""
    now_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    iso_time = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).isoformat()
    base, quote = ASSET_PAIRS_MAP.get(symbol, (symbol[:3], symbol[3:]))

    spread_abs = round(ask_price - bid_price, 8)
    mid_price = (ask_price + bid_price) / 2.0

    spread_bps_mid = round(((ask_price - bid_price) / mid_price * 10000.0), 5) if mid_price > 0 else 0.0
    spread_bps_bid = round(((ask_price - bid_price) / bid_price * 10000.0), 5) if bid_price > 0 else 0.0

    return Level1Ticker(
        symbol=symbol,
        base_asset=base,
        quote_asset=quote,
        bid_price=round(bid_price, 8),
        bid_qty=round(bid_qty, 8),
        ask_price=round(ask_price, 8),
        ask_qty=round(ask_qty, 8),
        mid_price=round(mid_price, 8),
        spread_abs=spread_abs,
        spread_bps_mid=spread_bps_mid,
        spread_bps_bid=spread_bps_bid,
        spread_bps=spread_bps_mid,
        timestamp_ms=now_ms,
        iso_time=iso_time,
        status=status,
    )


class SyntheticMarketGenerator:
    """
    Generates deterministic synthetic orderbook market states for tests and simulation.
    """

    @staticmethod
    def generate_market(
        scenario: str = "balanced",
        timestamp_ms: Optional[int] = None,
        jitter_pct: float = 0.0,
    ) -> Dict[str, Level1Ticker]:
        """
        Generates a dictionary of Level1Tickers for the given scenario.
        Scenarios:
          - 'balanced': Equilibrium market depth, tight spreads, no arb.
          - 'arbitrage_eth': Depressed ETHBTC ask creating ~+1.7% net arb yield (>0.05%).
          - 'arbitrage_sol': Depressed SOLBTC ask creating ~+1.2% net arb yield.
          - 'arbitrage_bnb': Depressed BNBBTC ask creating ~+1.1% net arb yield.
          - 'sub_threshold': Subtle price skew yielding +0.02% net (below 0.05% threshold).
          - 'crossed_book': BTCUSDT bid > ask anomaly.
          - 'locked_book': ETHUSDT bid == ask anomaly.
          - 'missing_pair': Drops BNBBTC (6 pairs returned).
          - 'stale': All tickers timestamped 30 seconds in the past.
        """
        now_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        if scenario == "stale":
            now_ms -= 30000  # 30 seconds in past

        tickers: Dict[str, Level1Ticker] = {}

        for symbol, base in BASE_MARKET.items():
            if scenario == "missing_pair" and symbol == "BNBBTC":
                continue

            bid = base["bid"]
            ask = base["ask"]
            bid_q = base["bid_qty"]
            ask_q = base["ask_qty"]
            status = "ACTIVE"

            if jitter_pct > 0.0:
                noise = (random.random() - 0.5) * 2 * (jitter_pct / 100.0)
                bid = bid * (1.0 + noise)
                ask = max(bid + 0.000001, ask * (1.0 + noise))

            # Scenario modifications
            if scenario == "arbitrage_eth" and symbol == "ETHBTC":
                # Lower ask significantly so buying ETH with BTC is cheap
                ask = 0.029400
                bid = 0.029390
            elif scenario == "arbitrage_sol" and symbol == "SOLBTC":
                ask = 0.0018400
                bid = 0.0018390
            elif scenario == "arbitrage_bnb" and symbol == "BNBBTC":
                ask = 0.007350
                bid = 0.007340
            elif scenario == "sub_threshold" and symbol == "ETHBTC":
                # Tune to +0.02% net yield (covers 0.30% fee + 0.02% = 0.32% gross)
                ask = 0.029880
                bid = 0.029870
            elif scenario == "crossed_book" and symbol == "BTCUSDT":
                bid = 80005.00
                ask = 80000.00
                status = "CROSSED"
            elif scenario == "locked_book" and symbol == "ETHUSDT":
                ask = bid
                status = "LOCKED"

            tickers[symbol] = create_level1_ticker(
                symbol=symbol,
                bid_price=bid,
                ask_price=ask,
                bid_qty=bid_q,
                ask_qty=ask_q,
                timestamp_ms=now_ms,
                status=status,
            )

        return tickers

    @staticmethod
    def generate_raw_binance_payload(scenario: str = "balanced") -> List[Dict[str, str]]:
        """
        Generates raw Binance REST JSON dictionaries as returned by bookTicker endpoint.
        Used for testing the raw JSON ingestion and normalization pipeline without network.
        """
        market = SyntheticMarketGenerator.generate_market(scenario=scenario)
        raw_list: List[Dict[str, str]] = []
        for sym in CORE_SYMBOLS:
            if sym in market:
                t = market[sym]
                raw_list.append({
                    "symbol": t.symbol,
                    "bidPrice": f"{t.bid_price:.8f}",
                    "bidQty": f"{t.bid_qty:.8f}",
                    "askPrice": f"{t.ask_price:.8f}",
                    "askQty": f"{t.ask_qty:.8f}",
                })
        return raw_list


class MockBinanceIngestionService:
    """
    Mock background poller for offline simulation and local terminal dashboard testing.
    Pumps synthetic updates into OrderBookStore with realistic timing.
    """

    def __init__(
        self,
        store: OrderBookStore,
        poll_interval: float = 2.0,
        scenario: str = "balanced",
    ) -> None:
        self.store = store
        self.poll_interval = poll_interval
        self.scenario = scenario
        self._running = False
        self._task: Optional[Any] = None

    async def start(self) -> None:
        self._running = True
        await self.tick()

    async def stop(self) -> None:
        self._running = False

    def set_scenario(self, scenario: str) -> None:
        self.scenario = scenario

    async def tick(self) -> None:
        """Pumps a single synthetic tick into the store."""
        tickers = SyntheticMarketGenerator.generate_market(
            scenario=self.scenario,
            jitter_pct=0.02,  # 2 bps jitter for dashboard tick flashes
        )
        await self.store.update_tickers(
            tickers=list(tickers.values()),
            latency_ms=12.5,
            used_weight_1m=4,
        )
