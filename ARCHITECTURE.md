# Architecture & scaling

## Layers

```
              ┌─────────────────────────────────────────────┐
   browser ─▶ │  static SPA (Plotly)   src/antimg/web/static │
              └───────────────┬─────────────────────────────┘
                              │ JSON over HTTP
              ┌───────────────▼─────────────────────────────┐
   TradingView│  FastAPI  src/antimg/web/api.py              │
   webhook  ─▶│   /api/coinflip  /api/backtest/*  /api/webhook│
              └───────────────┬─────────────────────────────┘
                              │ pure-Python calls (no transport coupling)
              ┌───────────────▼─────────────────────────────┐
              │  math core (transport-agnostic, unit-tested) │
              │   simcore · data · atr_strategy · options    │
              │   signals (SignalStore) · tradingview        │
              └───────────────┬─────────────────────────────┘
                              │
                   ┌──────────▼──────────┐
                   │ SignalStore + cache │  SQLite + pickle (1 node)
                   └─────────────────────┘  → Postgres/Redis (N nodes)
```

The **same math core** powers the desktop GUI (`app.py`) and the web API — the web layer
only adds transport, validation, serialization and signal ingestion. Nothing in the core
imports FastAPI, so it stays testable and reusable.

## The signal abstraction (this is the TradingView seam)

Every strategy reduces to a **sequence of `Trial`s** (a win or a loss with entry/exit).
The antimartingale sizing engine (`atr_strategy.run_linear` / `run_options`) is agnostic to
where trials come from:

| Source | Producer | Status |
|--------|----------|--------|
| Historical ATR | `atr_strategy.resolve_trials(price)` | implemented |
| **TradingView** | alert → `tradingview.parse_alert` → `signals.SignalStore` → `signals.signals_to_trials` | implemented (ingest + replay + **open/close pairing** + live **`GET /api/next-bet`**) |

So a TradingView Pine strategy is just a **signal generator**; our app is the
**antimartingale money-management calculator** layered on top of its win/loss stream.

### TradingView wiring
1. Set `ANTIMG_WEBHOOK_SECRET` on the server.
2. In the TradingView alert dialog, point the webhook URL at
   `https://<host>/api/webhook/tradingview` and use the JSON template in
   `src/antimg/tradingview.py` (must include `"passphrase": "<secret>"` and a `pnl` or
   `"outcome"`).
3. Closed-trade alerts accumulate in the `SignalStore`; the **4 · TradingView** tab (or
   `POST /api/backtest/from-signals`) replays them through the sizing engine.

**Closed loop (implemented):** separate open/close alerts are paired into trials by
`signals.signals_to_trials` (a buy/sell open + a later close on the same strategy+ticker →
one win/loss from the price move), and `GET /api/next-bet?strategy_id=&base_bet=&target_streak=&cap_mult=`
replays the stored outcomes through `atr_strategy.pyramid_state` and returns the next bet to
place — the read-back a Pine alert (or any client) queries to live-size its next order from the
running streak. Pure read (no mutation).

## Scalability

The service is **stateless** — no per-process session state — so it scales by replicas:

- **Horizontal**: `docker compose up --scale web=N` (or K8s replicas) behind any load
  balancer. `WEB_CONCURRENCY` sets gunicorn/uvicorn workers per replica.
- **Shared state** lives behind interfaces, swappable without touching the engine:
  - `SignalStore` — SQLite (single node) → **Postgres** (implement a `PostgresSignalStore`
    with the same 3 methods; point `ANTIMG_SIGNAL_DB` / a DSN at it).
  - price cache — pickle files → **Redis** (wrap `data.fetch`’s cache read/write).
- **Heavy backtests** run in Starlette’s threadpool today (sync handlers). For long jobs at
  scale, move them to a task queue (**Celery/RQ + Redis**) and return a job id + poll
  endpoint; the engine functions already return plain results, so this is a transport change.
- **Anti-DoS caps** are enforced in `web/config.py` (`max_iterations`, `max_target_streak`,
  `max_points` series downsampling) — tune per environment via env vars.
- **Data provider**: yfinance is rate-limited (Yahoo 429). The pickle cache absorbs repeat
  requests; for production add a TTL + a paid provider behind the same `data.fetch` seam.

## Config (12-factor, all via env — see `web/config.py`)

| Var | Default | Purpose |
|-----|---------|---------|
| `PORT` | 8000 | listen port |
| `WEB_CONCURRENCY` | 4 | workers per replica |
| `ANTIMG_CORS_ORIGINS` | `*` | comma-separated allowed origins |
| `ANTIMG_WEBHOOK_SECRET` | _(empty)_ | TradingView passphrase; empty disables the webhook |
| `ANTIMG_CACHE` | `/data/cache` (image) | price cache dir |
| `ANTIMG_SIGNAL_DB` | `/data/signals.db` (image) | SQLite signal store |
| `ANTIMG_MAX_ITERATIONS` | 2000000 | coin-flip cap |
| `ANTIMG_DEFAULT_START` | 2005-01-01 | default history start |
```
