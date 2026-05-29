"""FastAPI app — JSON API + static Plotly frontend.

Stateless request handlers (no per-process session state) so the app scales horizontally:
run N replicas behind a load balancer; shared state lives in the SignalStore (swap SQLite
for Postgres/Redis via env) and the data cache. CPU/IO handlers are sync `def` so Starlette
runs them in a threadpool, keeping the event loop free.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import atr_strategy as strat
from .. import data as datamod
from .. import instruments, signals, tradingview
from ..simcore import Simulation, expected_trades_per_cycle
from . import serialization as ser
from .config import settings
from .schemas import BacktestReq, CoinFlipReq, FromSignalsReq, OptionsReq

app = FastAPI(title="Antimartingale studio", version="1.0",
              description="Antimartingale simulator + ATR backtest + options + TradingView ingest")

app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False)

STORE: signals.SignalStore = signals.SQLiteSignalStore(settings.signal_db)
MP = settings.max_points


# ----------------------------------------------------------------- infra routes
@app.get("/api/health")
def health():
    return {"status": "ok", "webhook_enabled": settings.webhook_enabled}


@app.get("/api/instruments")
def list_instruments():
    return {"groups": {g: [{"ticker": t, "label": l} for t, l in items]
                       for g, items in instruments.CATALOG.items()}}


# ----------------------------------------------------------------- tab 1: coin-flip
@app.post("/api/coinflip")
def coinflip(req: CoinFlipReq):
    sim = Simulation()
    res = sim.simulate(req.iterations, req.target_streak, req.base_bet, req.win_prob,
                       req.mode, stop_at_first_target=req.stop_at_first_target, seed=req.seed)
    et = expected_trades_per_cycle(req.target_streak, req.win_prob)
    hist_x = list(range(len(res.history)))
    return {
        "history": ser.list_xy(hist_x, res.history, MP),
        "last_series": ser.list_xy(list(range(len(res.last_series))), res.last_series, MP),
        "series_counter": {str(k): v for k, v in sorted(res.series_counter.items())},
        "stats": {
            "trials": res.total_iterations, "cycles": res.cycles, "successes": res.successes,
            "final_bank": res.cumulative_bank,
            "ev_cycle_theory": res.closed_form_ev_cycle,
            "ev_cycle_empirical": res.empirical_ev_cycle,
            "trades_per_cycle": et,
            "ev_trade_theory": (res.closed_form_ev_cycle / et) if et else None,
        },
    }


# ----------------------------------------------------------------- tab 2/3: backtests
def _load(ticker: str, start: str, atr_period: int):
    try:
        daily = datamod.fetch(ticker, start=start)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"data fetch failed: {ex}")
    weekly = datamod.weekly(daily)
    watr = datamod.atr(weekly, atr_period)
    return daily, weekly, watr


def _backtest_payload(daily, res, options=False):
    out = {
        "price": ser.series_xy(daily["Close"], MP),
        "entries": ser.entries_payload(res.trials),
        "equity": ser.list_xy(res.equity_dates, res.equity, MP),
        "cum_commission": ser.list_xy(res.equity_dates, res.cum_commission, MP),
        "cum_slippage": ser.list_xy(res.equity_dates, res.cum_slippage, MP),
        "cum_cost": ser.list_xy(res.equity_dates, res.cum_cost, MP),
        "stats": {
            "n_trials": res.n_trials, "wins": res.wins, "empirical_p": res.empirical_p,
            "final_bank": res.final_bank, "max_drawdown": res.max_drawdown,
            "ev_cycle": res.closed_form_ev_cycle,
            "n_cycles": res.n_cycles,
            "total_commission": res.total_commission, "total_slippage": res.total_slippage,
            "total_cost": res.total_cost, "cost_per_cycle": res.cost_per_cycle,
            "cost_as_prob": res.cost_as_prob,
            "commission_as_prob": res.commission_as_prob,
            "slippage_as_prob": res.slippage_as_prob,
            "breakeven_p_with_cost": res.breakeven_p_with_cost,
            "edge_vs_breakeven": res.empirical_p - res.breakeven_p_with_cost,
        },
    }
    if options and res.delta_path:
        out["delta"] = ser.list_xy(res.delta_dates, res.delta_path, MP)
        dp = res.delta_path
        out["stats"].update({"delta_mean": sum(dp) / len(dp),
                             "delta_min": min(dp), "delta_max": max(dp)})
    return out


@app.post("/api/backtest/linear")
def backtest_linear(req: BacktestReq):
    daily, weekly, watr = _load(req.ticker, req.start, req.atr_period)
    trials = strat.resolve_trials(daily, weekly, watr, req.mult)
    if not trials:
        raise HTTPException(status_code=422, detail="no trials resolved for these params")
    res = strat.run_linear(trials, req.base_bet, req.target_streak,
                           commission=req.commission, slippage_pct=req.slippage_pct,
                           starting_bank=req.starting_bank, cap_mult=req.cap_mult)
    return _backtest_payload(daily, res)


@app.post("/api/backtest/options")
def backtest_options(req: OptionsReq):
    daily, weekly, watr = _load(req.ticker, req.start, req.atr_period)
    trials = strat.resolve_trials(daily, weekly, watr, req.mult)
    if not trials:
        raise HTTPException(status_code=422, detail="no trials resolved for these params")
    rv = datamod.realized_vol(daily["Close"], req.iv_window)
    res = strat.run_options(trials, daily, rv, req.base_bet, req.target_streak,
                            r=req.r, dte_days=req.dte_days, target_delta=req.target_delta,
                            commission=req.commission, slippage_pct=req.slippage_pct,
                            starting_bank=req.starting_bank, cap_mult=req.cap_mult)
    return _backtest_payload(daily, res, options=True)


# ----------------------------------------------------------------- TradingView ingest
@app.post("/api/webhook/tradingview")
async def tv_webhook(request: Request, x_webhook_secret: str | None = Header(default=None)):
    if not settings.webhook_enabled:
        raise HTTPException(status_code=503, detail="webhook disabled (set ANTIMG_WEBHOOK_SECRET)")
    raw = (await request.body()).decode("utf-8", "replace")
    payload = raw
    try:
        import json
        payload = json.loads(raw)
    except Exception:
        pass
    secret = x_webhook_secret or tradingview.extract_passphrase(payload)
    if secret != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="bad webhook secret")
    sig = tradingview.parse_alert(payload)
    sid = STORE.add(sig)
    return {"ok": True, "id": sid, "ticker": sig.ticker, "outcome": sig.resolved_outcome()}


@app.get("/api/signals")
def get_signals(strategy_id: str | None = None, limit: int = 1000):
    rows = STORE.list(strategy_id=strategy_id, limit=min(limit, 5000))
    return {"count": len(rows), "signals": [signals.to_dict(s) for s in rows]}


@app.delete("/api/signals")
def del_signals(strategy_id: str | None = None):
    return {"deleted": STORE.clear(strategy_id=strategy_id)}


@app.post("/api/backtest/from-signals")
def backtest_from_signals(req: FromSignalsReq):
    rows = STORE.list(strategy_id=req.strategy_id, limit=5000)
    trials = signals.signals_to_trials(rows)
    if not trials:
        raise HTTPException(status_code=422, detail="no outcome-bearing signals stored yet")
    res = strat.run_linear(trials, req.base_bet, req.target_streak,
                           commission=req.commission, slippage_pct=req.slippage_pct,
                           starting_bank=req.starting_bank, cap_mult=req.cap_mult)
    return {
        "equity": ser.list_xy(res.equity_dates, res.equity, MP),
        "stats": {"n_trials": res.n_trials, "wins": res.wins, "empirical_p": res.empirical_p,
                  "final_bank": res.final_bank, "max_drawdown": res.max_drawdown,
                  "ev_cycle": res.closed_form_ev_cycle},
    }


# ----------------------------------------------------------------- static frontend
_STATIC = Path(__file__).parent / "static"
if _STATIC.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")


@app.exception_handler(HTTPException)
async def _http_exc(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
