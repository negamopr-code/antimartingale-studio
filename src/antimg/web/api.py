"""FastAPI app — JSON API + static Plotly frontend.

Stateless request handlers (no per-process session state) so the app scales horizontally:
run N replicas behind a load balancer; shared state lives in the SignalStore (swap SQLite
for Postgres/Redis via env) and the data cache. CPU/IO handlers are sync `def` so Starlette
runs them in a threadpool, keeping the event loop free.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import atr_strategy as strat
from .. import data as datamod
from .. import hedged_intraday as hi
from .. import pi_model as pim
from .. import vol as volmod
from .. import instruments, scenarios, signals, tradingview
from ..simcore import Simulation, expected_trades_per_cycle
from . import serialization as ser
from .config import settings
from .schemas import (BacktestReq, CoinFlipReq, ExplainReq, FromSignalsReq,
                      HedgedIntradayReq, HedgedIntradayScanReq, InspectReq, OptionsReq, ScanReq)

app = FastAPI(title="Antimartingale studio", version="1.0",
              description="Antimartingale simulator + ATR backtest + options + TradingView ingest")

app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False)


@app.middleware("http")
async def _no_cache_app_assets(request: Request, call_next):
    """Force revalidation of the app shell (html/js/css) so a redeploy is always picked
    up. The big vendored Plotly bundle stays cacheable."""
    resp = await call_next(request)
    p = request.url.path
    if "/vendor/" not in p and (p == "/" or p.endswith((".js", ".css", ".html"))):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

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
        "table": res.table,
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
    out["entries"]["add"] = {"x": [d.isoformat() for d in res.add_dates],
                             "y": [float(v) for v in res.add_levels]}
    if options and res.delta_path:
        out["delta"] = ser.list_xy(res.delta_dates, res.delta_path, MP)
        dp = res.delta_path
        out["stats"].update({"delta_mean": sum(dp) / len(dp),
                             "delta_min": min(dp), "delta_max": max(dp)})
    return out


@app.post("/api/backtest/linear")
def backtest_linear(req: BacktestReq):
    daily, weekly, watr = _load(req.ticker, req.start, req.atr_period)
    # scale-into-one-position campaign on the ATR grid (shares / linear)
    res = strat.run_campaign(daily, weekly, watr, base_bet=req.base_bet,
                             target_streak=req.target_streak, mult=req.mult,
                             instrument="shares", mode=req.mode,
                             commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
                             starting_bank=req.starting_bank, cap_mult=req.cap_mult)
    if not res.table:
        raise HTTPException(status_code=422, detail="no campaigns resolved for these params")
    return _backtest_payload(daily, res)


def _build_vol(req, daily, ticker: str | None = None):
    """Construct the IV surface (real CBOE term structure + fixed-β skew) for the option model.

    Falls back to the asset's realized vol when no vol index is available (non-S&P/-VXN/etc),
    and to a flat constant when iv_source='constant'. With use_term_structure=False only the
    nearest tenor is used (flat in T). Returns a vol.VolModel (see src/antimg/vol.py).

    `ticker` overrides `req.ticker` — used by the hedged-intraday scan, whose request has no
    single ticker (it iterates the catalog), so each instrument builds its own surface.
    """
    ticker = ticker or req.ticker
    realized = datamod.realized_vol(daily["Close"], req.iv_window)
    vm = volmod.build(ticker, req.start, iv_source=req.iv_source,
                      skew_beta=req.skew_beta, realized=realized, iv_const=req.iv_const)
    if not req.use_term_structure and len(vm._T) > 1:    # collapse to the tenor nearest the option
        target = req.dte_days / 365.0
        keep = min(vm._T, key=lambda t: abs(t - target))
        vm = volmod.VolModel({keep: vm._series[keep]}, vm.skew_beta, label=vm.label + "+flatT")
    return vm, realized


def _coinflip_payload(daily, res, double_target):
    """Minimal backtest payload for the long-call coin-flip model (run_call_coinflip)."""
    pnls = [r["pnl"] for r in res.table]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = (sum(wins) / abs(sum(losses))) if losses else None
    return {
        "price": ser.series_xy(daily["Close"], MP),
        "entries": ser.entries_payload(res.trials),
        "equity": ser.list_xy(res.equity_dates, res.equity, MP),
        "cum_cost": ser.list_xy(res.equity_dates, res.cum_cost, MP),
        "table": res.table,
        "stats": {
            "n_trials": res.n_trials, "wins": res.wins, "empirical_p": res.empirical_p,
            "final_bank": res.final_bank, "max_drawdown": res.max_drawdown,
            "n_cycles": res.n_cycles, "total_cost": res.total_cost,
            "profit_factor": (round(pf, 3) if pf is not None else None),
            "double_target": double_target,
            "model": "long-call coin-flip (premium=bet, risk≤b)",
        },
    }


@app.post("/api/backtest/options")
def backtest_options(req: OptionsReq):
    daily, weekly, watr = _load(req.ticker, req.start, req.atr_period)
    if req.opt_model == "coinflip":
        # long-call coin-flip: premium is the bet (risk ≤ b), real per-date realized vol as IV
        realized = datamod.realized_vol(daily["Close"], req.iv_window)
        res = strat.run_call_coinflip(daily, weekly, watr, base_bet=req.base_bet,
                                      target_streak=req.target_streak, mult=req.mult,
                                      double_target=req.double_target, target_delta=req.target_delta,
                                      dte_days=req.dte_days, iv=req.iv_const, r=req.r,
                                      commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
                                      starting_bank=req.starting_bank, realized_vol=realized,
                                      iv_markup=req.iv_markup)
        if not res.table:
            raise HTTPException(status_code=422, detail="no cycles resolved for these params")
        return _coinflip_payload(daily, res, req.double_target)
    vm, realized = _build_vol(req, daily)
    # same campaign, but each lot is a delta-normalised long call (no -1ATR stop on the option;
    # the trailing stop caps risk at the initial b, convexity softens losses). IV from the
    # surface: real CBOE vol-index term structure interpolated to the tenor, + fixed-β skew.
    res = strat.run_campaign(daily, weekly, watr, base_bet=req.base_bet,
                             target_streak=req.target_streak, mult=req.mult,
                             instrument="calls", mode=req.mode, realized_vol=realized, r=req.r,
                             dte_days=req.dte_days, target_delta=req.target_delta,
                             commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
                             starting_bank=req.starting_bank, cap_mult=req.cap_mult,
                             roll_buffer_days=req.roll_buffer_days, vol_model=vm)
    if not res.table:
        raise HTTPException(status_code=422, detail="no campaigns resolved for these params")
    payload = _backtest_payload(daily, res, options=True)
    payload["stats"]["vol_model"] = vm.label
    payload["stats"]["skew_beta"] = vm.skew_beta
    payload["stats"]["vol_class"] = volmod.classify(req.ticker)
    return payload


# ----------------------------------------------------------------- tab 5: scan all instruments
def _campaign_summary(res, starting_bank: float) -> dict:
    """Bottom-line stats for one instrument's linear campaign, computed from the per-campaign
    P&L the same way the UI verdict does (sum of `table[].pnl`)."""
    pnls = [r["pnl"] for r in res.table]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    net = sum(pnls)
    win_sum, loss_sum = sum(wins), sum(losses)
    pf = (win_sum / abs(loss_sum)) if loss_sum else None      # None = no losing campaigns
    return {
        "n_campaigns": len(pnls), "wins": len(wins), "losses": len(losses),
        "net": round(net, 2),
        "ret_pct": round(100.0 * net / max(1e-9, starting_bank), 4),
        "profit_factor": (round(pf, 3) if pf is not None else None),
        "final_bank": round(res.final_bank, 2), "max_drawdown": round(res.max_drawdown, 2),
        "avg_win": round(win_sum / len(wins), 2) if wins else 0.0,
        "avg_loss": round(loss_sum / len(losses), 2) if losses else 0.0,
    }


def _detrend(daily: pd.DataFrame) -> pd.DataFrame:
    """Strip the net drift from a price series, keeping its volatility/intraweek shape intact.

    Remove the mean daily log-return from Close so the detrended path has ZERO net drift
    (a true fair coin), then scale Open/High/Low by the same per-day factor so intraday ranges
    are preserved. Running the strategy on this is the control: any profit here is NOT a
    directional edge (there is no direction) — it's the option-pricing/fill floor. The gap
    between the real result and this control is the part that's purely drift (regime-dependent).
    """
    import numpy as np
    close = daily["Close"].to_numpy(float)
    if len(close) < 3:
        return daily
    logret = np.diff(np.log(close))
    mu = logret.mean()
    new_close = np.empty_like(close)
    new_close[0] = close[0]
    new_close[1:] = close[0] * np.exp(np.cumsum(logret - mu))
    factor = new_close / close
    out = daily.copy()
    for col in ("Open", "High", "Low", "Close"):
        if col in out.columns:
            out[col] = out[col].to_numpy(float) * factor
    return out


def _shuffle_surrogate(daily: pd.DataFrame, seed: int, keep_drift: bool) -> pd.DataFrame:
    """IID surrogate: randomly permute the per-day bar shapes (logret + intraday hi/lo/open
    ratios in lockstep), destroying ALL serial structure (trends/momentum/vol-clustering) while
    keeping the exact marginal bar distribution. `keep_drift=False` also zeroes the mean log-return.

    This is the control the drift-strip should have been: a stop-and-pyramid on a driftless series
    with INDEPENDENT increments is EV≈0 by the martingale-free identity, so any profit that survives
    the shuffle is a fill/barrier artifact, not edge. Comparing base vs shuffle-with-drift vs
    shuffle-zero-drift cleanly splits net into trend / drift / floor without the detrend reversal artifact.
    """
    import numpy as np
    c = daily["Close"].to_numpy(float); o = daily["Open"].to_numpy(float)
    h = daily["High"].to_numpy(float); l = daily["Low"].to_numpy(float)
    if len(c) < 4:
        return daily
    g = np.diff(np.log(c))                                   # day-to-day log-return (len n-1)
    u = np.log(np.maximum(h[1:], c[1:]) / c[1:])             # intraday up-wick of each day
    dn = np.log(np.minimum(l[1:], c[1:]) / c[1:])            # intraday down-wick
    op = np.log(o[1:] / c[1:])                               # open vs close
    perm = np.random.default_rng(seed).permutation(len(g))
    gp = g[perm] - (0.0 if keep_drift else g.mean())
    nc = np.empty(len(c)); nc[0] = c[0]; nc[1:] = c[0] * np.exp(np.cumsum(gp))
    nh = nc.copy(); nl = nc.copy(); no = nc.copy()
    nh[1:] = nc[1:] * np.exp(u[perm]); nl[1:] = nc[1:] * np.exp(dn[perm]); no[1:] = nc[1:] * np.exp(op[perm])
    return pd.DataFrame({"Open": no, "High": np.maximum(nh, nc), "Low": np.minimum(nl, nc),
                         "Close": nc, "Volume": daily["Volume"].to_numpy()}, index=daily.index)


def _shuffle_net_stats(req: ScanReq, daily, seed0: int, keep_drift: bool, k: int):
    """Mean & sd of the strategy's net P&L over `k` IID shuffles. Returns (mean, sd) or (None,None)."""
    import numpy as np
    nets = []
    for s in range(k):
        try:
            sur = _shuffle_surrogate(daily, seed0 + s, keep_drift)
            w = datamod.weekly(sur); a = datamod.atr(w, req.atr_period)
            r = _scan_run(req, sur, w, a)
            nets.append(r.final_bank - req.starting_bank)
        except Exception:
            continue
    if not nets:
        return None, None
    arr = np.array(nets, dtype=float)
    return float(arr.mean()), float(arr.std())


def _coinflip_net(daily, weekly, watr, req, iv_markup: float) -> float:
    """Net P&L of the coin-flip on (daily,weekly,watr) at a given IV markup. Used for the
    breakeven-markup search; net is monotonically DECREASING in markup (pricier calls)."""
    realized = datamod.realized_vol(daily["Close"], req.iv_window)
    res = strat.run_call_coinflip(daily, weekly, watr, base_bet=req.base_bet,
                                  target_streak=req.target_streak, mult=req.mult,
                                  double_target=req.double_target, target_delta=req.target_delta,
                                  dte_days=req.dte_days, iv=0.20, r=req.r,
                                  commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
                                  starting_bank=req.starting_bank, realized_vol=realized,
                                  iv_markup=iv_markup)
    return res.final_bank - req.starting_bank


def _breakeven_markup(daily, weekly, watr, req, lo=0.5, hi=3.0, iters=6):
    """The IV markup at which this instrument's coin-flip net P&L = 0 (bisection; net is
    decreasing in markup). Returns (value_in[lo,hi], flag) where flag is '' if it crossed,
    'lo' if it loses even at the cheapest markup (be < lo ⇒ never profitable on real options),
    or 'hi' if it stays profitable even at the richest (be > hi ⇒ robust to pricing)."""
    nlo, nhi = _coinflip_net(daily, weekly, watr, req, lo), _coinflip_net(daily, weekly, watr, req, hi)
    if nlo <= 0:
        return lo, "lo"
    if nhi > 0:
        return hi, "hi"
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if _coinflip_net(daily, weekly, watr, req, mid) > 0:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 3), ""


def _scan_run(req: ScanReq, daily, weekly, watr):
    """Run the requested model on one instrument's prepared frames; returns the result."""
    if req.model == "coinflip":
        realized = datamod.realized_vol(daily["Close"], req.iv_window)
        return strat.run_call_coinflip(daily, weekly, watr, base_bet=req.base_bet,
                                       target_streak=req.target_streak, mult=req.mult,
                                       double_target=req.double_target, target_delta=req.target_delta,
                                       dte_days=req.dte_days, iv=0.20, r=req.r,
                                       commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
                                       starting_bank=req.starting_bank, realized_vol=realized,
                                       iv_markup=req.iv_markup)
    return strat.run_campaign(daily, weekly, watr, base_bet=req.base_bet,
                              target_streak=req.target_streak, mult=req.mult,
                              instrument="shares", mode=req.mode,
                              commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
                              starting_bank=req.starting_bank, cap_mult=req.cap_mult)


@app.post("/api/scan")
def scan_all(req: ScanReq):
    """One-click robustness sweep across the whole catalog with identical params.

    `model='shares'` runs the linear ATR pyramid; `model='coinflip'` runs the long-call
    coin-flip (premium = bet, real per-date IV × markup). Per-instrument bottom-line summary.
    Sequential by design — yfinance/Yahoo rate-limits a server IP (429), so we do NOT fan out.
    Per-ticker failures are captured (ok=False) instead of aborting the sweep.

    `stress=True` adds, per instrument, a DRIFT-STRIPPED control (same strategy on detrended,
    zero-drift prices) and — for coinflip — the breakeven IV markup. These separate a real
    structural edge from "we were long things that went up in a 20-year bull market".
    """
    rows = []
    for ticker, label, group in instruments.flat_with_group():
        try:
            daily, weekly, watr = _load(ticker, req.start, req.atr_period)
            res = _scan_run(req, daily, weekly, watr)
            if not res.table:
                rows.append({"ticker": ticker, "label": label, "group": group,
                             "ok": False, "error": "no cycles resolved"})
                continue
            row = {"ticker": ticker, "label": label, "group": group, "ok": True,
                   **_campaign_summary(res, req.starting_bank)}
            if req.stress:                                   # drift / trend / floor decomposition
                try:
                    bank = max(1e-9, req.starting_bank)
                    base_net = row["net"]
                    floor_m, floor_sd = _shuffle_net_stats(req, daily, 1000, False, req.shuffle_n)
                    driftk_m, _ = _shuffle_net_stats(req, daily, 5000, True, req.shuffle_n)
                    if floor_m is not None and driftk_m is not None:
                        # additive split: base = floor + drift + trend (telescopes exactly)
                        drift_part = driftk_m - floor_m          # IID-drift minus IID-zero-drift
                        trend_part = base_net - driftk_m         # real ordering on top of IID-drift
                        row["floor_ret_pct"] = round(100.0 * floor_m / bank, 2)
                        row["floor_sd_ret_pct"] = round(100.0 * floor_sd / bank, 2)
                        row["drift_ret_pct"] = round(100.0 * drift_part / bank, 2)
                        row["trend_ret_pct"] = round(100.0 * trend_part / bank, 2)
                        row["floor_net"] = round(floor_m, 2)
                    # naive detrend kept as a labelled REFERENCE (over-corrects on trends — see note)
                    dd = _detrend(daily); dw = datamod.weekly(dd); dwatr = datamod.atr(dw, req.atr_period)
                    row["detrend_ret_pct"] = round(
                        100.0 * (_scan_run(req, dd, dw, dwatr).final_bank - req.starting_bank) / bank, 2)
                    if req.model == "coinflip":
                        be, flag = _breakeven_markup(daily, weekly, watr, req)
                        row["be_markup"] = be
                        row["be_markup_flag"] = flag
                except Exception as ex:
                    row["control_error"] = f"{type(ex).__name__}: {ex}"
            rows.append(row)
        except HTTPException as ex:
            rows.append({"ticker": ticker, "label": label, "group": group,
                         "ok": False, "error": str(ex.detail)})
        except Exception as ex:                              # never let one ticker kill the sweep
            rows.append({"ticker": ticker, "label": label, "group": group,
                         "ok": False, "error": f"{type(ex).__name__}: {ex}"})

    ok = [r for r in rows if r["ok"]]
    profitable = [r for r in ok if r["net"] > 0]
    rets = sorted(r["ret_pct"] for r in ok)
    median_ret = rets[len(rets) // 2] if rets else 0.0
    summary = {
        "total": len(rows), "ok": len(ok), "failed": len(rows) - len(ok),
        "profitable": len(profitable),
        "profitable_pct": round(100.0 * len(profitable) / len(ok), 1) if ok else 0.0,
        "median_ret_pct": round(median_ret, 2),
        "mean_ret_pct": round(sum(rets) / len(rets), 2) if rets else 0.0,
        "best": max(ok, key=lambda r: r["ret_pct"], default=None),
        "worst": min(ok, key=lambda r: r["ret_pct"], default=None),
    }
    if req.stress:                                           # aggregate the drift/trend/floor split
        def _med(key):
            vals = sorted(r[key] for r in ok if r.get(key) is not None)
            return round(vals[len(vals) // 2], 2) if vals else None
        dec = [r for r in ok if "floor_ret_pct" in r]
        if dec:
            summary["floor_median_ret_pct"] = _med("floor_ret_pct")
            summary["drift_median_ret_pct"] = _med("drift_ret_pct")
            summary["trend_median_ret_pct"] = _med("trend_ret_pct")
            summary["floor_profitable_pct"] = round(
                100.0 * sum(1 for r in dec if r["floor_net"] > 0) / len(dec), 1)
            summary["shuffle_n"] = req.shuffle_n
        summary["detrend_median_ret_pct"] = _med("detrend_ret_pct")
        bes = sorted(r["be_markup"] for r in ok if r.get("be_markup") is not None)
        if bes:
            summary["be_markup_median"] = bes[len(bes) // 2]
    return {"params": req.model_dump(), "results": rows, "summary": summary}


# ----------------------------------------------------------------- tab 6: explain (step-by-step trace)
def _jsonable(events: list[dict]) -> list[dict]:
    """Coerce numpy scalars in the trace to plain Python so FastAPI can serialise them."""
    out = []
    for e in events:
        out.append({k: (float(v) if isinstance(v, (float, int)) and not isinstance(v, bool)
                        else v) for k, v in e.items()})
    return out


@app.post("/api/explain")
def explain(req: ExplainReq):
    """Step-by-step trace of the first campaign on a synthetic flat/up/down path.

    shares → the average-based pyramid (grid view); calls → the long-call COIN-FLIP
    (premium = the bet, risk ≤ b by construction, dynamic doubling target). The trace
    comes from the real engine so the money mechanics can be inspected directly.
    """
    daily = scenarios.scenario(req.scenario, atr_period=req.atr_period,
                               target_streak=req.target_streak)
    weekly = datamod.weekly(daily)
    watr = datamod.atr(weekly, req.atr_period)
    trace: list[dict] = []

    if req.instrument == "calls":
        res = strat.run_call_coinflip(daily, weekly, watr, base_bet=req.base_bet,
                                      target_streak=req.target_streak, mult=req.mult,
                                      double_target=req.double_target, target_delta=req.target_delta,
                                      dte_days=req.dte_days, iv=req.iv, r=0.045,
                                      starting_bank=10_000.0, trace=trace)
        camp1 = _jsonable([e for e in trace if e.get("camp") == 1])
        cf_exit = next((e for e in camp1 if e["t"] == "cf_exit"), None)
        end = (pd.Timestamp(cf_exit["date"]) + pd.Timedelta(days=10)) if cf_exit else daily.index[-1]
        win = daily.loc[daily.index <= end]
        return {
            "scenario": req.scenario, "b": req.base_bet, "instrument": "calls",
            "model": "coinflip", "double_target": req.double_target,
            "price": ser.series_xy(win["Close"], MP),
            "rounds": [e for e in camp1 if e["t"] == "cf_round"],
            "cf_exit": cf_exit, "trace": camp1, "table": res.table[:1],
        }

    res = strat.run_campaign(daily, weekly, watr, base_bet=req.base_bet,
                             target_streak=req.target_streak, mult=req.mult,
                             instrument="shares", mode="pyramid", starting_bank=10_000.0,
                             trace=trace)
    camp1 = _jsonable([e for e in trace if e.get("camp") == 1])
    entry = next((e for e in camp1 if e["t"] == "entry"), None)
    exit_ = next((e for e in camp1 if e["t"] == "exit"), None)
    rungs = []
    if entry:
        R0, h = entry["price"], entry["h"]
        rungs = [{"k": k, "level": round(R0 + k * h, 4)}
                 for k in range(0, req.target_streak + 1)]
    end = pd.Timestamp(exit_["date"]) + pd.Timedelta(days=7) if exit_ else daily.index[-1]
    win = daily.loc[daily.index <= end]
    return {
        "scenario": req.scenario, "b": req.base_bet, "instrument": "shares", "model": "grid",
        "price": ser.series_xy(win["Close"], MP),
        "high": ser.series_xy(win["High"], MP),
        "low": ser.series_xy(win["Low"], MP),
        "trace": camp1, "rungs": rungs, "entry": entry, "exit": exit_,
        "table": res.table[:1],
    }


@app.post("/api/inspect")
def inspect(req: InspectReq):
    """Run the engine on a REAL instrument over a chosen window with full tracing, returning
    ALL campaigns' events so the UI can give a window overview and drill into any one campaign
    (entry / scale-in / exit detail) exactly like the Explain tab, but on real data."""
    try:
        daily = datamod.fetch(req.ticker, start=req.start, end=req.end)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"data fetch failed: {ex}")
    daily = daily.loc[daily.index >= pd.Timestamp(req.start)]
    if req.end:
        daily = daily.loc[daily.index <= pd.Timestamp(req.end)]
    if daily.empty or len(daily) < req.atr_period * 7:
        raise HTTPException(status_code=422, detail="not enough data in this window for the ATR period")
    weekly = datamod.weekly(daily)
    watr = datamod.atr(weekly, req.atr_period)
    trace: list[dict] = []

    if req.model == "coinflip":
        realized = datamod.realized_vol(daily["Close"], req.iv_window)
        res = strat.run_call_coinflip(daily, weekly, watr, base_bet=req.base_bet,
                                      target_streak=req.target_streak, mult=req.mult,
                                      double_target=req.double_target, target_delta=req.target_delta,
                                      dte_days=req.dte_days, iv=0.20, r=req.r,
                                      commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
                                      starting_bank=req.starting_bank, realized_vol=realized,
                                      iv_markup=req.iv_markup, trace=trace)
        model, instrument = "coinflip", "calls"
    elif req.model == "calls":
        # pyramid of delta-normalised long calls WITH auto-roll near expiry — the model whose
        # roll mechanic is otherwise invisible. Uses realized vol as IV (no surface), like coinflip.
        realized = datamod.realized_vol(daily["Close"], req.iv_window)
        res = strat.run_campaign(daily, weekly, watr, base_bet=req.base_bet,
                                 target_streak=req.target_streak, mult=req.mult,
                                 instrument="calls", mode=req.mode, realized_vol=realized, r=req.r,
                                 dte_days=req.dte_days, target_delta=req.target_delta,
                                 commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
                                 starting_bank=req.starting_bank, cap_mult=req.cap_mult,
                                 roll_buffer_days=req.roll_buffer_days, trace=trace)
        model, instrument = "grid", "calls"
    else:
        res = strat.run_campaign(daily, weekly, watr, base_bet=req.base_bet,
                                 target_streak=req.target_streak, mult=req.mult,
                                 instrument="shares", mode=req.mode,
                                 commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
                                 starting_bank=req.starting_bank, cap_mult=req.cap_mult, trace=trace)
        model, instrument = "grid", "shares"
    if not res.table:
        raise HTTPException(status_code=422, detail="no campaigns resolved in this window")
    return {
        "ticker": req.ticker, "model": model, "instrument": instrument, "b": req.base_bet,
        "target_streak": req.target_streak, "double_target": req.double_target,
        "roll_buffer_days": req.roll_buffer_days,
        "final_bank": res.final_bank, "n_cycles": res.n_cycles,
        "price": ser.series_xy(daily["Close"], MP),
        "high": ser.series_xy(daily["High"], MP),
        "low": ser.series_xy(daily["Low"], MP),
        "trace": _jsonable(trace), "table": res.table,
    }


# ----------------------------------------------------------------- tab 8: hedged intraday (ПИ)
def _intraday_feed(req):
    """Fetch the intraday scalp feed per req.scalp_data. Returns the intraday DataFrame, or None
    to fall back to the daily bar (not requested / fetch failed / wrong asset class).

    - 'daily'  → None (one OHLC bar/day; the scalp is unmeasured).
    - '1m'     → FREE deep 1-minute crypto bars from Binance public REST (keyless; crypto only —
                 ETH/BTC/SOL, the doctrine's ideal instrument). Non-crypto tickers fall back.
                 1m comes from the bulk monthly dumps (deep history); window capped to the last
                 ANTIMG_HI_1M_DAYS days (default 730), earlier days use the daily bar.
    - 'hourly' → yfinance 60m bars (~730d) for any ticker; start clamped to the 725d cutoff.
    """
    mode = getattr(req, "scalp_data", "daily")
    if mode == "daily":
        return None
    if mode == "1m":
        # 1-min now comes from the BULK monthly dumps (data.binance.vision) so DEEP history is
        # feasible — a historical/multi-year window finally gets real 1m, not a daily fallback.
        # Still cap how far back we fetch 1m (the engine walks every bar → 1y ≈ 525k bars): the
        # last ANTIMG_HI_1M_DAYS days of the window use 1m, earlier days fall back to the daily bar.
        # Default 730d (≈2y, ~1M bars); raise it for deeper 1m at the cost of a slower run.
        win = int(os.environ.get("ANTIMG_HI_1M_DAYS", "730"))
        # cap is relative to the WINDOW END (not today) so a HISTORICAL backtest gets 1m for the last
        # `win` days of ITS OWN window, not "last win days from today" (which would exclude old windows).
        end = getattr(req, "end", None) or pd.Timestamp.now().normalize().date().isoformat()
        cutoff = (pd.Timestamp(end) - pd.Timedelta(days=win)).date().isoformat()
        start = max(req.start, cutoff)
        try:
            df = datamod.fetch_intraday_crypto(req.ticker, "1m", start=start,
                                               end=getattr(req, "end", None))
            return df if df is not None and not df.empty else None
        except Exception:
            return None                                   # non-crypto / geo-blocked → daily bar
    # 'hourly': yfinance refuses hourly older than ~730d — clamp the start so the request succeeds;
    # days before the cutoff fall back to the daily bar (partial intraday coverage on the recent part).
    cutoff = (pd.Timestamp.now().normalize() - pd.Timedelta(days=725)).date().isoformat()
    start = max(req.start, cutoff)
    try:
        df = datamod.fetch_intraday(req.ticker, "60m", start=start, end=getattr(req, "end", None))
        return df if df is not None and not df.empty else None
    except Exception:
        return None


def _run_hi(daily, datr, vm, realized, req, trace=None, intraday=None):
    """Call the ПИ engine with the knobs from a HedgedIntradayReq or HedgedIntradayScanReq
    (they share field names). Returns the HedgedIntradayResult."""
    return hi.run_hedged_intraday(
        daily, datr, starting_bank=req.starting_bank, risk_pct=req.risk_pct,
        dte_days=req.dte_days, roll_buffer_days=req.roll_buffer_days,
        roll_profit_pct=getattr(req, "roll_profit_pct", 0.0), r=req.r,
        n_parts=req.n_parts, grid_atr_frac=req.grid_atr_frac, grid_mult=req.grid_mult,
        intraday_frac=req.intraday_frac, scalp_model=req.scalp_model,
        scalp_k=getattr(req, "scalp_k", hi.SCALP_K_DEFAULT),
        scalp_capture=getattr(req, "scalp_capture", 0.5),
        scalp_recenter_days=req.scalp_recenter_days, use_bbands=req.use_bbands,
        bb_window=req.bb_window, bb_k=req.bb_k,
        scalp_efficiency=req.scalp_efficiency, max_rt_per_day=req.max_rt_per_day,
        stuck_penalty=req.stuck_penalty, commission_pct=req.commission_pct,
        slippage_pct=req.slippage_pct, vol_model=vm, realized_vol=realized,
        intraday=intraday, trace=trace)


def _coinflip_projection(res, assumed_capture: float) -> dict:
    """Reduce a ПИ run to the corpus's profitability primitives + the "0.6-vs-0.45" coin-flip read.

    The straddle theta and the per-trade scalp income both scale with realized vol (∝ σ·S) once the
    book is sized to a fixed risk budget, so the **coverage ratio** (scalp income ÷ theta) is roughly
    vol-INVARIANT — governed by trades/month × capture fraction, not by the instrument's vol. That is
    why a capture fraction measured on a 1-min crypto feed projects onto any asset: vol cancels.

    • `coverage` ≥ 1 ⇒ the scalp pays the theta with no trend at all = a "0.6-type" (winning) strategy.
    • `breakeven_capture` (φ*) = the capture at which coverage = 1; catch more than φ* ⇒ profitable.
    • `coverage_at_assumed` = coverage rescaled to a chosen capture (scalp income ∝ capture).
    • `period_win_rate` = the empirical coin-flip p (fraction of straddle periods that finished green).
    """
    cap = res.capture_fraction
    cov = res.coverage_ratio
    cov_assumed = (cov * (assumed_capture / cap)) if cap > 1e-9 else None
    # the "type": coverage≥1 means flat markets alone already pay the theta (a positive-EV coin flip);
    # <1 means flat markets bleed and you NEED a trend (the gamma) to come out ahead.
    if cov >= 1.0:
        flip_type = "0.6-type (скальп платит тету сам)"
    elif cov >= 0.5:
        flip_type = "≈0.5 (скальп платит часть, нужен тренд)"
    else:
        flip_type = "0.45-type (флет кровит, держится на тренде/гамме)"
    return {
        "trades_per_month": round(res.trades_per_month, 1),
        "trades_per_month_target": "200–250",           # doctrine, for a loaded book on 1m
        "profit_per_trade": round(res.profit_per_trade, 2),
        "capture_fraction": round(cap, 4),
        "theta_per_month": round(res.theta_per_month, 2),
        "scalp_per_month": round(res.scalp_per_month, 2),
        "coverage_ratio": round(cov, 3),
        "breakeven_capture": round(res.breakeven_capture, 4),
        "assumed_capture": round(assumed_capture, 4),
        "coverage_at_assumed": (round(cov_assumed, 3) if cov_assumed is not None else None),
        "period_win_rate": round(res.period_win_rate, 3),
        "flip_type": flip_type,
        "scalp_avail_pts": round(res.scalp_avail_pts, 2),
        "scalp_harvest_pts": round(res.scalp_harvest_pts, 2),
    }


def _hi_summary(res, starting_bank: float) -> dict:
    """Per-instrument bottom line for the ПИ scan (one row)."""
    net = res.final_bank - starting_bank
    return {
        "net": round(net, 2),
        "ret_pct": round(100.0 * net / max(1e-9, starting_bank), 2),
        "cagr_pct": round(res.ann_return_pct, 2),
        "straddle_pnl": round(res.straddle_pnl, 2),
        "scalp_pnl": round(res.scalp_pnl, 2),
        "scalp_cover_pct": round(res.scalp_covers_theta_pct, 1),
        "n_rolls": res.n_rolls, "n_days": res.n_days, "years": round(res.years, 2),
        "max_drawdown": round(res.max_drawdown, 2),
        "worst_period_pnl": round(res.worst_period_pnl, 2),
        "max_premium_at_risk": round(res.max_premium_at_risk, 2),
        "scalp_round_trips": res.scalp_round_trips,
        "trades_per_month": round(res.trades_per_month, 1),
        "capture_fraction": round(res.capture_fraction, 4),
        "coverage_ratio": round(res.coverage_ratio, 3),
        "period_win_rate": round(res.period_win_rate, 3),
        # the straddle loss cap holds if the worst single period never lost more than its premium
        "loss_cap_ok": bool(res.worst_period_pnl >= -res.max_premium_at_risk - 1e-6),
    }


@app.post("/api/hedged-intraday")
def hedged_intraday(req: HedgedIntradayReq):
    """Прикрытый Интрадей (Korovin): a long synthetic straddle (2 ATM calls − 1 future) whose
    theta is paid by a counter-trend intraday scalping grid. Daily-bar backtest — the straddle
    is BS-marked daily and rolled ATM near expiry; the scalp overlay harvests the reversed part
    of each day's range. Returns separated straddle / scalp / total streams (judge by the total).
    """
    try:
        daily = datamod.fetch(req.ticker, start=req.start, end=req.end)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"data fetch failed: {ex}")
    daily = daily.loc[daily.index >= pd.Timestamp(req.start)]
    if req.end:
        daily = daily.loc[daily.index <= pd.Timestamp(req.end)]
    if daily.empty or len(daily) < req.atr_period * 3:
        raise HTTPException(status_code=422, detail="not enough data in this window for the ATR period")
    datr = datamod.atr_on_timeframe(daily, req.grid_timeframe, req.atr_period)  # grid-step ATR (daily/weekly/monthly)
    vm, realized = _build_vol(req, daily)
    res = _run_hi(daily, datr, vm, realized, req, intraday=_intraday_feed(req))
    if not res.table:
        raise HTTPException(status_code=422, detail="no straddle periods resolved for these params")
    rolls = res.rolls
    return {
        "ticker": req.ticker, "vol_model": vm.label, "vol_class": volmod.classify(req.ticker),
        "price": ser.series_xy(daily["Close"], MP),
        "equity_total": ser.list_xy(res.equity_dates, res.equity_total, MP),
        "equity_straddle": ser.list_xy(res.equity_dates, res.equity_straddle, MP),
        "equity_scalp": ser.list_xy(res.equity_dates, res.equity_scalp, MP),
        "theta_path": ser.list_xy(res.equity_dates, res.theta_path, MP),
        "rolls": {"x": [rr["date"] for rr in rolls], "y": [rr["spot"] for rr in rolls]},
        "table": res.table,
        "stats": {
            "final_bank": round(res.final_bank, 2),
            "net_pnl": round(res.final_bank - res.starting_bank, 2),
            "straddle_pnl": round(res.straddle_pnl, 2),
            "scalp_pnl": round(res.scalp_pnl, 2),
            "total_theta": round(res.total_theta, 2),
            "scalp_covers_theta_pct": round(res.scalp_covers_theta_pct, 1),
            "gamma_dir_pnl": round(res.gamma_dir_pnl, 2),
            "breakeven_scalp_cover_pct": round(res.breakeven_scalp_cover_pct, 1),
            "ann_return_pct": round(res.ann_return_pct, 2),
            "years": round(res.years, 2),
            "n_rolls": res.n_rolls, "n_days": res.n_days,
            "max_drawdown": round(res.max_drawdown, 2),
            "worst_period_pnl": round(res.worst_period_pnl, 2),
            "max_premium_at_risk": round(res.max_premium_at_risk, 2),
            "total_cost": round(res.total_cost, 2),
            "scalp_model": res.scalp_model, "scalp_round_trips": res.scalp_round_trips,
            "scalp_heals": res.scalp_heals, "confident_flat_days": res.confident_flat_days,
            "scalp_scaled_max": res.scalp_scaled_max, "n_parts": req.n_parts, "intraday_bars": res.intraday_bars,
            "scalp_data": getattr(req, "scalp_data", "daily"),
            "starting_bank": res.starting_bank,
            "roll_profit_pct": getattr(req, "roll_profit_pct", 0.0),
            "profit_rolls": sum(1 for x in res.rolls if x.get("reason") == "профит-цель"),
            "grid_timeframe": req.grid_timeframe, "use_bbands": req.use_bbands,
            "vol_model": vm.label, "vol_class": volmod.classify(req.ticker),
            "coinflip": _coinflip_projection(res, req.assumed_capture),
        },
        "use_bbands": req.use_bbands,
    }


@app.post("/api/hedged-intraday/attribution")
def hedged_intraday_attribution(req: HedgedIntradayReq):
    """The MATHEMATICAL MODEL: decompose the ПИ P&L into theta (cost), gamma (trend), scalp (flat) and
    CONCLUDE which part builds which part of the profit. Runs the backtest for the MEASURED streams,
    then fits the closed-form model (a=ρB/2T, Σ=C_s·ρB·vr, Γ=a·vr²·g) — theta & scalp from first
    principles, the gamma-capture g calibrated to the run — so the closed-form reproduces the backtest
    and exposes the vol-dependence (Γ∝vr² convex/trend, Σ∝vr linear/flat, Θ=−a constant cost)."""
    try:
        daily = datamod.fetch(req.ticker, start=req.start, end=req.end)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"data fetch failed: {ex}")
    daily = daily.loc[daily.index >= pd.Timestamp(req.start)]
    if req.end:
        daily = daily.loc[daily.index <= pd.Timestamp(req.end)]
    if daily.empty or len(daily) < req.atr_period * 3:
        raise HTTPException(status_code=422, detail="not enough data in this window for the ATR period")
    datr = datamod.atr_on_timeframe(daily, req.grid_timeframe, req.atr_period)
    vm, realized = _build_vol(req, daily)
    res = _run_hi(daily, datr, vm, realized, req, intraday=_intraday_feed(req))
    if not res.table:
        raise HTTPException(status_code=422, detail="no straddle periods resolved for these params")
    # vol state: σ_I = mean ATM IV actually paid across the rolled straddles; σ_R = mean realized vol
    ivs = [row["iv"] for row in res.table if row.get("iv")]
    sig_I = (sum(ivs) / len(ivs)) if ivs else req.iv_const
    rv_clean = realized.dropna() if realized is not None else None
    sig_R = float(rv_clean.mean()) if rv_clean is not None and not rv_clean.empty else sig_I
    yrs = max(res.years, 1e-6)
    T = req.dte_days / 365.0
    # MEASURED decomposition (the real backtest) → the attribution / conclusion rests on these
    measured = pim.attribute_measured(res.total_theta, res.gamma_dir_pnl, res.scalp_pnl, dte_years=T)
    # CLOSED-FORM model that reproduces it (g calibrated to the measured gamma leg)
    g = pim.calibrate_gamma_capture(res.gamma_dir_pnl, req.starting_bank, req.risk_pct, T, sig_I, sig_R, yrs)
    cf = pim.closed_form(req.starting_bank, req.risk_pct, T, sig_I, sig_R,
                         scalp_k=getattr(req, "scalp_k", hi.SCALP_K_DEFAULT),
                         intraday_frac=req.intraday_frac, gamma_capture=g, years=yrs)
    def _dump(a):
        return {"theta": round(float(a.theta), 2), "gamma_trend": round(float(a.gamma_trend), 2),
                "scalp_flat": round(float(a.scalp_flat), 2), "total": round(float(a.total), 2),
                "pct_from_trend": round(float(a.pct_from_trend), 1),
                "pct_from_flat": round(float(a.pct_from_flat), 1),
                "profitable": bool(a.profitable), "regime": a.regime, "conclusion": a.conclusion}
    return {
        "ticker": req.ticker, "scalp_model": res.scalp_model,
        "state": {"sigma_implied": round(sig_I, 4), "sigma_realized": round(sig_R, 4),
                  "vr": round(sig_R / max(sig_I, 1e-9), 3), "years": round(yrs, 2),
                  "bank": req.starting_bank, "risk_pct": req.risk_pct, "dte_years": round(T, 3)},
        "model_params": {"a_theta_rate": round(cf.a, 2), "c_s": round(cf.c_s, 4),
                         "gamma_capture_g": round(g, 3),
                         "profitable_condition": round(cf.profitable_condition, 3),
                         "scalp_k": getattr(req, "scalp_k", hi.SCALP_K_DEFAULT)},
        "measured": _dump(measured),         # from the backtest streams (theta/gamma_dir/scalp)
        "closed_form": _dump(cf),            # the equations, reproducing the backtest
    }


@app.post("/api/hedged-intraday/extrapolate")
def hedged_intraday_extrapolate(req: HedgedIntradayScanReq):
    """EXTRAPOLATE the P&L attribution across the WHOLE catalog — fully predictive, NO per-instrument
    backtest. For each instrument we read only DATA: σ_I (mean ATM IV from the vol surface), σ_R (mean
    realized vol), and the variance ratio VR(63) (trend vs mean-reversion). Then the closed-form model
    (pi_model) gives the annual decomposition:
        Θ=−a (a=ρB/2T) · Γ=a·vr²·g  with g = VR/(VR+1) (TREND fraction) · Σ=C_s·ρB·vr with K from VR.
    The crypto 1m feed anchored the scalp constant K; g is validated against the daily-faithful straddle
    gamma (corr ≈ 0.4). ⚠ Gamma/g leg is well-grounded; the scalp/K leg's MAGNITUDE is the rough one
    (intraday edge only truly measurable on crypto). Returns a ranked table + aggregate."""
    rows = []
    T = req.dte_days / 365.0
    capture = getattr(req, "scalp_capture", instruments.CAPTURE_DEFAULT)
    capture_mode = getattr(req, "capture_mode", "flat")
    use_preset = capture_mode == "preset"
    def _fin(x, nd=0):
        try:
            x = float(x)
            return round(x, nd) if (x == x and abs(x) != float("inf")) else 0.0   # NaN/Inf → 0
        except Exception:
            return 0.0
    # force the simple, positive-only capture scalp so EVERY instrument is estimated from its REAL
    # daily ranges (theta + straddle gamma stay exact from the real path; scalp = capture×range×lots,
    # only wins — losers carried & hedged by the straddle). This is the direct "we caught X% of the
    # daily move over history" estimate, not a σ/edge proxy.
    req.scalp_model = "capture"
    for ticker, label, group in instruments.flat_with_group():
        # per-CLASS capture preset (rangy commodities/crypto ↑, trend-prone equity/vol ↓) — a SCENARIO,
        # see instruments.CAPTURE_PRESET. 'flat' mode uses the single scalp_capture for every instrument.
        cap_i = instruments.capture_preset(group) if use_preset else capture
        req.scalp_capture = cap_i
        try:
            daily = datamod.fetch(ticker, start=req.start)
            daily = daily.loc[daily.index >= pd.Timestamp(req.start)]
            if daily.empty or len(daily) < max(200, req.atr_period * 3):
                rows.append({"ticker": ticker, "label": label, "group": group, "ok": False,
                             "error": "insufficient data"})
                continue
            datr = datamod.atr_on_timeframe(daily, req.grid_timeframe, req.atr_period)
            vm, realized = _build_vol(req, daily, ticker=ticker)
            res = _run_hi(daily, datr, vm, realized, req)
            if not res.table:
                rows.append({"ticker": ticker, "label": label, "group": group, "ok": False,
                             "error": "no straddle periods"})
                continue
            yrs = max(res.years, 1e-6)
            # attribution on the REAL streams (theta + straddle gamma faithful; scalp = positive capture)
            att = pim.attribute_measured(res.total_theta, res.gamma_dir_pnl, res.scalp_pnl, dte_years=T)
            net = res.final_bank - res.starting_bank
            sR = float(realized.dropna().mean()) if realized is not None and not realized.dropna().empty else 0.0
            # Report everything as a % OF THE THETA (the rent) — compounding-INVARIANT, so the absurd
            # crypto bank-compounding cancels and the numbers stay interpretable: "the scalp pays X% of
            # the rent, the gamma Y%, net = X+Y−100%". Plus geometric CAGR for the $ feel.
            th = abs(res.total_theta) if abs(res.total_theta) > 1e-9 else 1e-9
            scalp_cover = 100.0 * res.scalp_pnl / th
            gamma_cover = 100.0 * res.gamma_dir_pnl / th
            rows.append({
                "ticker": ticker, "label": label, "group": group, "ok": True,
                "capture": round(cap_i, 3),                 # the per-class (or flat) capture used here
                "sigma_R": _fin(sR, 3),
                "scalp_cover_pct": _fin(scalp_cover),       # scalp ÷ |theta| — the flat leg pays this % of rent
                "gamma_cover_pct": _fin(gamma_cover),       # gamma ÷ |theta| — the trend leg pays this %
                "net_cover_pct": _fin(scalp_cover + gamma_cover - 100.0),   # net profit as % of the rent
                "cagr_pct": _fin(res.ann_return_pct, 1),
                "pct_from_trend": _fin(att.pct_from_trend),
                "pct_from_flat": _fin(att.pct_from_flat),
                "win_rate": _fin(res.period_win_rate, 3),   # coin-flip p: fraction of straddle periods green
                "n_periods": len(res.table),
                "regime": att.regime, "profitable": bool(net > 0)})
        except Exception as ex:
            rows.append({"ticker": ticker, "label": label, "group": group, "ok": False,
                         "error": str(ex)[:80]})
    ok = [r for r in rows if r.get("ok")]
    ok.sort(key=lambda r: r["net_cover_pct"], reverse=True)
    n = len(ok)
    med = lambda key: round(sorted(r[key] for r in ok)[n // 2], 1) if n else 0.0
    agg = {
        "n": n, "n_failed": len(rows) - n, "capture": capture, "dte_years": round(T, 3),
        "capture_mode": capture_mode,
        "capture_range": [round(min((r["capture"] for r in ok), default=0.0), 3),
                          round(max((r["capture"] for r in ok), default=0.0), 3)] if use_preset else None,
        "n_profitable": sum(1 for r in ok if r["profitable"]),
        "n_trend_built": sum(1 for r in ok if r["regime"] == "trend-built (gamma)"),
        "n_flat_built": sum(1 for r in ok if r["regime"] == "flat-built (scalp)"),
        "n_bleeding": sum(1 for r in ok if r["regime"] == "bleeding (theta wins)"),
        "median_scalp_cover_pct": med("scalp_cover_pct"),
        "median_net_cover_pct": med("net_cover_pct"),
        # the coin-flip reduction: p = win-rate of straddle periods (>0.5 ⇒ "0.6-type" edge)
        "median_win_rate": med("win_rate"),
        "n_p_above_half": sum(1 for r in ok if r["win_rate"] > 0.5),
    }
    return {"rows": ok, "aggregate": agg}


@app.post("/api/hedged-intraday/inspect")
def hedged_intraday_inspect(req: HedgedIntradayReq):
    """Watch the ПИ strategy EXECUTE over a chosen window: price + Bollinger flat-band, the ATM
    straddle strike (step line), every counter-trend scalp entry/exit, rolls, and the P&L
    decomposition — so the rules (don't fade a breakout, carry stuck parts, straddle runs the
    trend) can be audited visually. Use a short window (e.g. 3 months) to see each trade."""
    try:
        daily = datamod.fetch(req.ticker, start=req.start, end=req.end)
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"data fetch failed: {ex}")
    daily = daily.loc[daily.index >= pd.Timestamp(req.start)]
    if req.end:
        daily = daily.loc[daily.index <= pd.Timestamp(req.end)]
    if daily.empty or len(daily) < max(req.atr_period, req.bb_window) + 2:
        raise HTTPException(status_code=422, detail="not enough data in this window")
    datr = datamod.atr_on_timeframe(daily, req.grid_timeframe, req.atr_period)
    vm, realized = _build_vol(req, daily)
    trace: list = []
    res = _run_hi(daily, datr, vm, realized, req, trace=trace, intraday=_intraday_feed(req))
    if not res.table:
        raise HTTPException(status_code=422, detail="no straddle period resolved in this window")
    opens = [e for e in trace if e["t"] == "scalp_open"]
    closes = [e for e in trace if e["t"] == "scalp_close"]
    heals = [e for e in trace if e["t"] == "scalp_heal"]
    cflat = [e for e in trace if e["t"] == "confident_flat"]
    setups = [e for e in trace if e["t"] == "grid_setup"]
    g0 = setups[0] if setups else None                   # the n_parts working-part levels (first period)
    # ── per-part scalp LEDGER: every entry/exit in order, with a running cumulative scalp P&L ──
    ledger, cum, per_part, open_parts = [], 0.0, {}, []
    def _open_str():                                      # current open position, e.g. "ч.1+ч.2"
        return "+".join(f"ч.{p}" for p in sorted(set(open_parts))) if open_parts else "—"
    for e in trace:
        if e["t"] == "scalp_open":
            open_parts.append(e["part"])
            ledger.append({"date": e["date"], "kind": "вход", "part": e["part"], "side": e["side"],
                           "price": e["price"], "lots": e["lots"], "pnl": 0.0, "cum": round(cum, 2),
                           "streak": e.get("streak", 0), "conf_flat": e.get("conf_flat", False),
                           "scale": e.get("scale", 1.0), "open": _open_str()})
        elif e["t"] == "scalp_close":
            cum += e["pnl"]
            if e["part"] in open_parts:
                open_parts.remove(e["part"])
            ledger.append({"date": e["date"], "kind": "выход", "part": e["part"], "side": e["side"],
                           "price": e["exit"], "lots": e["lots"], "pnl": e["pnl"], "cum": round(cum, 2),
                           "streak": e.get("streak", 0), "conf_flat": e.get("conf_flat", False),
                           "scale": e.get("scale", 1.0), "open": _open_str()})
            pp = per_part.setdefault(e["part"], {"part": e["part"], "round_trips": 0, "pnl": 0.0})
            pp["round_trips"] += 1; pp["pnl"] = round(pp["pnl"] + e["pnl"], 2)
    ledger_full = len(ledger)
    if len(ledger) > 4000:                                # anti-bloat for very long windows
        ledger = ledger[:4000]
    per_part = [per_part[k] for k in sorted(per_part)]
    mid = daily["Close"].rolling(req.bb_window).mean()
    sd = daily["Close"].rolling(req.bb_window).std()
    ub_s = mid + req.bb_k * sd
    lb_s = mid - req.bb_k * sd
    # TREND regime spans = contiguous dates where price is OUTSIDE the Bollinger band (here the
    # grid STEPS ASIDE — no new counter-trend entries — and lets the straddle run). Inside = FLAT.
    out = ((daily["Close"] > ub_s) | (daily["Close"] < lb_s)).fillna(False).to_numpy()
    di = [d.isoformat() for d in daily.index]
    trend_spans, i0 = [], None
    for k, flag in enumerate(out):
        if flag and i0 is None:
            i0 = k
        elif not flag and i0 is not None:
            trend_spans.append({"x0": di[i0], "x1": di[k]}); i0 = None
    if i0 is not None:
        trend_spans.append({"x0": di[i0], "x1": di[-1]})
    # straddle strike as a step line over each period (open→close at its strike)
    strike_x, strike_y = [], []
    for row in res.table:
        strike_x += [row["open"], row["close"]]
        strike_y += [row["strike"], row["strike"]]
    sh = [e for e in opens if e["side"] == "short"]
    lo = [e for e in opens if e["side"] == "long"]
    return {
        "ticker": req.ticker, "use_bbands": req.use_bbands,
        "price": ser.series_xy(daily["Close"], MP),
        "bb_upper": ser.series_xy(mid + req.bb_k * sd, MP),
        "bb_lower": ser.series_xy(mid - req.bb_k * sd, MP),
        "strike": {"x": strike_x, "y": strike_y},
        "scalp_short": {"x": [e["date"] for e in sh], "y": [e["price"] for e in sh]},
        "scalp_long": {"x": [e["date"] for e in lo], "y": [e["price"] for e in lo]},
        "scalp_close": {"x": [e["date"] for e in closes], "y": [e["exit"] for e in closes],
                        "pnl": [e["pnl"] for e in closes]},
        "heals": {"x": [e["date"] for e in heals], "y": [e["spot"] for e in heals]},
        "confident_flat": {"x": [e["date"] for e in cflat]},
        "grid_levels": {"sell": g0["sell"], "buy": g0["buy"], "center": g0["center"],
                        "part_lots": g0["part_lots"]} if g0 else None,
        "n_parts": req.n_parts,
        "ledger": ledger, "ledger_full": ledger_full, "per_part": per_part,
        "trend_spans": trend_spans,
        "rolls": {"x": [rr["date"] for rr in res.rolls], "y": [rr["spot"] for rr in res.rolls]},
        "equity_total": ser.list_xy(res.equity_dates, res.equity_total, MP),
        "equity_straddle": ser.list_xy(res.equity_dates, res.equity_straddle, MP),
        "equity_scalp": ser.list_xy(res.equity_dates, res.equity_scalp, MP),
        "stats": {
            "net_pnl": round(res.final_bank - res.starting_bank, 2),
            "straddle_pnl": round(res.straddle_pnl, 2), "scalp_pnl": round(res.scalp_pnl, 2),
            "gamma_dir_pnl": round(res.gamma_dir_pnl, 2), "total_theta": round(res.total_theta, 2),
            "scalp_round_trips": res.scalp_round_trips, "n_rolls": res.n_rolls,
            "scalp_opens": len(opens), "scalp_stuck_at_end": len(opens) - len(closes),
            "scalp_heals": res.scalp_heals, "confident_flat_days": res.confident_flat_days,
            "scalp_scaled_max": res.scalp_scaled_max, "trend_days": int(out.sum()), "intraday_bars": res.intraday_bars,
            "scalp_data": getattr(req, "scalp_data", "daily"),
            "starting_bank": res.starting_bank,
            "roll_profit_pct": getattr(req, "roll_profit_pct", 0.0),
            "profit_rolls": sum(1 for x in res.rolls if x.get("reason") == "профит-цель"),
            "ann_return_pct": round(res.ann_return_pct, 2), "n_days": res.n_days,
            "vol_model": vm.label,
            "coinflip": _coinflip_projection(res, req.assumed_capture),
        },
    }


@app.post("/api/hedged-intraday/scan")
def hedged_intraday_scan(req: HedgedIntradayScanReq):
    """Bulk ПИ backtest across the WHOLE catalog with identical params — to see on which
    instruments the synthetic straddle + scalping holds up (the corpus flags silver/ETH as the
    volatile sweet spot, gold as the beginner pick). Sequential (Yahoo 429); per-ticker failures
    are captured, not fatal. Heavier than the shares scan (daily BS reprice per instrument)."""
    rows = []
    for ticker, label, group in instruments.flat_with_group():
        try:
            daily = datamod.fetch(ticker, start=req.start)
            daily = daily.loc[daily.index >= pd.Timestamp(req.start)]
            if daily.empty or len(daily) < req.atr_period * 3:
                rows.append({"ticker": ticker, "label": label, "group": group,
                             "ok": False, "error": "not enough data"})
                continue
            datr = datamod.atr_on_timeframe(daily, req.grid_timeframe, req.atr_period)
            vm, realized = _build_vol(req, daily, ticker=ticker)
            res = _run_hi(daily, datr, vm, realized, req)
            if not res.table:
                rows.append({"ticker": ticker, "label": label, "group": group,
                             "ok": False, "error": "no straddle periods resolved"})
                continue
            rows.append({"ticker": ticker, "label": label, "group": group, "ok": True,
                         "vol_model": vm.label, **_hi_summary(res, req.starting_bank)})
        except Exception as ex:                              # never let one ticker kill the sweep
            rows.append({"ticker": ticker, "label": label, "group": group,
                         "ok": False, "error": f"{type(ex).__name__}: {ex}"})

    ok = [r for r in rows if r["ok"]]
    profitable = [r for r in ok if r["net"] > 0]
    cagrs = sorted(r["cagr_pct"] for r in ok)
    covers = sorted(r["scalp_cover_pct"] for r in ok)
    median = lambda xs: xs[len(xs) // 2] if xs else 0.0
    summary = {
        "total": len(rows), "ok": len(ok), "failed": len(rows) - len(ok),
        "profitable": len(profitable),
        "profitable_pct": round(100.0 * len(profitable) / len(ok), 1) if ok else 0.0,
        "median_cagr_pct": round(median(cagrs), 2),
        "mean_cagr_pct": round(sum(cagrs) / len(cagrs), 2) if cagrs else 0.0,
        # mean with the single BEST instrument dropped — exposes how much the headline rests on one
        # outlier (e.g. SOL). If this is ≪ the full mean, the average is outlier-carried.
        "mean_cagr_ex_best_pct": round(sum(cagrs[:-1]) / len(cagrs[:-1]), 2) if len(cagrs) > 1 else 0.0,
        "median_scalp_cover_pct": round(median(covers), 1),
        "loss_cap_ok_pct": round(100.0 * sum(1 for r in ok if r["loss_cap_ok"]) / len(ok), 1) if ok else 0.0,
        "best": max(ok, key=lambda r: r["cagr_pct"], default=None),
        "worst": min(ok, key=lambda r: r["cagr_pct"], default=None),
    }
    return {"params": req.model_dump(), "results": rows, "summary": summary}


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


@app.get("/api/next-bet")
def next_bet(strategy_id: str = "default",
             base_bet: float = Query(100.0, gt=0),
             target_streak: int = Query(10, ge=1, le=settings.max_target_streak),
             cap_mult: float | None = Query(None, gt=0)):
    """Live antimartingale sizing — the closed loop. A TradingView Pine alert (or any client)
    reads this back AFTER its closed-trade alerts have streamed into the SignalStore to learn
    how big the NEXT order should be, given the current win streak. Pure read (no mutation):
    replays the stored win/loss outcomes through the pyramid state machine.

    `next_bet` is in the SAME units as `base_bet` (dollars, or contracts, or risk-%). On a fresh
    strategy with no signals it returns `base_bet` at streak 0.
    """
    rows = STORE.list(strategy_id=strategy_id, limit=5000)
    outcomes = [t.outcome for t in signals.signals_to_trials(rows)]
    st_ = strat.pyramid_state(outcomes, base_bet, target_streak, cap_mult)
    mult = round(st_["next_bet"] / base_bet, 4) if base_bet else None
    note = (f"{st_['streak']} consecutive win(s); place the next bet at "
            f"{st_['next_bet']:g} (= {mult}×base)." if st_["streak"] > 0
            else ("after a loss — reset to base." if st_["last_outcome"] == "loss"
                  else ("target streak booked — reset to base." if st_["target_streak_completions"]
                        else "no signals yet — start at base.")))
    return {"strategy_id": strategy_id, "base_bet": base_bet, "target_streak": target_streak,
            "cap_mult": cap_mult, "next_bet_mult": mult, **st_, "note": note}


@app.post("/api/backtest/from-signals")
def backtest_from_signals(req: FromSignalsReq):
    rows = STORE.list(strategy_id=req.strategy_id, limit=5000)
    trials = signals.signals_to_trials(rows)
    if not trials:
        raise HTTPException(status_code=422, detail="no outcome-bearing signals stored yet")
    res = strat.run_linear(trials, req.base_bet, req.target_streak,
                           commission_pct=req.commission_pct, slippage_pct=req.slippage_pct,
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
