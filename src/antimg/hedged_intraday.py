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
        limit (never naked). **Stuck legs are CARRIED to the roll** (doctrine: heal injured parts,
        never abandon the construction) — this is what lets the counter-trend grid actually capture
        mean-reversion (an open underwater leg is a bet on the reversion). `scalp_recenter_days>0`
        force-closes legs to the current price on a timer — but that REALIZES the not-yet-reverted
        legs as losses and throws away the edge, so it defaults to 0 (OFF). No
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
    gamma_dir_pnl: float = 0.0         # straddle P&L minus theta = the gamma+directional capture
    breakeven_scalp_cover_pct: float = 0.0  # % of theta the scalp must cover for net=0 (doctrine min ≈100%)
    scalp_heals: int = 0               # times stuck parts were healed (re-centered) with accumulated profit
    confident_flat_days: int = 0       # days in "уверенный флет" (≥N clean round-trips, scaling allowed)
    scalp_scaled_max: float = 1.0      # max working-part lot scale-up reached in уверенный флет (1.0 = never)
    intraday_bars: int = 0             # # intraday bars the scalp walked (>0 ⇒ scalp on a real intraday feed)


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
                        dte_days: int = 365, roll_buffer_days: int = 10,
                        roll_profit_pct: float = 0.0, r: float = 0.045,
                        qdiv: float = 0.0, n_parts: int = 5, grid_atr_frac: float = 0.5,
                        grid_mult: float = 2.0, intraday_frac: float = 1.0 / 3.0,
                        scalp_model: str = "grid", scalp_recenter_days: int = 0,
                        heal_with_profit: bool = True, confident_flat_n: int = 3,
                        confident_flat_scale: bool = True,
                        use_bbands: bool = True, bb_window: int = 20, bb_k: float = 2.0,
                        scalp_efficiency: float = 0.5,
                        max_rt_per_day: float = 10.0, stuck_penalty: float = 0.5,
                        commission_pct: float = 0.0, slippage_pct: float = 0.0, vol_model=None,
                        realized_vol: "pd.Series | None" = None,
                        default_sigma: float = 0.20, intraday: "pd.DataFrame | None" = None,
                        trace: "list | None" = None) -> HedgedIntradayResult:
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
    # Bollinger Bands = the FLAT detector. Scalp counter-trend only INSIDE the band; when price
    # breaks OUT (a confirmed trend) suspend new counter-trend entries and let the straddle +
    # trend-reserve run (doctrine: don't fade a galloping market). ub/lb are NaN until warmed up.
    _mid = daily["Close"].rolling(bb_window).mean()
    _sd = daily["Close"].rolling(bb_window).std()
    ub_np = (_mid + bb_k * _sd).to_numpy(float)
    lb_np = (_mid - bb_k * _sd).to_numpy(float)
    # optional INTRADAY feed for the scalp: group bars by calendar day → the grid walks the real
    # intraday path (many round-trips) instead of one daily OHLC bar. Straddle/theta/rolls stay daily.
    # FLAT/TREND gate goes INTRADAY too (doctrine: "решения на внутридневном таймфрейме в реальном
    # времени; дневные бары скрывают шум"): identify the range on the intraday feed itself — a rolling
    # ~1-day Bollinger band ("цену зажали в диапазоне на протяжении часа/дня"). Price breaking it
    # intraday = a "галоп"/trend → step aside, let the straddle run; inside it = flat → scalp.
    intraday_by_day: dict = {}
    if intraday is not None and not intraday.empty:
        idf = intraday.sort_index()
        ohlc = idf[["Open", "High", "Low", "Close"]].to_numpy(float)
        # intraday range band: window ≈ one day of this feed (≥ bb_window), k = bb_k σ
        n_days_i = max(idf.index.normalize().nunique(), 1)
        bars_per_day = max(int(round(len(idf) / n_days_i)), 1)
        win_i = max(bb_window, bars_per_day)
        _ic = idf["Close"]
        _mid_i = _ic.rolling(win_i, min_periods=bb_window).mean()
        _sd_i = _ic.rolling(win_i, min_periods=bb_window).std()
        ub_i = (_mid_i + bb_k * _sd_i).to_numpy(float)
        lb_i = (_mid_i - bb_k * _sd_i).to_numpy(float)
        for k_i, (ts, row) in enumerate(zip(idf.index, ohlc)):
            # carry each bar's OWN intraday band so the scalp gate is intraday, not the daily verdict
            intraday_by_day.setdefault(ts.normalize(), []).append((row, ub_i[k_i], lb_i[k_i]))

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
        # DELTA-NEUTRAL CORE (the doctrine — corpus-confirmed): the synthetic straddle is symmetric,
        # 2·n_str calls hedged by n_str short futures (e.g. "30 Колл − 15 Фьюч": sell EXACTLY calls/2 so
        # the position is a *ровный стреддл* with zero net delta) → it profits on a big move EITHER way
        # via gamma (a −72% crash MUST win, same as a rally). The three-thirds is the SCALP LIMIT
        # (how many futures the scalp sells counter-trend within the 33→67% band), NOT a permanent
        # directional tilt of the core. (Bugfix: was (2/3)·n_str = the 33% FLOOR = net-LONG core, which
        # silently bled the straddle on DOWN moves — BTC 60k→17k showed a near-flat/negative straddle,
        # nonsense for long-vol. The earlier "net-long trend reserve" lesson over-fit to up-movers.)
        base_futs = 1.0 * n_str
        return {"S0": S0, "date": date, "sig": sig, "K": K, "c0": c0, "n_str": n_str,
                "base_futs": base_futs,
                "F0": S0, "prem_book": prem_book, "expiry": date + pd.Timedelta(days=dte_days),
                "start_i": day_i}

    # ---- counter-trend grid state (event-driven, scalp_model='grid') ----
    scalp_realized = 0.0          # booked round-trips + stuck legs closed at roll
    scalp_cost_cum = 0.0          # scalp fill costs (separate so cum_scalp nets it once)
    scalp_acc = 0.0               # range-model running net
    heal_budget = 0.0             # accumulated booked round-trip profit available to "heal" stuck parts
    clean_streak = 0              # consecutive clean round-trips with no stuck/heal → уверенный флет
    GRID: dict = {}

    def setup_grid(center: float, atr_open: float, n_str: float, date=None):
        step = grid_atr_frac * atr_open if atr_open > 0 else abs(center) * 0.01
        offs, acc = [], 0.0
        for k in range(n_parts):
            acc += step * (grid_mult ** k)            # exponential gaps → cumulative offsets
            offs.append(acc)
        sell_lv = [center + o for o in offs]
        buy_lv = [center - o for o in offs]
        lim = max(2.0 * n_str * intraday_frac, 0.0)       # scalp limit = ⅓ of CALLS (2·n_str·⅓), the 33→67% band
        GRID.clear()
        GRID.update(center=center, reach=offs[-1] if offs else 0.0,
                    sell_lv=sell_lv, buy_lv=buy_lv,
                    inner_up=[center] + sell_lv[:-1],     # buy-back target for a short at sell_lv[k]
                    inner_dn=[center] + buy_lv[:-1],      # sell target for a long at buy_lv[k]
                    part_lots=lim / max(1, n_parts),
                    sarm=[True] * n_parts, barm=[True] * n_parts, legs=[])
        if trace is not None and date is not None:        # emit the 5 working-part levels for the chart
            trace.append({"t": "grid_setup", "date": date.date().isoformat(),
                          "center": round(center, 4),
                          "sell": [round(x, 4) for x in sell_lv],
                          "buy": [round(x, 4) for x in buy_lv],
                          "part_lots": round(lim / max(1, n_parts), 3)})

    def _book_roundtrip(pnl):                             # уверенный флет / heal budget bookkeeping
        nonlocal heal_budget, clean_streak
        heal_budget += pnl                                # accrued profit usable to unstick parts
        clean_streak += 1
        if clean_streak == confident_flat_n and trace is not None:
            trace.append({"t": "confident_flat", "date": _book_roundtrip.date,
                          "streak": clean_streak})

    def scalp_walk(o, h, l, c, ub, lb, date):
        nonlocal scalp_realized, scalp_cost_cum
        _book_roundtrip.date = date.date().isoformat()
        g = GRID
        if not g or g["part_lots"] <= 0:
            return
        # УВЕРЕННЫЙ ФЛЕТ / заслуженный риск (doctrine): after ≥confident_flat_n clean cycles, scale the
        # working-part lot UP — funded by ACCRUED PROFIT only (heal_budget). Capped at ×2 so the total
        # scalp (n_parts·2·base) never exceeds calls−base ⇒ still never naked.
        pl = g["part_lots"]
        lot_scale = 1.0
        if confident_flat_scale and clean_streak >= confident_flat_n and st["prem_book"] > 0:
            lot_scale = 1.0 + min(max(heal_budget, 0.0) / st["prem_book"], 1.0)
            pl *= lot_scale
            if lot_scale > res.scalp_scaled_max:
                res.scalp_scaled_max = round(lot_scale, 3)
        # FLAT gate: don't open a counter-trend leg into a breakout (short above the upper band /
        # long below the lower band) — step aside, let the straddle run. Exits are always allowed.
        gate_short = (lambda lv: not (use_bbands and np.isfinite(ub) and lv > ub))
        gate_long = (lambda lv: not (use_bbands and np.isfinite(lb) and lv < lb))
        def _rec(t, **kw):
            if trace is not None:
                trace.append({"t": t, "date": date.date().isoformat(),
                              "streak": clean_streak, "conf_flat": clean_streak >= confident_flat_n,
                              "scale": round(lot_scale, 3), **kw})
        path = [o, l, h, c] if c >= o else [o, h, l, c]
        for a, b in zip(path, path[1:]):
            if b > a:                                     # rising segment
                for k in range(n_parts):                  # enter shorts at sell-levels crossed up
                    lv = g["sell_lv"][k]
                    if g["sarm"][k] and a < lv <= b and gate_short(lv):
                        g["legs"].append({"side": "S", "k": k, "entry": lv,
                                          "target": g["inner_up"][k], "lots": pl})
                        g["sarm"][k] = False; scalp_cost_cum += fee * pl * lv
                        _rec("scalp_open", side="short", part=k + 1, price=round(lv, 4), lots=round(pl, 3))
                for leg in g["legs"][:]:                  # close longs at their sell-target
                    if leg["side"] == "L" and a < leg["target"] <= b:
                        pnl = (leg["target"] - leg["entry"]) * leg["lots"]
                        scalp_realized += pnl
                        scalp_cost_cum += fee * leg["lots"] * leg["target"]
                        res.scalp_round_trips += 1; _book_roundtrip(pnl)
                        g["barm"][leg["k"]] = True; g["legs"].remove(leg)
                        _rec("scalp_close", side="long", part=leg["k"] + 1, lots=round(leg["lots"], 3),
                             entry=round(leg["entry"], 4), exit=round(leg["target"], 4), pnl=round(pnl, 2))
            elif b < a:                                   # falling segment
                for k in range(n_parts):                  # enter longs at buy-levels crossed down
                    lv = g["buy_lv"][k]
                    if g["barm"][k] and b <= lv < a and gate_long(lv):
                        g["legs"].append({"side": "L", "k": k, "entry": lv,
                                          "target": g["inner_dn"][k], "lots": pl})
                        g["barm"][k] = False; scalp_cost_cum += fee * pl * lv
                        _rec("scalp_open", side="long", part=k + 1, price=round(lv, 4), lots=round(pl, 3))
                for leg in g["legs"][:]:                  # buy back shorts at their target
                    if leg["side"] == "S" and b <= leg["target"] < a:
                        pnl = (leg["entry"] - leg["target"]) * leg["lots"]
                        scalp_realized += pnl
                        scalp_cost_cum += fee * leg["lots"] * leg["target"]
                        res.scalp_round_trips += 1; _book_roundtrip(pnl)
                        g["sarm"][leg["k"]] = True; g["legs"].remove(leg)
                        _rec("scalp_close", side="short", part=leg["k"] + 1, lots=round(leg["lots"], 3),
                             entry=round(leg["entry"], 4),
                             exit=round(leg["target"], 4), pnl=round(pnl, 2))

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
    entry_notional = st["n_str"] * 2.0 * st["c0"] + st["base_futs"] * st["S0"]
    realized_straddle = -fee * entry_notional             # entry fills: 2 calls + ⅓-of-calls base future
    cum_scalp = 0.0
    cum_theta = 0.0
    cost_total = fee * entry_notional
    res.max_premium_at_risk = st["prem_book"]
    period_start_total = 0.0
    last_recenter = idx[i]
    if scalp_model == "grid":
        setup_grid(st["K"], atr_np[i] if np.isfinite(atr_np[i]) and atr_np[i] > 0 else st["S0"] * 0.01,
                   st["n_str"], date=idx[i])

    def straddle_unreal(S, d):
        T_rem = max((st["expiry"] - d).days / 365.0, 1e-6)
        c = float(opt.call_price(S, st["K"], T_rem, r, st["sig"], qdiv))
        calls = st["n_str"] * 2.0 * c - st["prem_book"]   # long calls vs premium paid
        fut = st["base_futs"] * (-(S - st["F0"]))         # short base hedge = calls/2 (delta-neutral)
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

        # ---- ROLL: near expiry (schedule) OR planned profit reached (doctrine module 26/27: roll
        # IN THE PROFIT ZONE after a strong move — close the WHOLE construction incl stuck scalp legs,
        # re-open a fresh ATM delta-neutral straddle, compound the bank, scrap stuck parts. Target =
        # roll_profit_pct % of the deposit at risk this period; doctrine ref ≈ 5–7%/mo. 0 = OFF). ----
        expiry_due = (st["expiry"] - d).days <= roll_buffer_days
        profit_due = False
        if roll_profit_pct > 0 and not expiry_due:
            mtm_chk, _, _ = straddle_unreal(S, d)
            live_profit = mtm_chk + (realized_straddle + scalp_pnl(S)) - period_start_total
            target = (roll_profit_pct / 100.0) * (starting_bank + period_start_total)
            profit_due = target > 0 and live_profit >= target
        if expiry_due or profit_due:
            if scalp_model == "grid":
                scalp_close_all(S)
            mtm, c_now, _ = straddle_unreal(S, d)
            roll_cost = fee * (st["n_str"] * 2.0 * c_now + st["base_futs"] * S)
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
                "period_pnl": round(period_total, 2), "bank": round(bank_now, 2),
                "roll_reason": "профит-цель" if profit_due else "экспирация"})
            res.worst_period_pnl = min(res.worst_period_pnl, period_total)
            period_start_total = realized_straddle + cum_scalp
            st = open_straddle(j, bank_now)
            open_notional = st["n_str"] * 2.0 * st["c0"] + st["base_futs"] * st["S0"]
            realized_straddle -= fee * open_notional
            cost_total += fee * open_notional
            res.max_premium_at_risk = max(res.max_premium_at_risk, st["prem_book"])
            res.n_rolls += 1
            res.rolls.append({"n": res.n_rolls, "date": d.date().isoformat(),
                              "spot": round(float(S), 4), "new_strike": round(st["K"], 4),
                              "iv": round(st["sig"], 4), "n_straddles": round(st["n_str"], 3),
                              "reason": "профит-цель" if profit_due else "экспирация"})
            if scalp_model == "grid":
                setup_grid(st["K"], atr_d, st["n_str"], date=d)
                last_recenter = d

        # ---- OPTIONAL grid re-centering (scalp_recenter_days>0, default OFF) ----
        # Re-anchors the grid to the current price every N days. ⚠ It force-closes open legs at
        # market — which REALIZES the underwater counter-trend legs that were about to mean-revert,
        # destroying the edge (verified: flips a clean OU mean-reverter from +933 to −602). Default 0
        # = carry stuck legs to the roll (doctrine-faithful, lets the grid capture mean-reversion).
        if (scalp_model == "grid" and scalp_recenter_days > 0
                and (d - last_recenter).days >= scalp_recenter_days):
            scalp_close_all(op)                           # realize open legs at today's open
            setup_grid(op, atr_d, st["n_str"], date=d)  # re-anchor grid to current price
            last_recenter = d

        # ---- straddle daily mark-to-market + modeled 1-day theta ----
        unreal, c_now, T_rem = straddle_unreal(S, d)
        c_tom = float(opt.call_price(S, st["K"], max(T_rem - 1.0 / 365.0, 1e-6), r, st["sig"], qdiv))
        cum_theta += st["n_str"] * 2.0 * (c_tom - c_now)  # ≤ 0: decay holding S flat one day

        # ---- scalping overlay ----
        if scalp_model == "grid":
            bars = intraday_by_day.get(d.normalize())
            if bars:                                       # walk the real intraday path of this day
                for (row, ub_b, lb_b) in bars:             # row=(O,H,L,C); ub_b/lb_b = INTRADAY band
                    io, ih, il, ic = row
                    scalp_walk(io, ih, il, ic, ub_b, lb_b, d)   # intraday flat/trend gate
                    res.intraday_bars += 1
            else:                                          # no intraday feed → daily bar + daily band
                scalp_walk(op, hi, lo, S, ub_np[j], lb_np[j], d)
            # ---- залипшие части (when to DROP a working part) ----
            # If price has left the WHOLE grid (all near parts stuck) we HEAL — but ONLY by spending
            # accumulated round-trip profit (doctrine: "unstick with accrued profit, else let the
            # straddle pay"). If there isn't enough booked profit, we CARRY the stuck parts to the
            # roll and the straddle's gamma covers the trend. This is the answer to "when to drop a part".
            if heal_with_profit and GRID.get("legs") and GRID.get("reach", 0) > 0 \
                    and abs(S - GRID["center"]) > GRID["reach"]:
                loss = max(0.0, -scalp_open_mtm(S))       # what closing the stuck legs would realize
                if heal_budget >= loss:                   # enough accrued profit to "heal" them
                    scalp_close_all(S)
                    heal_budget -= loss
                    clean_streak = 0                      # a heal interrupts the уверенный-флет streak
                    setup_grid(S, atr_d, st["n_str"], date=d)  # move the field hospital to the current range
                    res.scalp_heals += 1
                    if trace is not None:
                        trace.append({"t": "scalp_heal", "date": d.date().isoformat(),
                                      "spot": round(float(S), 4), "loss": round(loss, 2)})
                # else: carry the injured parts — the straddle pays (no forced realization)
            if clean_streak >= confident_flat_n:
                res.confident_flat_days += 1
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
        realized_straddle += mtm - fee * (st["n_str"] * 2.0 * c_now + st["base_futs"] * Sf)
        cost_total += fee * (st["n_str"] * 2.0 * c_now + st["base_futs"] * Sf)
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
    # gamma+directional capture = straddle P&L stripped of theta; and how much of theta the scalp
    # must cover for the whole construction to break even (the doctrine says scalp's MIN job ≈100%).
    res.gamma_dir_pnl = realized_straddle - cum_theta
    res.breakeven_scalp_cover_pct = (100.0 * (-realized_straddle) / abs(cum_theta)
                                     if realized_straddle < 0 and abs(cum_theta) > 1e-9 else 0.0)
    return res
