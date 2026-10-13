# ArbForge — Real-Time Cryptocurrency Triangular Arbitrage Engine

High-performance triangular arbitrage detection and execution simulation engine engineered with FastAPI, asyncio, Jinja2, and WebSockets.

## Overview
ArbForge monitors multi-exchange order books and currency pairs simultaneously, detecting negative-cycle price discrepancies in real-time and calculating execution profit margins including fees and slippage.

## Features
- **Asyncio Price Ingestion**: High-throughput simulated and live order book streaming.
- **Bellman-Ford / Log-Price Cycle Detection**: Rapid cyclic graph evaluation for 3-leg trade loops.
- **Live Visual Dashboard**: Real-time arbitrage opportunity monitoring and execution ledger.

## Quick Start
```bash
launch.bat
# Access web dashboard at http://localhost:8000
```
