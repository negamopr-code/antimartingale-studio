# Antimartingale studio

Three-tab desktop app for studying the **pyramid-on-wins (antimartingale)** strategy —
double the bet after every win, reset after every loss, stop a streak at a target `N`.

> Doctrine + EV math live in `~/.claude/skills/antimartingal-strategy/SKILL.md`.
> Core identity: **`E[cycle] = b · ((2p)^N − 1)`** — positive only when `p > 0.5`.

## The three tabs

| Tab | What it does |
|-----|--------------|
| **1 · Coin-flip** | The abstract simulator (improved from the original Tkinter script). Inputs: iterations, target streak, base bet, win prob, seed, separate/continuous. Plots cumulative bank + last streak; side panel shows the closed-form vs empirical EV and the cycle-length histogram. |
| **2 · ATR backtest** | The strategy on a **real instrument**. Pick any ticker, set commissions/slippage; **ATR is computed automatically** (weekly). Weekly entry, daily intra-week resolution (see below). Plots price + win/loss entries and the equity curve. |
| **3 · Options (auto Δ)** | Same win/loss sequence, but P&L from a **modeled deep-ITM call**. Black-Scholes **delta is computed automatically and plotted** over each holding period (IV = the asset's realized volatility). Configurable DTE, target delta, risk-free rate. Shows how the option's premium floor truncates the left tail. |

## Resolution rule (Tabs 2 & 3) — confirmed 2026-05-29

- Entry = **weekly open**; ATR = ATR(14) on **weekly** bars; barriers **fixed at entry**:
  `up = open + mult·ATR`, `dn = open − mult·ATR`.
- Walk **daily** bars chronologically vs the fixed barriers: first `high ≥ up` → **win**,
  first `low ≤ dn` → **loss**. A single day touching **both** → **loss-first** (conservative).
  Rationale: weekly ATR ≈ √5 ≈ 2.2× daily, so a single day spanning both barriers needs
  ~4.4 daily-ATR of range — practically never.
- Next entry = the next weekly bar opening after the resolution day.

Weekly entries + daily resolution = full multi-decade history (daily is the finest interval
with full free history) **and** an unambiguous race.

## Run

```bash
./scripts/restore.sh            # venv + deps + tests (idempotent)

.venv/bin/python -m pytest -q   # headless tests
.venv/bin/python scripts/run_gui.py   # GUI — needs python3-tk (run on a host, not the dev container)
```

`tkinter` is not installed in the dev container; the math modules
(`simcore`, `data`, `atr_strategy`, `options`) are fully testable headless.

## Web app (FastAPI + Plotly) & deployment

The same math core is exposed as a JSON API with a static Plotly frontend (4 tabs: the
three above + a **TradingView** tab). See `ARCHITECTURE.md` for the scaling & TradingView
integration design.

```bash
# local dev (auto-reload) -> http://127.0.0.1:8000
.venv/bin/python scripts/run_web.py

# production-ish, portable Docker (any VPS / PaaS)
docker compose -f deploy/docker-compose.yml up --build
# scale horizontally:
docker compose -f deploy/docker-compose.yml up --scale web=3
```

TradingView: set `ANTIMG_WEBHOOK_SECRET`, point your alert webhook at
`/api/webhook/tradingview` (JSON template in `src/antimg/tradingview.py`). Closed-trade
alerts feed the antimartingale sizing engine via the **TradingView** tab.

Key endpoints: `GET /api/health` · `GET /api/instruments` · `POST /api/coinflip` ·
`POST /api/backtest/linear` · `POST /api/backtest/options` ·
`POST /api/webhook/tradingview` · `GET /api/signals` · `POST /api/backtest/from-signals`.
Interactive docs at `/docs`.

## Layout

```
src/antimg/
  simcore.py       coin-flip pyramid + closed-form EV          (Tab 1)
  data.py          yfinance/stooq fetch, cache, ATR, weekly, realized vol
  instruments.py   broad ticker catalog
  atr_strategy.py  resolve_trials (win/loss) + run_linear / run_options
  options.py       Black-Scholes delta / price / strike-for-delta
  app.py           Tkinter ttk.Notebook GUI
  signals.py       Signal schema + SignalStore (SQLite/in-mem) + signals->trials
  tradingview.py   TradingView alert payload -> Signal adapter
  web/
    api.py         FastAPI app (JSON API + static mount)
    config.py      env-driven settings (12-factor)
    schemas.py     pydantic request models (+ anti-DoS caps)
    serialization.py  pandas -> compact JSON (downsampled)
    static/        index.html · app.js · style.css  (Plotly SPA, 4 tabs)
tests/             headless unit + API tests (synthetic data, no network)
scripts/           run_gui.py · run_web.py · restore.sh
deploy/            Dockerfile · docker-compose.yml
ARCHITECTURE.md    layers, signal abstraction, scaling & TradingView path
```
