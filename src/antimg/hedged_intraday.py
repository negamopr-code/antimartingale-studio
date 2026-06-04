"""Backtest of the **Прикрытый Интрадей** (ПИ / Hedged Intraday) method — Korovin.

Doctrine source: the `/hedgedintraday` skill (NotebookLM `5fada65b`). This is NOT the
antimartingale family; it is a **long-volatility synthetic straddle whose theta is paid by
counter-trend intraday futures scalping**. We reproduce it as a daily-bar backtest.

Construction (per the corpus, confirmed by live consult 2026-06-04):
  • Synthetic straddle = **2 ATM Calls − 1 Future** at the central strike (V-payoff, long
    gamma, delta-neutral at entry). Max loss = the premium paid for the calls.
  • Priced by Black-Scholes mark-to-market (we have no historical option chains); IV from the
    real CBOE vol surface (vol.VolModel) or realized vol. Rolled to a fresh ATM strike near
    expiry (monthly by default).
  • **Scalping overlay**: an exponential counter-trend futures grid inside the day's range
    harvests mean-reversion ("beats the theta"). With only daily OHLC we cannot see the tick
    path, so the scalp P&L for a day is modeled from the bar geometry:
        reversed_range = (High−Low) − |Close−Open|        # the part of the range that came back
        harvest        = min(max_rt·g1, efficiency·reversed_range)   # counter-trend round-trips
        trend_drag     = penalty·max(0, |Close−Open| − g1)           # parts stuck offside in a trend
        scalp_pnl_day  = part_lots · (harvest − trend_drag) − fills_cost
    where g1 = grid_atr_frac·ATR (first grid step) and part_lots derives from the three-thirds
    rule (intraday limit ≈ intraday_frac of the futures, split over n_parts). This is an
    APPROXIMATION (no tick data) with an explicit, tunable efficiency — read the verdict caveat.

⚠ Daily bars structurally UNDERCOUNT scalping: one OHLC bar reveals ~1 reversal/day
((H−L)−|C−O|), but real ПИ scalps ~10 round-trips/day on a 1-min chart. So the modeled scalp
income here is a PESSIMISTIC LOWER BOUND and theta dominates more than it would live — raise
`max_rt_per_day`/`scalp_efficiency` to approximate intraday frequency. (Empirically the default
conservative settings recover ~14% of theta on SPY/GLD 2018-26, matching the corpus's own
"students offset 10–15% of straddle cost per month" figure — a calibration check, not a target.)

The honest read (corpus): scalping's MINIMUM task is to pay theta; it does NOT always cover it
(dead/low-vol months bleed). Real edge = scalping PLUS an eventual move (the straddle's gamma).
Realistic long-run target ≈ 25–40%/yr, not per-period doubling. We therefore separate the P&L
into **straddle (gamma−theta)** and **scalp** streams and judge by total account growth.

Not financial advice — an educational reproduction of a third-party method.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import options as opt


@dataclass
class HedgedIntradayResult:
    equity_dates: list[pd.Timestamp] = field(default_factory=list)
    equity_total: list[float] = field(default_factory=list)
    equity_straddle: list[float] = field(default_factory=list)   # cumulative straddle P&L (from 0)
    equity_scalp: list[float] = field(default_factory=list)      # cumulative scalp P&L (from 0)
    theta_path: list[float] = field(default_factory=list)        # cumulative modeled theta paid (<=0)
    rolls: list[dict] = field(default_factory=list)              # roll events (re-strike at expiry)
    table: list[dict] = field(default_factory=list)              # per-straddle-period summary
    # bottom line
    starting_bank: float = 0.0
    final_bank: float = 0.0
    straddle_pnl: float = 0.0
    scalp_pnl: float = 0.0
    total_theta: float = 0.0
    n_rolls: int = 0
    n_days: int = 0
    years: float = 0.0
    ann_return_pct: float = 0.0
    max_drawdown: float = 0.0
    worst_period_pnl: float = 0.0      # most negative single-straddle-period total — should be ≥ −premium
    max_premium_at_risk: float = 0.0   # largest premium paid for any one straddle (the loss floor)
    scalp_covers_theta_pct: float = 0.0
    total_cost: float = 0.0


def _sigma_at(rv: "pd.Series | None", date, default: float) -> float:
    if rv is None:
        return default
    try:
        v = rv.asof(date)
    except Exception:
        v = np.nan
    return float(v) if v is not None and np.isfinite(v) and v > 0 else default


def _atm_iv(vol_model, realized_vol, date, T0, default_sigma) -> float:
    if vol_model is not None:
        a = vol_model.atm(date, T0)
        if a is not None and np.isfinite(a) and a > 0:
            return float(a)
    return _sigma_at(realized_vol, date, default_sigma)


def _drawdown(equity: list[float]) -> float:
    peak = -np.inf
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return mdd


def run_hedged_intraday(daily: pd.DataFrame, daily_atr: pd.Series, *,
                        starting_bank: float = 10_000.0, risk_pct: float = 0.20,
                        dte_days: int = 30, roll_buffer_days: int = 5, r: float = 0.045,
                        qdiv: float = 0.0, n_parts: int = 5, grid_atr_frac: float = 0.5,
                        grid_mult: float = 2.0, intraday_frac: float = 1.0 / 3.0,
                        scalp_efficiency: float = 0.5, max_rt_per_day: float = 10.0,
                        stuck_penalty: float = 0.5, commission_pct: float = 0.0,
                        slippage_pct: float = 0.0, vol_model=None,
                        realized_vol: "pd.Series | None" = None,
                        default_sigma: float = 0.20) -> HedgedIntradayResult:
    """Daily-bar backtest of the synthetic-straddle + counter-trend-scalping ПИ method.

    The straddle (2 ATM calls − 1 future) is marked-to-market daily via BS and rolled to a
    fresh ATM strike within `roll_buffer_days` of expiry. The scalping overlay harvests the
    reversed portion of each day's range (see module docstring). Returns separated straddle /
    scalp / total P&L streams so the result can be judged honestly by total account growth.

    Sizing follows the doctrine: premium budget = `risk_pct`·bank (re-sized at each roll to the
    running bank). `n_straddles` = budget / (2·ATM-call). The scalp intraday limit = `intraday_frac`
    of the futures held (three-thirds rule), split into `n_parts` working parts; the first grid
    step g1 = `grid_atr_frac`·ATR, spaced exponentially by `grid_mult`.
    """
    res = HedgedIntradayResult(starting_bank=starting_bank)
    daily = daily.sort_index()
    idx = daily.index
    O = daily["Open"].to_numpy(float); H = daily["High"].to_numpy(float)
    Lo = daily["Low"].to_numpy(float); C = daily["Close"].to_numpy(float)
    atr_np = daily_atr.reindex(idx).to_numpy(float)
    T0 = dte_days / 365.0
    fee = (commission_pct + slippage_pct) / 100.0

    # exponential grid: g1, g1·m, g1·m², … ; "reach" = outermost level distance from center
    steps = [grid_mult ** k for k in range(max(1, n_parts))]
    reach_units = float(sum(steps))      # in multiples of g1

    # warm up until ATR is finite
    i = 0
    while i < len(idx) and not (np.isfinite(atr_np[i]) and atr_np[i] > 0):
        i += 1
    if i >= len(idx):
        return res

    # ---- open the first straddle ----
    def open_straddle(day_i: int, bank: float):
        S0 = C[day_i]; date = idx[day_i]
        sig = _atm_iv(vol_model, realized_vol, date, T0, default_sigma)
        K = S0                                          # ATM (nearest strike == spot)
        c0 = float(opt.call_price(S0, K, T0, r, sig, qdiv))
        prem_per = 2.0 * c0                             # premium per straddle unit (2 long calls)
        budget = max(risk_pct * bank, 0.0)
        n_str = (budget / prem_per) if prem_per > 1e-12 else 0.0
        prem_book = n_str * prem_per
        return {"S0": S0, "date": date, "sig": sig, "K": K, "c0": c0, "n_str": n_str,
                "F0": S0, "prem_book": prem_book, "expiry": date + pd.Timedelta(days=dte_days),
                "start_i": day_i}

    st = open_straddle(i, starting_bank)
    # entry fills cost: 2 calls (premium notional) + 1 future (spot notional) per straddle
    entry_notional = st["n_str"] * (2.0 * st["c0"] + st["S0"])
    realized_straddle = -fee * entry_notional
    cum_scalp = 0.0
    cum_theta = 0.0
    cost_total = fee * entry_notional
    res.max_premium_at_risk = st["prem_book"]
    period_start_total = 0.0                            # P&L accumulator at the start of this straddle period

    def straddle_unreal(S, d):
        T_rem = max((st["expiry"] - d).days / 365.0, 1e-6)
        c = float(opt.call_price(S, st["K"], T_rem, r, st["sig"], qdiv))
        calls = st["n_str"] * 2.0 * c - st["prem_book"]      # long calls vs premium paid
        fut = st["n_str"] * (-(S - st["F0"]))                # short 1 future per straddle
        return calls + fut, c, T_rem

    for j in range(i, len(idx)):
        d = idx[j]
        S, hi, lo, op = C[j], H[j], Lo[j], O[j]
        atr_d = atr_np[j]
        if not (np.isfinite(atr_d) and atr_d > 0):
            atr_d = abs(S) * 0.01                            # fallback step ~1% if ATR missing mid-series

        # ---- roll near expiry: crystallize the current straddle, re-strike ATM ----
        if (st["expiry"] - d).days <= roll_buffer_days:
            mtm, c_now, _ = straddle_unreal(S, d)
            close_notional = st["n_str"] * (2.0 * c_now + S)
            roll_cost = fee * close_notional
            realized_straddle += mtm - roll_cost
            cost_total += roll_cost
            bank_now = starting_bank + realized_straddle + cum_scalp
            # per-period summary row (P&L attributable to the just-closed straddle period)
            period_total = (realized_straddle + cum_scalp) - period_start_total
            res.table.append({
                "i": len(res.table) + 1, "open": st["date"].date().isoformat(),
                "close": d.date().isoformat(), "strike": round(st["K"], 2),
                "iv": round(st["sig"], 4), "n_straddles": round(st["n_str"], 3),
                "premium": round(st["prem_book"], 2),
                "straddle_pnl": round(mtm, 2),
                "period_pnl": round(period_total, 2),
                "bank": round(bank_now, 2),
            })
            res.worst_period_pnl = min(res.worst_period_pnl, period_total)
            period_start_total = realized_straddle + cum_scalp
            # re-open at current spot, re-sized to the running bank
            st = open_straddle(j, bank_now)
            open_notional = st["n_str"] * (2.0 * st["c0"] + st["S0"])
            realized_straddle -= fee * open_notional
            cost_total += fee * open_notional
            res.max_premium_at_risk = max(res.max_premium_at_risk, st["prem_book"])
            res.n_rolls += 1
            res.rolls.append({"n": res.n_rolls, "date": d.date().isoformat(),
                              "spot": round(float(S), 4), "new_strike": round(st["K"], 4),
                              "iv": round(st["sig"], 4), "n_straddles": round(st["n_str"], 3)})

        # ---- straddle daily mark-to-market + modeled 1-day theta ----
        unreal, c_now, T_rem = straddle_unreal(S, d)
        c_tom = float(opt.call_price(S, st["K"], max(T_rem - 1.0 / 365.0, 1e-6), r, st["sig"], qdiv))
        theta_day = st["n_str"] * 2.0 * (c_tom - c_now)       # ≤ 0: decay holding S flat one day
        cum_theta += theta_day

        # ---- scalping overlay (counter-trend grid harvest of the reversed range) ----
        g1 = grid_atr_frac * atr_d                            # first grid step (price points)
        intraday_limit_lots = st["n_str"] * intraday_frac     # three-thirds: ⅓ of futures
        part_lots = intraday_limit_lots / max(1, n_parts)
        rng = max(hi - lo, 0.0)
        move = abs(S - op)                                    # directional component of the bar
        reversed_range = max(rng - move, 0.0)                 # the part that came back (mean-revert)
        if g1 > 1e-12:
            harvest = min(max_rt_per_day * g1, scalp_efficiency * reversed_range)
            trend_drag = stuck_penalty * max(0.0, move - g1)  # parts stuck offside in a trend
            rt = harvest / g1                                 # round-trips (for the fills cost)
        else:
            harvest = trend_drag = rt = 0.0
        scalp_gross = part_lots * (harvest - trend_drag)
        scalp_fill_notional = 2.0 * rt * part_lots * S        # buy+sell per round-trip
        scalp_cost = fee * scalp_fill_notional
        cum_scalp += scalp_gross - scalp_cost
        cost_total += scalp_cost

        total = starting_bank + realized_straddle + unreal + cum_scalp
        res.equity_dates.append(d)
        res.equity_total.append(total)
        res.equity_straddle.append(realized_straddle + unreal)
        res.equity_scalp.append(cum_scalp)
        res.theta_path.append(cum_theta)

    # ---- close the final open straddle at the last bar ----
    if res.equity_dates:
        last = idx[-1]; Sf = C[-1]
        mtm, c_now, _ = straddle_unreal(Sf, last)
        close_notional = st["n_str"] * (2.0 * c_now + Sf)
        realized_straddle += mtm - fee * close_notional
        cost_total += fee * close_notional
        period_total = (realized_straddle + cum_scalp) - period_start_total
        res.table.append({
            "i": len(res.table) + 1, "open": st["date"].date().isoformat(),
            "close": last.date().isoformat(), "strike": round(st["K"], 2),
            "iv": round(st["sig"], 4), "n_straddles": round(st["n_str"], 3),
            "premium": round(st["prem_book"], 2), "straddle_pnl": round(mtm, 2),
            "period_pnl": round(period_total, 2),
            "bank": round(starting_bank + realized_straddle + cum_scalp, 2),
        })
        res.worst_period_pnl = min(res.worst_period_pnl, period_total)
        # the last equity point already had the open straddle's unreal; replace with realized close
        res.equity_total[-1] = starting_bank + realized_straddle + cum_scalp
        res.equity_straddle[-1] = realized_straddle

    # ---- bottom line ----
    res.straddle_pnl = realized_straddle
    res.scalp_pnl = cum_scalp
    res.total_theta = cum_theta
    res.total_cost = cost_total
    res.final_bank = starting_bank + realized_straddle + cum_scalp
    res.n_days = len(res.equity_dates)
    if res.n_days >= 2:
        res.years = max((res.equity_dates[-1] - res.equity_dates[0]).days / 365.25, 1e-6)
        growth = res.final_bank / starting_bank if starting_bank > 0 else 1.0
        res.ann_return_pct = 100.0 * (growth ** (1.0 / res.years) - 1.0) if growth > 0 else -100.0
    res.max_drawdown = _drawdown(res.equity_total)
    res.scalp_covers_theta_pct = (100.0 * cum_scalp / abs(cum_theta)) if abs(cum_theta) > 1e-9 else 0.0
    return res
