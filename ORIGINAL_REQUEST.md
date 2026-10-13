# Original User Request

## Initial Request — 2026-09-05T11:22:59Z

Deploy a full multi-agent team to build ArbForge: a real-time quantitative orderbook depth scanner and triangular arbitrage cycle engine streaming live Level-1 top-of-book market depth, constructing a directed conversion graph, and deterministically evaluating closed triangular execution loops to detect synthetic exchange rate discrepancies in real time.

Working directory: c:\Projects\ArbForge
Integrity mode: development

## Requirements

### R1. Live Level-1 Orderbook Ingestion & Normalization
- Concurrently poll top-of-book market depth (best bid price, best bid quantity, best ask price, best ask quantity) for core cryptocurrency trading pairs (including BTCUSDT, ETHUSDT, ETHBTC, SOLUSDT, SOLBTC, BNBUSDT, BNBBTC) from the unauthenticated public Binance bookTicker REST endpoint (https://api.binance.com/api/v3/ticker/bookTicker).
- Store normalized pairs in an in-memory, thread-safe / asyncio-safe structure with calculated spreads (absolute and basis points) and update timestamps.
- Expose a REST endpoint at `/api/orderbook-matrix` returning the live normalized Level-1 data.

### R2. Directed Asset Conversion Graph & Deterministic Arbitrage Engine
- Model asset conversion as a directed graph where nodes represent assets and edges represent market exchange rates.
- Enumerate closed 3-asset loops (A -> B -> C -> A, e.g., USDT -> BTC -> ETH -> USDT).
- Model bid/ask spread execution realities deterministically:
  - Buying Base with Quote executes at Ask price (Qty_Base = Qty_Quote / Ask).
  - Selling Base for Quote executes at Bid price (Qty_Quote = Qty_Base * Bid).
  - Include taker fee deduction per leg and compute yield percentage:
    Yield = ((Qty_final - Qty_initial) / Qty_initial) * 100
  - Evaluate Level-1 top-of-book depth bottlenecks across all legs to determine maximum executable volume and expected net profit in quote currency.
  - Flag loops crossing the theoretical execution threshold (> 0.05%) as active arbitrage opportunities.
- Expose an endpoint at `/api/triangular-cycles` detailing active opportunities, step-by-step conversion chains, synthetic vs. direct rates, and discrepancy percentages.

### R3. High-Density Financial Terminal Interface
- Implement an institutional dark-mode financial terminal dashboard using CSS Grid and vanilla JavaScript served by FastAPI at `/`.
- Real-time tabular display of current Best Bid, Best Ask, Bid/Ask Quantities, and calculated Spreads (bps) with visual tick update flashes.
- Real-time Arbitrage Loop Card / Panel detailing step-by-step conversion chains, execution status, live discrepancy yield % (color-coded green for positive, red for negative), and executable capacity.
- Configurable background polling or streaming interval (~2 seconds) with live UI updates.

## Verification Resources & Acceptance Criteria

### Automated Testing
- [ ] Implement an automated test suite (in tests/test_engine.py) verifying:
  1. Directional bid/ask conversions (buying at ask vs selling at bid).
  2. Yield percentage calculations with and without trading fees.
  3. Depth bottleneck calculation determining maximum executable size based on Level-1 quantities.
  4. Detection of synthetic exchange rate discrepancies exceeding the 0.05% threshold.
- [ ] Automated test suite runs and passes with 100% success rate (python -m unittest discover tests or pytest).

### System Integration & Service Verification
- [ ] Application starts cleanly via `python app.py` or `uvicorn app:app` on port 8000 without errors.
- [ ] `/api/orderbook-matrix` returns valid JSON with all monitored pairs, bid/ask prices, and depths.
- [ ] `/api/triangular-cycles` returns computed loops and discrepancy metrics.
- [ ] Web dashboard renders responsive dark-mode financial layout and updates dynamically without console errors.
