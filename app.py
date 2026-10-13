"""
app.py
ArbForge: Real-Time Orderbook Depth Scanner & Triangular Arbitrage Cycle Engine.

Main FastAPI application entrypoint with:
- Async Binance bookTicker ingestion lifecycle
- REST API for orderbook matrix, triangular cycles, health, and config
- Server-Sent Events (SSE) live stream
- Static HTML terminal dashboard
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from arbforge.api import router as api_router, get_orderbook_store
from arbforge.engine import ArbitrageEngine, DEFAULT_FEE_RATE, ARBITRAGE_THRESHOLD_PCT
from arbforge.ingestion import BinanceBookTickerPoller
from arbforge.store import CORE_SYMBOLS, OrderBookStore

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("arbforge")

# ---------------------------------------------------------------------------
# Configuration State (mutable at runtime via /api/config)
# ---------------------------------------------------------------------------
_config: Dict[str, Any] = {
    "fee_rate": DEFAULT_FEE_RATE,
    "threshold_pct": ARBITRAGE_THRESHOLD_PCT,
    "initial_capital": 10_000.0,
    "poll_interval": 2.0,
}

_startup_time: float = 0.0

# ---------------------------------------------------------------------------
# Application Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown of the Binance book ticker poller."""
    global _startup_time
    _startup_time = time.time()

    store = OrderBookStore(core_symbols=CORE_SYMBOLS)
    app.state.store = store

    poller = BinanceBookTickerPoller(
        store=store,
        poll_interval=_config["poll_interval"],
        symbols=CORE_SYMBOLS,
    )
    app.state.poller = poller

    logger.info("ArbForge engine starting — polling %d pairs every %.1fs",
                len(CORE_SYMBOLS), _config["poll_interval"])

    await poller.start()

    yield

    logger.info("ArbForge engine shutting down...")
    await poller.stop()


# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ArbForge",
    description="Real-Time Orderbook Depth Scanner & Triangular Arbitrage Cycle Engine",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount the API router under /api
app.include_router(api_router, prefix="/api")


# ---------------------------------------------------------------------------
# SSE Stream: /api/stream
# ---------------------------------------------------------------------------

async def _sse_generator(store: OrderBookStore):
    """Yields Server-Sent Events with orderbook + arbitrage data every poll cycle."""
    while True:
        try:
            matrix = store.get_matrix()
            telemetry = store.get_telemetry()

            # Evaluate arbitrage cycles
            opportunities = []
            if matrix:
                try:
                    opportunities = ArbitrageEngine.evaluate_cycles(
                        tickers=matrix,
                        fee_rate=_config["fee_rate"],
                        min_yield_threshold=_config["threshold_pct"],
                    )
                except Exception:
                    pass

            # Serialize tickers
            pairs_data = []
            for sym in CORE_SYMBOLS:
                t = matrix.get(sym)
                if t:
                    pairs_data.append(t.model_dump())

            # Serialize opportunities
            opps_data = [opp.model_dump() for opp in opportunities]
            active_count = sum(1 for o in opportunities if o.is_arbitrage)

            payload = {
                "type": "update",
                "timestamp_ms": int(time.time() * 1000),
                "iso_time": datetime.now(timezone.utc).isoformat(),
                "pairs": pairs_data,
                "opportunities": opps_data,
                "active_count": active_count,
                "scanned_count": len(opps_data),
                "telemetry": {
                    "latency_ms": telemetry.get("latency_ms", 0),
                    "poll_count": telemetry.get("poll_count", 0),
                    "error_count": telemetry.get("error_count", 0),
                    "last_error": telemetry.get("last_error"),
                    "used_weight_1m": telemetry.get("used_weight_1m", 0),
                    "pair_count": telemetry.get("pair_count", 0),
                    "uptime_s": round(time.time() - _startup_time, 1),
                },
                "config": dict(_config),
            }

            yield f"data: {json.dumps(payload, default=str)}\n\n"

        except asyncio.CancelledError:
            break
        except Exception as exc:
            error_payload = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(error_payload)}\n\n"

        await asyncio.sleep(_config["poll_interval"])


@app.get("/api/stream", tags=["Streaming"])
async def sse_stream(request: Request):
    """Server-Sent Events endpoint streaming live market data and arbitrage cycles."""
    store: OrderBookStore = get_orderbook_store(request)
    return StreamingResponse(
        _sse_generator(store),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Config Endpoint: GET & POST /api/config
# ---------------------------------------------------------------------------

@app.get("/api/config", tags=["Configuration"])
async def get_config():
    """Returns the current engine configuration."""
    return {"status": "ok", "config": dict(_config)}


@app.post("/api/config", tags=["Configuration"])
async def update_config(request: Request):
    """Dynamically update engine configuration (fee_rate, threshold, capital, poll_interval)."""
    body = await request.json()

    if "fee_rate" in body:
        _config["fee_rate"] = max(0.0, min(1.0, float(body["fee_rate"])))
    if "fee_bps" in body:
        _config["fee_rate"] = max(0.0, min(1.0, float(body["fee_bps"]) / 10_000.0))
    if "threshold_pct" in body:
        _config["threshold_pct"] = max(0.0, float(body["threshold_pct"]))
    if "initial_capital" in body:
        _config["initial_capital"] = max(0.0, float(body["initial_capital"]))
    if "poll_interval" in body:
        new_interval = max(1.0, min(30.0, float(body["poll_interval"])))
        _config["poll_interval"] = new_interval
        if hasattr(request.app.state, "poller"):
            request.app.state.poller.poll_interval = new_interval

    return {"status": "ok", "config": dict(_config)}


# ---------------------------------------------------------------------------
# Health Endpoint: GET /api/health
# ---------------------------------------------------------------------------

@app.get("/api/health", tags=["Health"])
async def health_check(request: Request):
    """Returns engine health, uptime, Binance API latency, and cycle throughput."""
    store: OrderBookStore = get_orderbook_store(request)
    telemetry = store.get_telemetry()
    status = store.get_status(max_age_sec=5.0)

    return {
        "status": status,
        "uptime_s": round(time.time() - _startup_time, 1),
        "latency_ms": telemetry.get("latency_ms", 0),
        "poll_count": telemetry.get("poll_count", 0),
        "error_count": telemetry.get("error_count", 0),
        "last_error": telemetry.get("last_error"),
        "used_weight_1m": telemetry.get("used_weight_1m", 0),
        "pair_count": telemetry.get("pair_count", 0),
        "config": dict(_config),
        "monitored_pairs": CORE_SYMBOLS,
    }


# ---------------------------------------------------------------------------
# Dashboard: GET /
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
async def dashboard():
    """Serves the high-density financial terminal dashboard."""
    index_path = TEMPLATES_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse(
            content="<h1>ArbForge</h1><p>Dashboard template not found.</p>",
            status_code=500,
        )
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


@app.get("/docs-interactive", response_class=HTMLResponse, tags=["Documentation"])
@app.get("/interactive-docs", response_class=HTMLResponse, tags=["Documentation"])
async def interactive_docs():
    """Serves the interactive quantitative documentation and simulator."""
    docs_path = TEMPLATES_DIR / "docs.html"
    if not docs_path.exists():
        return HTMLResponse(
            content="<h1>ArbForge</h1><p>Documentation template not found.</p>",
            status_code=500,
        )
    return HTMLResponse(content=docs_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("ARBFORGE_PORT", "8000"))
    host = os.environ.get("ARBFORGE_HOST", "127.0.0.1")

    logger.info("Starting ArbForge on %s:%d", host, port)
    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
        access_log=True,
    )
