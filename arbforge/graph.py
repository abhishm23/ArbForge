"""
arbforge/graph.py
Directed Asset Conversion Graph & Deterministic Cycle Enumeration Engine for ArbForge.

Models assets as graph nodes and market trading pairs as dual directed edges.
Implements deterministic cycle enumeration for all 6 USDT-rooted triangular loops
and all 18 directional paths across the 7 core cryptocurrency trading pairs.

Conforms strictly to ORIGINAL_REQUEST.md, PROJECT.md, and arbforge/models.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Collection, Dict, List, Optional, Set, Tuple, Union

from arbforge.models import ConversionLeg, Level1Ticker


# ============================================================================
# Core Constants
# ============================================================================

CORE_ASSETS: Tuple[str, ...] = ("USDT", "BTC", "ETH", "SOL", "BNB")

CORE_PAIRS: Tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "ETHBTC",
    "SOLUSDT",
    "SOLBTC",
    "BNBUSDT",
    "BNBBTC",
)

PAIR_DEFINITIONS: Dict[str, Tuple[str, str]] = {
    "BTCUSDT": ("BTC", "USDT"),
    "ETHUSDT": ("ETH", "USDT"),
    "ETHBTC": ("ETH", "BTC"),
    "SOLUSDT": ("SOL", "USDT"),
    "SOLBTC": ("SOL", "BTC"),
    "BNBUSDT": ("BNB", "USDT"),
    "BNBBTC": ("BNB", "BTC"),
}


# ============================================================================
# Directed Conversion Edge
# ============================================================================

@dataclass(frozen=True)
class DirectedEdge:
    """
    Represents an executable directed conversion from from_asset to to_asset.
    Encapsulates market orderbook execution rules:
      - Quote -> Base: BUY at Ask
      - Base -> Quote: SELL at Bid
    """
    from_asset: str
    to_asset: str
    symbol: str
    action: str  # "BUY" or "SELL"
    base_asset: str
    quote_asset: str

    def get_price(self, ticker: Any) -> float:
        """
        Returns the execution price (Ask for BUY, Bid for SELL).
        Accepts Level1Ticker instances or dictionary snapshots.
        """
        if isinstance(ticker, dict):
            ask = float(ticker.get("ask_price", ticker.get("askPrice", 0.0)))
            bid = float(ticker.get("bid_price", ticker.get("bidPrice", 0.0)))
        else:
            ask = float(ticker.ask_price)
            bid = float(ticker.bid_price)

        if self.action == "BUY":
            if ask <= 0.0 or not math.isfinite(ask):
                raise ValueError(f"Ask price must be strictly positive, got {ask} for {self.symbol}")
            return ask
        else:  # SELL
            if bid <= 0.0 or not math.isfinite(bid):
                raise ValueError(f"Bid price must be strictly positive, got {bid} for {self.symbol}")
            return bid

    def get_available_qty_base(self, ticker: Any) -> float:
        """
        Returns available resting depth in base currency units.
        Accepts Level1Ticker instances or dictionary snapshots.
        """
        if isinstance(ticker, dict):
            ask_q = float(ticker.get("ask_qty", ticker.get("askQty", 0.0)))
            bid_q = float(ticker.get("bid_qty", ticker.get("bidQty", 0.0)))
        else:
            ask_q = float(ticker.ask_qty)
            bid_q = float(ticker.bid_qty)

        if self.action == "BUY":
            return max(0.0, ask_q)
        else:  # SELL
            return max(0.0, bid_q)

    def get_rate_multiplier(self, price: float, taker_fee_rate: float = 0.001) -> float:
        """
        Computes the effective rate multiplier after taker fee deduction.
        BUY:  (1.0 / price) * (1.0 - fee)
        SELL: price * (1.0 - fee)
        """
        if price <= 0.0:
            raise ValueError("Execution price must be strictly positive")
        fee_factor = 1.0 - taker_fee_rate
        if self.action == "BUY":
            return (1.0 / price) * fee_factor
        else:
            return price * fee_factor

    def get_gross_rate_multiplier(self, price: float) -> float:
        """Computes gross multiplier without fees."""
        if price <= 0.0:
            raise ValueError("Execution price must be strictly positive")
        if self.action == "BUY":
            return 1.0 / price
        else:
            return price

    def get_input_capacity(self, price: float, available_qty_base: float) -> float:
        """
        Returns maximum input quantity in from_asset units that can be consumed.
        BUY:  available_qty_base * price (in Quote asset)
        SELL: available_qty_base (in Base asset)
        """
        if self.action == "BUY":
            return available_qty_base * price
        else:
            return available_qty_base

    def build_conversion_leg(
        self,
        ticker: Any,
        step: int,
        taker_fee_rate: float = 0.001,
    ) -> ConversionLeg:
        """Constructs a validated ConversionLeg data model from live ticker."""
        price = self.get_price(ticker)
        avail_qty = self.get_available_qty_base(ticker)
        multiplier = self.get_rate_multiplier(price, taker_fee_rate)

        return ConversionLeg(
            step=step,
            from_asset=self.from_asset,
            to_asset=self.to_asset,
            symbol=self.symbol,
            action=self.action,
            price=price,
            available_qty_base=avail_qty,
            taker_fee_rate=taker_fee_rate,
            rate_multiplier=multiplier,
        )


# ============================================================================
# Triangular Cycle Path
# ============================================================================

@dataclass(frozen=True)
class TriangularCyclePath:
    """
    Represents an ordered 3-step closed conversion cycle.
    Path format: (A, B, C, A).
    """
    cycle_id: str
    path: Tuple[str, str, str, str]
    edges: Tuple[DirectedEdge, DirectedEdge, DirectedEdge]
    starting_asset: str
    alt_symbol: Optional[str] = None
    orientation: str = "CUSTOM"  # "FORWARD" (USDT->BTC->ALT->USDT) or "REVERSE" (USDT->ALT->BTC->USDT)

    @property
    def symbols(self) -> Tuple[str, str, str]:
        """Returns the market pair symbols required by this cycle."""
        return (self.edges[0].symbol, self.edges[1].symbol, self.edges[2].symbol)

    def build_legs(
        self,
        tickers: Dict[str, Any],
        taker_fee_rate: float = 0.001,
    ) -> List[ConversionLeg]:
        """Builds all 3 ConversionLeg models for this cycle."""
        legs: List[ConversionLeg] = []
        for idx, edge in enumerate(self.edges, start=1):
            ticker = tickers.get(edge.symbol)
            if ticker is None:
                raise KeyError(f"Missing required ticker {edge.symbol} for cycle {self.cycle_id}")
            legs.append(edge.build_conversion_leg(ticker, step=idx, taker_fee_rate=taker_fee_rate))
        return legs

    def calculate_synthetic_vs_direct_rates(
        self,
        tickers: Dict[str, Any],
    ) -> Tuple[float, float, float]:
        """
        Calculates (synthetic_rate, direct_rate, discrepancy_pct) matching the reference oracle.
        Supports canonical forward and reverse USDT cycles.
        """
        def _get_p(sym: str, field_name: str) -> float:
            t = tickers[sym]
            if isinstance(t, dict):
                val = t.get(field_name)
                if val is None:
                    # try camelCase
                    camel = "askPrice" if "ask" in field_name else "bidPrice"
                    val = t.get(camel)
                return float(val)
            return float(getattr(t, field_name))

        if self.orientation == "FORWARD" and self.alt_symbol:
            # Forward: USDT -> BTC -> ALT -> USDT
            p_ask_btc = _get_p("BTCUSDT", "ask_price")
            p_ask_altbtc = _get_p(f"{self.alt_symbol}BTC", "ask_price")
            p_bid_altusdt = _get_p(f"{self.alt_symbol}USDT", "bid_price")

            synthetic_rate = p_ask_altbtc * p_ask_btc
            direct_rate = p_bid_altusdt
            discrepancy_pct = ((direct_rate - synthetic_rate) / synthetic_rate) * 100.0
            return synthetic_rate, direct_rate, discrepancy_pct

        elif self.orientation == "REVERSE" and self.alt_symbol:
            # Reverse: USDT -> ALT -> BTC -> USDT
            p_ask_altusdt = _get_p(f"{self.alt_symbol}USDT", "ask_price")
            p_bid_altbtc = _get_p(f"{self.alt_symbol}BTC", "bid_price")
            p_bid_btcusdt = _get_p("BTCUSDT", "bid_price")

            direct_rate = p_ask_altusdt
            synthetic_rate = p_bid_altbtc * p_bid_btcusdt
            discrepancy_pct = ((synthetic_rate - direct_rate) / direct_rate) * 100.0
            return synthetic_rate, direct_rate, discrepancy_pct

        else:
            # Fallback general calculation
            e1, e2, e3 = self.edges
            t1 = tickers[e1.symbol]
            t2 = tickers[e2.symbol]
            t3 = tickers[e3.symbol]
            gross_synthetic = e1.get_gross_rate_multiplier(e1.get_price(t1)) * e2.get_gross_rate_multiplier(e2.get_price(t2))
            direct_rate = 1.0 / e3.get_gross_rate_multiplier(e3.get_price(t3))
            discrepancy_pct = ((gross_synthetic - direct_rate) / direct_rate) * 100.0 if direct_rate > 0 else 0.0
            return gross_synthetic, direct_rate, discrepancy_pct


# ============================================================================
# Asset Graph
# ============================================================================

class AssetGraph:
    """
    Directed Graph modeling cryptocurrency conversion across trading pairs.
    Manages nodes (assets), edges (pairs/actions), and cycle enumeration.
    """

    def __init__(self, pairs: Optional[Dict[str, Tuple[str, str]]] = None) -> None:
        self._nodes: Set[str] = set()
        self._pairs: Dict[str, Tuple[str, str]] = {}
        self._edges: Dict[Tuple[str, str], DirectedEdge] = {}
        self._adjacency: Dict[str, Set[str]] = {}

        pair_dict = pairs if pairs is not None else PAIR_DEFINITIONS
        for symbol, (base, quote) in pair_dict.items():
            self.add_pair(symbol, base, quote)

    @property
    def nodes(self) -> Set[str]:
        """Returns the set of tradeable assets."""
        return set(self._nodes)

    @property
    def pairs(self) -> Dict[str, Tuple[str, str]]:
        """Returns registered trading pair definitions."""
        return dict(self._pairs)

    def add_pair(self, symbol: str, base_asset: str, quote_asset: str) -> None:
        """
        Adds a trading pair to the graph, creating two directed edges:
          1. quote_asset -> base_asset (BUY at Ask)
          2. base_asset -> quote_asset (SELL at Bid)
        """
        sym = symbol.strip().upper()
        base = base_asset.strip().upper()
        quote = quote_asset.strip().upper()

        self._nodes.add(base)
        self._nodes.add(quote)
        self._pairs[sym] = (base, quote)

        if quote not in self._adjacency:
            self._adjacency[quote] = set()
        if base not in self._adjacency:
            self._adjacency[base] = set()

        # Quote -> Base (BUY)
        edge_buy = DirectedEdge(
            from_asset=quote,
            to_asset=base,
            symbol=sym,
            action="BUY",
            base_asset=base,
            quote_asset=quote,
        )
        self._edges[(quote, base)] = edge_buy
        self._adjacency[quote].add(base)

        # Base -> Quote (SELL)
        edge_sell = DirectedEdge(
            from_asset=base,
            to_asset=quote,
            symbol=sym,
            action="SELL",
            base_asset=base,
            quote_asset=quote,
        )
        self._edges[(base, quote)] = edge_sell
        self._adjacency[base].add(quote)

    def get_edge(self, from_asset: str, to_asset: str) -> Optional[DirectedEdge]:
        """Returns the directed edge connecting from_asset to to_asset, if present."""
        return self._edges.get((from_asset.upper(), to_asset.upper()))

    def get_neighbors(self, asset: str) -> List[str]:
        """Returns all destination assets directly reachable from the specified asset."""
        return sorted(list(self._adjacency.get(asset.upper(), set())))

    # ------------------------------------------------------------------------
    # Canonical USDT-Rooted Loops (Guaranteed 6 Cycles)
    # ------------------------------------------------------------------------

    def get_usdt_triangular_cycles(self) -> List[TriangularCyclePath]:
        """
        Returns the 6 authoritative USDT-rooted triangular loops in canonical order:
          1. USDT -> BTC -> ETH -> USDT  [Forward ETH]
          2. USDT -> ETH -> BTC -> USDT  [Reverse ETH]
          3. USDT -> BTC -> SOL -> USDT  [Forward SOL]
          4. USDT -> SOL -> BTC -> USDT  [Reverse SOL]
          5. USDT -> BTC -> BNB -> USDT  [Forward BNB]
          6. USDT -> BNB -> BTC -> USDT  [Reverse BNB]
        """
        alts = ["ETH", "SOL", "BNB"]
        cycles: List[TriangularCyclePath] = []

        for alt in alts:
            # Forward: USDT -> BTC -> ALT -> USDT
            e1 = self.get_edge("USDT", "BTC")
            e2 = self.get_edge("BTC", alt)
            e3 = self.get_edge(alt, "USDT")

            if e1 and e2 and e3:
                cycles.append(
                    TriangularCyclePath(
                        cycle_id=f"USDT->BTC->{alt}->USDT",
                        path=("USDT", "BTC", alt, "USDT"),
                        edges=(e1, e2, e3),
                        starting_asset="USDT",
                        alt_symbol=alt,
                        orientation="FORWARD",
                    )
                )

            # Reverse: USDT -> ALT -> BTC -> USDT
            e1_rev = self.get_edge("USDT", alt)
            e2_rev = self.get_edge(alt, "BTC")
            e3_rev = self.get_edge("BTC", "USDT")

            if e1_rev and e2_rev and e3_rev:
                cycles.append(
                    TriangularCyclePath(
                        cycle_id=f"USDT->{alt}->BTC->USDT",
                        path=("USDT", alt, "BTC", "USDT"),
                        edges=(e1_rev, e2_rev, e3_rev),
                        starting_asset="USDT",
                        alt_symbol=alt,
                        orientation="REVERSE",
                    )
                )

        return cycles

    # ------------------------------------------------------------------------
    # All 18 Directional Paths Across Core Pairs
    # ------------------------------------------------------------------------

    def get_all_core_cycles(self) -> List[TriangularCyclePath]:
        """
        Returns all 18 directional paths across the 7 core crypto pairs:
        3 triangles x 2 orientations x 3 starting roots = 18 paths.
        """
        alts = ["ETH", "SOL", "BNB"]
        paths: List[TriangularCyclePath] = []

        for alt in alts:
            # Triangle {USDT, BTC, ALT}
            # Orientation 1: Forward (USDT -> BTC -> ALT)
            fwd_loop = ["USDT", "BTC", alt]
            for shift in range(3):
                a = fwd_loop[shift]
                b = fwd_loop[(shift + 1) % 3]
                c = fwd_loop[(shift + 2) % 3]
                e1 = self.get_edge(a, b)
                e2 = self.get_edge(b, c)
                e3 = self.get_edge(c, a)
                if e1 and e2 and e3:
                    is_usdt_root = (a == "USDT")
                    paths.append(
                        TriangularCyclePath(
                            cycle_id=f"{a}->{b}->{c}->{a}",
                            path=(a, b, c, a),
                            edges=(e1, e2, e3),
                            starting_asset=a,
                            alt_symbol=alt,
                            orientation="FORWARD" if is_usdt_root else "CUSTOM",
                        )
                    )

            # Orientation 2: Reverse (USDT -> ALT -> BTC)
            rev_loop = ["USDT", alt, "BTC"]
            for shift in range(3):
                a = rev_loop[shift]
                b = rev_loop[(shift + 1) % 3]
                c = rev_loop[(shift + 2) % 3]
                e1 = self.get_edge(a, b)
                e2 = self.get_edge(b, c)
                e3 = self.get_edge(c, a)
                if e1 and e2 and e3:
                    is_usdt_root = (a == "USDT")
                    paths.append(
                        TriangularCyclePath(
                            cycle_id=f"{a}->{b}->{c}->{a}",
                            path=(a, b, c, a),
                            edges=(e1, e2, e3),
                            starting_asset=a,
                            alt_symbol=alt,
                            orientation="REVERSE" if is_usdt_root else "CUSTOM",
                        )
                    )

        return paths

    # ------------------------------------------------------------------------
    # Dynamic Cycle Enumeration via Graph Traversal
    # ------------------------------------------------------------------------

    def enumerate_triangular_cycles(
        self,
        root_asset: Optional[str] = None,
    ) -> List[TriangularCyclePath]:
        """
        Dynamically finds 3-asset loops via bounded Depth-First Search (DFS).
        If root_asset is specified, filters paths starting and ending with root_asset.
        """
        cycles: List[TriangularCyclePath] = []
        visited_cycles: Set[Tuple[str, str, str]] = set()

        search_roots = [root_asset.upper()] if root_asset else sorted(list(self._nodes))

        for n1 in search_roots:
            for n2 in self.get_neighbors(n1):
                if n2 == n1:
                    continue
                for n3 in self.get_neighbors(n2):
                    if n3 in (n1, n2):
                        continue
                    if n1 in self.get_neighbors(n3):
                        cycle_tuple = (n1, n2, n3)
                        if cycle_tuple in visited_cycles:
                            continue
                        visited_cycles.add(cycle_tuple)

                        e1 = self.get_edge(n1, n2)
                        e2 = self.get_edge(n2, n3)
                        e3 = self.get_edge(n3, n1)

                        if e1 and e2 and e3:
                            # Identify alt asset if canonical
                            alt = None
                            for node in (n1, n2, n3):
                                if node in ("ETH", "SOL", "BNB"):
                                    alt = node
                                    break

                            orientation = "CUSTOM"
                            if n1 == "USDT" and n2 == "BTC":
                                orientation = "FORWARD"
                            elif n1 == "USDT" and n3 == "BTC":
                                orientation = "REVERSE"

                            cycles.append(
                                TriangularCyclePath(
                                    cycle_id=f"{n1}->{n2}->{n3}->{n1}",
                                    path=(n1, n2, n3, n1),
                                    edges=(e1, e2, e3),
                                    starting_asset=n1,
                                    alt_symbol=alt,
                                    orientation=orientation,
                                )
                            )

        return cycles

    def validate_tickers_available(
        self,
        cycle: TriangularCyclePath,
        available_symbols: Collection[str],
    ) -> bool:
        """Verifies that all 3 pairs required by a cycle are available in the snapshot."""
        return all(s in available_symbols for s in cycle.symbols)


# Backward compatibility alias
AssetConversionGraph = AssetGraph
