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
  • **Scalping overlay**: an exponential counter-trend futures grid that harvests mean-reversion
    ("beats the theta"). Two models (see run_hedged_intraday):
      - **grid** (default) — EVENT-DRIVEN at the DAILY cadence. The key insight (user, 2026-06-04):
        the absence of tick data does NOT doom the backtest — pair a LONG-dated straddle (slow
        theta, default DTE 180d) with a WIDE grid whose step ≈ daily ATR, and the daily bar's
        range is large relative to the step, so each bar contains complete round-trips that daily
        OHLC resolves faithfully. Each bar is walked along an OHLC path (green O→L→H→C / red
        O→H→L→C); resting limit orders fill when crossed; a short at a sell-level is bought back
        one step lower (and vice-versa); each working part holds ≤1 leg so total ≤ the intraday
        limit (never naked); genuinely stuck legs are carried and MtM'd, closed at the roll. No
        efficiency/round-trip/penalty fudge. ⚠ HONEST SCOPE: long-dated options make the THETA
        faithful, and the straddle GAMMA on big moves is faithful — but the SCALP is still a LOWER
        BOUND: a daily bar holds ~1 reversal, so this books only ~10–40 round-trips/yr vs live ПИ's
        ~2500/yr (intraday oscillation isn't in the bar). So the backtest fairly measures "buy a
        long straddle and roll it" (profitable on volatile/trending instruments — the doctrine's
        "big fish"), NOT the flat-market scalping that needs intraday data.
      - **range** — legacy CRUDE heuristic `part_lots·(min(max_rt·g1, eff·reversed_range) −
        penalty·max(0,|C−O|−g1))`, reversed_range = (H−L)−|C−O|. NOT mechanically faithful: its
        magnitude is whatever the eff/max_rt knobs are set to (so it can over- OR under-state the
        scalp), and it never carries a position. Kept only for comparison — prefer 'grid'. At
        eff=0.5 it happens to recover ~14% of theta on a SPY/GLD MONTHLY straddle, near the corpus's
        "students offset 10–15% of straddle cost/month" figure.
    Both: part_lots from the three-thirds rule (intraday limit ≈ intraday_frac of futures / n_parts),
    grid step g1 = grid_atr_frac·dailyATR, spaced exponentially by grid_mult.

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
    scalp_model: str = "grid"          # 'grid' = event-driven daily round-trips; 'range' = heuristic
    scalp_round_trips: int = 0         # completed counter-trend round-trips (grid model)


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
                        dte_days: int = 180, roll_buffer_days: int = 10, r: float = 0.045,
                        qdiv: float = 0.0, n_parts: int = 5, grid_atr_frac: float = 1.0,
                        grid_mult: float = 2.0, intraday_frac: float = 1.0 / 3.0,
                        scalp_model: str = "grid", scalp_efficiency: float = 0.5,
                        max_rt_per_day: float = 10.0, stuck_penalty: float = 0.5,
                        commission_pct: float = 0.0, slippage_pct: float = 0.0, vol_model=None,
                        realized_vol: "pd.Series | None" = None,
                        default_sigma: float = 0.20) -> HedgedIntradayResult:
    """Daily-bar backtest of the synthetic-straddle + counter-trend-scalping ПИ method.

    The straddle (2 ATM calls − 1 future) is marked-to-market daily via BS and rolled to a
    fresh ATM strike within `roll_buffer_days` of expiry. Returns separated straddle / scalp /
    total P&L streams so the result is judged honestly by total account growth.

    Two scalp models:
      • **grid** (default) — an event-driven counter-trend grid run AT THE DAILY CADENCE. The
        grid step (g1 = `grid_atr_frac`·dailyATR, default 1×) is sized to the daily range, so a
        long-dated straddle (slow theta, default DTE 180d) needs only a few big round-trips that
        a daily bar fully resolves. Each bar is walked along an OHLC path (green: O→L→H→C, red:
        O→H→L→C); resting limit orders fill when the bar crosses them; a short booked at a
        sell-level is bought back one step lower (and vice-versa), each working part holding ≤1
        leg (so total position ≤ the intraday limit — never naked). Genuinely stuck legs are
        carried and marked-to-market, closed at the roll. NO efficiency/round-trip/penalty fudge:
        daily OHLC is *representative* in this regime, not a lower bound.
      • **range** — the older heuristic `part_lots·(min(max_rt·g1, eff·reversed_range) −
        stuck·max(0,|C−O|−g1))`, a pessimistic LOWER bound for a fast intraday grid on a monthly
        straddle (daily bars hide ~10 intraday round-trips/day). Kept for comparison.

    Sizing: premium budget = `risk_pct`·bank (re-sized at each roll). `n_straddles` = budget /
    (2·ATM-call). Scalp intraday limit = `intraday_frac` of the futures (three-thirds), split
    into `n_parts` working parts, spaced exponentially by `grid_mult`.
    """
    res = HedgedIntradayResult(starting_bank=starting_bank)
    res.scalp_model = scalp_model
    daily = daily.sort_index()
    idx = daily.index
    O = daily["Open"].to_numpy(float); H = daily["High"].to_numpy(float)
    Lo = daily["Low"].to_numpy(float); C = daily["Close"].to_numpy(float)
    atr_np = daily_atr.reindex(idx).to_numpy(float)
    T0 = dte_days / 365.0
    fee = (commission_pct + slippage_pct) / 100.0

    # warm up until ATR is finite
    i = 0
    while i < len(idx) and not (np.isfinite(atr_np[i]) and atr_np[i] > 0):
        i += 1
    if i >= len(idx):
        return res

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

    # ---- counter-trend grid state (event-driven, scalp_model='grid') ----
    scalp_realized = 0.0          # booked round-trips + stuck legs closed at roll
    scalp_cost_cum = 0.0          # scalp fill costs (separate so cum_scalp nets it once)
    scalp_acc = 0.0               # range-model running net
    GRID: dict = {}

    def setup_grid(center: float, atr_open: float, n_str: float):
        step = grid_atr_frac * atr_open if atr_open > 0 else abs(center) * 0.01
        offs, acc = [], 0.0
        for k in range(n_parts):
            acc += step * (grid_mult ** k)            # exponential gaps → cumulative offsets
            offs.append(acc)
        sell_lv = [center + o for o in offs]
        buy_lv = [center - o for o in offs]
        lim = max(n_str * intraday_frac, 0.0)
        GRID.clear()
        GRID.update(sell_lv=sell_lv, buy_lv=buy_lv,
                    inner_up=[center] + sell_lv[:-1],     # buy-back target for a short at sell_lv[k]
                    inner_dn=[center] + buy_lv[:-1],      # sell target for a long at buy_lv[k]
                    part_lots=lim / max(1, n_parts),
                    sarm=[True] * n_parts, barm=[True] * n_parts, legs=[])

    def scalp_walk(o, h, l, c):
        nonlocal scalp_realized, scalp_cost_cum
        g = GRID
        if not g or g["part_lots"] <= 0:
            return
        pl = g["part_lots"]
        path = [o, l, h, c] if c >= o else [o, h, l, c]
        for a, b in zip(path, path[1:]):
            if b > a:                                     # rising segment
                for k in range(n_parts):                  # enter shorts at sell-levels crossed up
                    lv = g["sell_lv"][k]
                    if g["sarm"][k] and a < lv <= b:
                        g["legs"].append({"side": "S", "k": k, "entry": lv,
                                          "target": g["inner_up"][k], "lots": pl})
                        g["sarm"][k] = False; scalp_cost_cum += fee * pl * lv
                for leg in g["legs"][:]:                  # close longs at their sell-target
                    if leg["side"] == "L" and a < leg["target"] <= b:
                        scalp_realized += (leg["target"] - leg["entry"]) * leg["lots"]
                        scalp_cost_cum += fee * leg["lots"] * leg["target"]
                        res.scalp_round_trips += 1; g["barm"][leg["k"]] = True; g["legs"].remove(leg)
            elif b < a:                                   # falling segment
                for k in range(n_parts):                  # enter longs at buy-levels crossed down
                    lv = g["buy_lv"][k]
                    if g["barm"][k] and b <= lv < a:
                        g["legs"].append({"side": "L", "k": k, "entry": lv,
                                          "target": g["inner_dn"][k], "lots": pl})
                        g["barm"][k] = False; scalp_cost_cum += fee * pl * lv
                for leg in g["legs"][:]:                  # buy back shorts at their target
                    if leg["side"] == "S" and b <= leg["target"] < a:
                        scalp_realized += (leg["entry"] - leg["target"]) * leg["lots"]
                        scalp_cost_cum += fee * leg["lots"] * leg["target"]
                        res.scalp_round_trips += 1; g["sarm"][leg["k"]] = True; g["legs"].remove(leg)

    def scalp_open_mtm(S):
        return sum(((S - leg["entry"]) if leg["side"] == "L" else (leg["entry"] - S)) * leg["lots"]
                   for leg in GRID.get("legs", []))

    def scalp_close_all(S):                               # realize stuck legs (at roll / final)
        nonlocal scalp_realized, scalp_cost_cum
        for leg in GRID.get("legs", []):
            scalp_realized += ((S - leg["entry"]) if leg["side"] == "L" else (leg["entry"] - S)) * leg["lots"]
            scalp_cost_cum += fee * leg["lots"] * S
        GRID["legs"] = []

    st = open_straddle(i, starting_bank)
    entry_notional = st["n_str"] * (2.0 * st["c0"] + st["S0"])
    realized_straddle = -fee * entry_notional             # entry fills: 2 calls + 1 future
    cum_scalp = 0.0
    cum_theta = 0.0
    cost_total = fee * entry_notional
    res.max_premium_at_risk = st["prem_book"]
    period_start_total = 0.0
    if scalp_model == "grid":
        setup_grid(st["K"], atr_np[i] if np.isfinite(atr_np[i]) and atr_np[i] > 0 else st["S0"] * 0.01,
                   st["n_str"])

    def straddle_unreal(S, d):
        T_rem = max((st["expiry"] - d).days / 365.0, 1e-6)
        c = float(opt.call_price(S, st["K"], T_rem, r, st["sig"], qdiv))
        calls = st["n_str"] * 2.0 * c - st["prem_book"]   # long calls vs premium paid
        fut = st["n_str"] * (-(S - st["F0"]))             # short 1 future per straddle
        return calls + fut, c, T_rem

    def scalp_pnl(S):                                     # cumulative scalp P&L (model-dependent)
        if scalp_model == "grid":
            return scalp_realized + scalp_open_mtm(S) - scalp_cost_cum
        return scalp_acc

    for j in range(i, len(idx)):
        d = idx[j]
        S, hi, lo, op = C[j], H[j], Lo[j], O[j]
        atr_d = atr_np[j]
        if not (np.isfinite(atr_d) and atr_d > 0):
            atr_d = abs(S) * 0.01                         # fallback step ~1% if ATR missing mid-series

        # ---- roll near expiry: crystallize the straddle (and any stuck scalp legs), re-strike ATM ----
        if (st["expiry"] - d).days <= roll_buffer_days:
            if scalp_model == "grid":
                scalp_close_all(S)
            mtm, c_now, _ = straddle_unreal(S, d)
            roll_cost = fee * st["n_str"] * (2.0 * c_now + S)
            realized_straddle += mtm - roll_cost
            cost_total += roll_cost
            cum_scalp = scalp_pnl(S)
            bank_now = starting_bank + realized_straddle + cum_scalp
            period_total = (realized_straddle + cum_scalp) - period_start_total
            res.table.append({
                "i": len(res.table) + 1, "open": st["date"].date().isoformat(),
                "close": d.date().isoformat(), "strike": round(st["K"], 2),
                "iv": round(st["sig"], 4), "n_straddles": round(st["n_str"], 3),
                "premium": round(st["prem_book"], 2), "straddle_pnl": round(mtm, 2),
                "period_pnl": round(period_total, 2), "bank": round(bank_now, 2)})
            res.worst_period_pnl = min(res.worst_period_pnl, period_total)
            period_start_total = realized_straddle + cum_scalp
            st = open_straddle(j, bank_now)
            open_notional = st["n_str"] * (2.0 * st["c0"] + st["S0"])
            realized_straddle -= fee * open_notional
            cost_total += fee * open_notional
            res.max_premium_at_risk = max(res.max_premium_at_risk, st["prem_book"])
            res.n_rolls += 1
            res.rolls.append({"n": res.n_rolls, "date": d.date().isoformat(),
                              "spot": round(float(S), 4), "new_strike": round(st["K"], 4),
                              "iv": round(st["sig"], 4), "n_straddles": round(st["n_str"], 3)})
            if scalp_model == "grid":
                setup_grid(st["K"], atr_d, st["n_str"])

        # ---- straddle daily mark-to-market + modeled 1-day theta ----
        unreal, c_now, T_rem = straddle_unreal(S, d)
        c_tom = float(opt.call_price(S, st["K"], max(T_rem - 1.0 / 365.0, 1e-6), r, st["sig"], qdiv))
        cum_theta += st["n_str"] * 2.0 * (c_tom - c_now)  # ≤ 0: decay holding S flat one day

        # ---- scalping overlay ----
        if scalp_model == "grid":
            scalp_walk(op, hi, lo, S)
        else:                                             # range heuristic (lower bound)
            g1 = grid_atr_frac * atr_d
            part_lots = (st["n_str"] * intraday_frac) / max(1, n_parts)
            reversed_range = max((hi - lo) - abs(S - op), 0.0)
            if g1 > 1e-12:
                harvest = min(max_rt_per_day * g1, scalp_efficiency * reversed_range)
                rt = harvest / g1
                scalp_gross = part_lots * (harvest - stuck_penalty * max(0.0, abs(S - op) - g1))
                sc = fee * 2.0 * rt * part_lots * S
            else:
                scalp_gross = sc = 0.0
            scalp_acc += scalp_gross - sc
            scalp_cost_cum += sc
        cum_scalp = scalp_pnl(S)

        res.equity_dates.append(d)
        res.equity_total.append(starting_bank + realized_straddle + unreal + cum_scalp)
        res.equity_straddle.append(realized_straddle + unreal)
        res.equity_scalp.append(cum_scalp)
        res.theta_path.append(cum_theta)

    # ---- close the final open straddle (+ stuck scalp legs) at the last bar ----
    if res.equity_dates:
        last = idx[-1]; Sf = C[-1]
        if scalp_model == "grid":
            scalp_close_all(Sf)
        mtm, c_now, _ = straddle_unreal(Sf, last)
        realized_straddle += mtm - fee * st["n_str"] * (2.0 * c_now + Sf)
        cost_total += fee * st["n_str"] * (2.0 * c_now + Sf)
        cum_scalp = scalp_pnl(Sf)
        period_total = (realized_straddle + cum_scalp) - period_start_total
        res.table.append({
            "i": len(res.table) + 1, "open": st["date"].date().isoformat(),
            "close": last.date().isoformat(), "strike": round(st["K"], 2),
            "iv": round(st["sig"], 4), "n_straddles": round(st["n_str"], 3),
            "premium": round(st["prem_book"], 2), "straddle_pnl": round(mtm, 2),
            "period_pnl": round(period_total, 2),
            "bank": round(starting_bank + realized_straddle + cum_scalp, 2)})
        res.worst_period_pnl = min(res.worst_period_pnl, period_total)
        res.equity_total[-1] = starting_bank + realized_straddle + cum_scalp
        res.equity_straddle[-1] = realized_straddle

    # ---- bottom line ----
    cost_total += scalp_cost_cum
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
