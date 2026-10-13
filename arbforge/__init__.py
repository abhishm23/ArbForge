"""
ArbForge: Real-Time Quantitative Orderbook Ingestion & Triangular Arbitrage Engine.
"""

from arbforge.api import CANONICAL_PAIRS, router as api_router
from arbforge.engine import (
    ArbitrageEngine,
    ARBITRAGE_THRESHOLD_PCT,
    BNB_DISCOUNT_FEE_RATE,
    DEFAULT_FEE_RATE,
    calculate_depth_bottleneck,
    calculate_yield,
    calculate_yield_pct,
    cumulative_fee_multiplier,
    depth_bottleneck,
    directional_buy,
    directional_sell,
)
from arbforge.graph import (
    CORE_ASSETS,
    CORE_PAIRS,
    PAIR_DEFINITIONS,
    AssetConversionGraph,
    AssetGraph,
    DirectedEdge,
    TriangularCyclePath,
)
from arbforge.ingestion import (
    BINANCE_BOOK_TICKER_URL,
    BinanceBookTickerPoller,
    BinanceIngestionService,
)
from arbforge.mock import (
    BASE_MARKET,
    MockBinanceIngestionService,
    SyntheticMarketGenerator,
    create_level1_ticker,
)
from arbforge.models import (
    ConversionLeg,
    CrossedBookError,
    EmptyBookError,
    InvalidPayloadError,
    InvalidPriceError,
    InvalidQuantityError,
    Level1Ticker,
    LockedBookError,
    OrderBookError,
    OrderBookMatrixResponse,
    TriangularCycleOpportunity,
    TriangularCyclesResponse,
    calculate_spreads,
    parse_safe_float,
    parse_safe_price,
    parse_safe_quantity,
    parse_symbol_pair,
)
from arbforge.store import CORE_SYMBOLS, OrderBookStore

__all__ = [
    # Models
    "Level1Ticker",
    "OrderBookMatrixResponse",
    "ConversionLeg",
    "TriangularCycleOpportunity",
    "TriangularCyclesResponse",
    "calculate_spreads",
    "parse_symbol_pair",
    "parse_safe_float",
    "parse_safe_price",
    "parse_safe_quantity",
    # Exceptions
    "OrderBookError",
    "InvalidPayloadError",
    "InvalidPriceError",
    "InvalidQuantityError",
    "EmptyBookError",
    "CrossedBookError",
    "LockedBookError",
    # Store
    "OrderBookStore",
    "CORE_SYMBOLS",
    # Ingestion
    "BinanceBookTickerPoller",
    "BinanceIngestionService",
    "BINANCE_BOOK_TICKER_URL",
    # Mocking
    "SyntheticMarketGenerator",
    "create_level1_ticker",
    "MockBinanceIngestionService",
    "BASE_MARKET",
    # API
    "api_router",
    "CANONICAL_PAIRS",
    # Graph (Milestone 2)
    "AssetGraph",
    "AssetConversionGraph",
    "DirectedEdge",
    "TriangularCyclePath",
    "CORE_ASSETS",
    "CORE_PAIRS",
    "PAIR_DEFINITIONS",
    # Engine (Milestone 2)
    "ArbitrageEngine",
    "directional_buy",
    "directional_sell",
    "cumulative_fee_multiplier",
    "calculate_yield_pct",
    "calculate_yield",
    "calculate_depth_bottleneck",
    "depth_bottleneck",
    "DEFAULT_FEE_RATE",
    "BNB_DISCOUNT_FEE_RATE",
    "ARBITRAGE_THRESHOLD_PCT",
]
