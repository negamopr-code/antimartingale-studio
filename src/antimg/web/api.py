"""FastAPI app — JSON API + static Plotly frontend.

Stateless request handlers (no per-process session state) so the app scales horizontally:
run N replicas behind a load balancer; shared state lives in the SignalStore (swap SQLite
for Postgres/Redis via env) and the data cache. CPU/IO handlers are sync `def` so Starlette
runs them in a threadpool, keeping the event loop free.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import atr_strategy as strat
from .. import data as datamod
from .. import vol as volmod
from .. import instruments, scenarios, signals, tradingview
from ..simcore import Simulation, expected_trades_per_cycle
from . import serialization as ser
from .config import settings
from .schemas import (BacktestReq, CoinFlipReq, ExplainReq, FromSignalsReq, InspectReq,
                      OptionsReq, ScanReq)

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


def _build_vol(req: "OptionsReq", daily):
    """Construct the IV surface (real CBOE term structure + fixed-β skew) for the option model.

    Falls back to the asset's realized vol when no vol index is available (non-S&P/-VXN/etc),
    and to a flat constant when iv_source='constant'. With use_term_structure=False only the
    nearest tenor is used (flat in T). Returns a vol.VolModel (see src/antimg/vol.py).
    """
    realized = datamod.realized_vol(daily["Close"], req.iv_window)
    vm = volmod.build(req.ticker, req.start, iv_source=req.iv_source,
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
