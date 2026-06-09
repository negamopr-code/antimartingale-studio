"""Tab 14 — «Симуляция в деньгах»: a single-period, fully-transparent worked example of the
Прикрытый Интрадей (ПИ) method on a real instrument over a real past window.

Unlike the multi-roll backtest (Tab 8), this is ONE construction held for ONE period, with every
number exposed: what you buy, what the synthetic straddle costs, the exponential scalp grid in real
prices, and — for crypto — the scalp income MEASURED by walking the real 1-minute path (the doctrine's
only honestly-measurable instrument). It answers, in dollars: «что конкретно купить, сколько стоит
стреддл, сколько принесёт скальпинг» for e.g. $10k on ETH.

Construction (Korovin, synthetic straddle):
    1 straddle unit = 2 ATM calls − 1 future  →  pays |S_T − S0| at expiry, costs 2·call_premium.
    M straddle units  ⇒  buy 2M calls, short M futures.  Premium budget = risk_pct · deposit = 2·c0·M.
    Three-thirds: the M short futures are the delta-neutral BASE; the intraday scalp LIMIT =
    intraday_frac · (calls count) = intraday_frac · 2M, split into n_parts working parts spaced
    EXPONENTIALLY (step·grid_mult^k) around S0.

Honesty (skill INVARIANTS):
  • Loss is capped at the premium paid (#2): straddle_net ≥ −premium.
  • The scalp can only be MEASURED on an intraday feed (#5); on daily bars it is a labelled SCENARIO
    (capture × Σ daily-range × scalp-lots). For crypto we walk the real 1m path and report the
    measured number alongside, carrying stuck parts (never force-closing — #1).
Not financial advice — educational reproduction of a third-party method.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import options as opt

TRADING_DAYS = 252


@dataclass
class PiSimResult:
    # --- request echo ------------------------------------------------------------------
    ticker: str = ""
    vol_model: str = ""
    deposit: float = 0.0
    risk_pct: float = 0.0
    dte_days: int = 0
    # --- 1. entry snapshot -------------------------------------------------------------
    entry_date: str = ""
    expiry_date: str = ""
    S0: float = 0.0
    K: float = 0.0
    iv: float = 0.0
    r: float = 0.0
    call_price: float = 0.0
    put_price: float = 0.0
    straddle_unit_cost: float = 0.0      # 2·call (cost of one 2C−1F straddle unit)
    premium_budget: float = 0.0          # risk_pct · deposit (what you actually spend)
    straddle_units: float = 0.0          # M
    n_calls: float = 0.0                 # 2M
    n_futures: float = 0.0               # M (short, the delta-neutral base)
    breakeven_move: float = 0.0          # 2·c0 — move needed for the straddle to pay off ($)
    breakeven_pct: float = 0.0
    # --- 2. three-thirds + grid --------------------------------------------------------
    intraday_frac: float = 0.0
    intraday_limit_lots: float = 0.0     # ETH of futures dedicated to scalping (⅓ of calls)
    n_parts: int = 0
    part_lots: float = 0.0               # ETH per working part
    daily_atr: float = 0.0
    first_step: float = 0.0
    grid_mult: float = 0.0
    grid: list[dict] = field(default_factory=list)   # [{part, offset, sell, buy, lots}]
    # --- 3. period outcome -------------------------------------------------------------
    S_T: float = 0.0
    move_abs: float = 0.0
    move_pct: float = 0.0
    n_days: int = 0
    straddle_gross: float = 0.0          # M·|S_T−S0| (intrinsic at expiry)
    theta_cost: float = 0.0              # = premium_budget (the rent, max loss)
    straddle_net: float = 0.0            # gross − premium
    # scalp — presented as a BAND (floor → realistic → optimistic), not a single confident number
    scalp_capture: float = 0.0
    coverage_anchor: float = 0.0         # calibrated realistic coverage (ETH-1m + guru 10–15%/mo)
    sum_daily_range: float = 0.0
    scalp_scenario: float = 0.0          # OPTIMISTIC ceiling: capture × Σ range × limit (no losers)
    scalp_realistic: float = 0.0         # REALISTIC anchor: coverage_anchor × theta (vol-invariant)
    scalp_floor: float | None = None     # MEASURED on real bars (1m crypto = real; 60m else = undercounts)
    scalp_floor_label: str = ""          # "1m" | "60m"
    scalp_realized: float | None = None  # closed round-trips on the measured path (the "flat income")
    scalp_open_mtm: float | None = None  # stuck counter-trend legs marked at S_T (≤0 in a trend)
    scalp_net_lots: float = 0.0          # signed net stuck futures at S_T (+long / −short) — tilts the V
    scalp_round_trips: int = 0
    scalp_source: str = "anchor"         # "1m-measured" (crypto, real) | "anchor" (else, calibrated)
    scalp_income: float = 0.0            # headline contribution used in totals (conservative)
    coverage: float = 0.0                # scalp_income / theta_cost  (≥1 = flat self-pays the rent)
    chop: dict = field(default_factory=dict)        # adaptive chop-scalp model (the trader's know-how)
    chop_diag: dict = field(default_factory=dict)   # MEASURED chop fraction / path (grounds the model)
    payoff: dict = field(default_factory=dict)   # terminal payoff curves (straddle vs scalp-tilted V)
    # --- 4. verdict + presentation -----------------------------------------------------
    total_net: float = 0.0               # straddle_net + scalp_income
    total_pct: float = 0.0               # of deposit
    verdict: str = ""
    steps: list[dict] = field(default_factory=list)   # narrated walkthrough
    timeline: dict = field(default_factory=dict)      # {dates, close} for the chart


def _build_grid(center: float, first_step: float, grid_mult: float, n_parts: int,
                part_lots: float) -> list[dict]:
    """Exponential grid: cumulative offsets step·(1+m+m²+…), symmetric sell/buy levels."""
    rows, acc = [], 0.0
    for k in range(n_parts):
        acc += first_step * (grid_mult ** k)
        rows.append({"part": k + 1, "offset": round(acc, 4),
                     "sell": round(center + acc, 4), "buy": round(center - acc, 4),
                     "lots": round(part_lots, 4)})
    return rows


def measure_scalp_1m(intraday: pd.DataFrame, center: float, grid: list[dict], n_parts: int,
                     *, fee: float = 0.0, bb_window: int = 120, bb_k: float = 2.0) -> tuple[float, float, int, float]:
    """Walk the real 1-minute path with the counter-trend exponential grid (mirrors the Tab-8 engine).

    Response orders: short at each sell-level crossed up, target = the next inner level; long at each
    buy-level crossed down, target = next inner level. Stuck parts are CARRIED (never force-closed) —
    they're marked-to-market at the final price (INVARIANT #1). A Bollinger FLAT-GATE (doctrine: «don't
    fade a galloping market») suspends NEW counter-trend entries on a breakout — no new short above the
    upper band / no new long below the lower band; EXITS are always allowed. Set bb_k=0 to disable.
    Returns (realized, open_mtm, round_trips, net_lots) — net_lots signed (+long / −short stuck legs).
    """
    if intraday is None or intraday.empty:
        return 0.0, 0.0, 0, 0.0
    sell_lv = [g["sell"] for g in grid]
    buy_lv = [g["buy"] for g in grid]
    inner_up = [center] + sell_lv[:-1]   # buy-back target for a short opened at sell_lv[k]
    inner_dn = [center] + buy_lv[:-1]    # sell target for a long opened at buy_lv[k]
    lots = [g["lots"] for g in grid]
    sarm = [True] * n_parts              # short-arm rearmed?
    barm = [True] * n_parts              # long(buy)-arm rearmed?
    legs: list[dict] = []
    realized = 0.0
    rts = 0
    O = intraday["Open"].to_numpy(float); H = intraday["High"].to_numpy(float)
    L = intraday["Low"].to_numpy(float);  C = intraday["Close"].to_numpy(float)
    # FLAT-GATE bands: rolling mean ± k·std of the 1m close (don't open counter-trend INTO a breakout)
    if bb_k > 0 and len(C) >= 5:
        cs = pd.Series(C)
        mid = cs.rolling(bb_window, min_periods=5).mean()
        sd = cs.rolling(bb_window, min_periods=5).std(ddof=0).fillna(0.0)
        ub = (mid + bb_k * sd).to_numpy(); lb = (mid - bb_k * sd).to_numpy()
    else:
        ub = np.full(len(C), np.inf); lb = np.full(len(C), -np.inf)
    for i in range(len(C)):
        o, h, l, c = O[i], H[i], L[i], C[i]
        up_i, lo_i = ub[i], lb[i]
        path = [o, l, h, c] if c >= o else [o, h, l, c]   # intrabar traversal guess
        for a, b in zip(path, path[1:]):
            if b > a:                                      # rising segment
                for k in range(n_parts):
                    lv = sell_lv[k]
                    if sarm[k] and a < lv <= b and not (np.isfinite(up_i) and lv > up_i):
                        legs.append({"side": "S", "k": k, "entry": lv, "target": inner_up[k],
                                     "lots": lots[k]})
                        sarm[k] = False
                for leg in legs[:]:
                    if leg["side"] == "L" and a < leg["target"] <= b:
                        realized += (leg["target"] - leg["entry"]) * leg["lots"]
                        realized -= fee * leg["lots"] * leg["target"]
                        rts += 1; barm[leg["k"]] = True; legs.remove(leg)
            elif b < a:                                    # falling segment
                for k in range(n_parts):
                    lv = buy_lv[k]
                    if barm[k] and b <= lv < a and not (np.isfinite(lo_i) and lv < lo_i):
                        legs.append({"side": "L", "k": k, "entry": lv, "target": inner_dn[k],
                                     "lots": lots[k]})
                        barm[k] = False
                for leg in legs[:]:
                    if leg["side"] == "S" and b <= leg["target"] < a:
                        realized += (leg["entry"] - leg["target"]) * leg["lots"]
                        realized -= fee * leg["lots"] * leg["target"]
                        rts += 1; sarm[leg["k"]] = True; legs.remove(leg)
    S_T = float(C[-1])
    open_mtm = sum(((S_T - leg["entry"]) if leg["side"] == "L" else (leg["entry"] - S_T)) * leg["lots"]
                   for leg in legs)
    net_lots = sum((leg["lots"] if leg["side"] == "L" else -leg["lots"]) for leg in legs)
    return realized, open_mtm, rts, net_lots


def simulate(daily: pd.DataFrame, vol_model, *, ticker: str, deposit: float, start: str,
             dte_days: int, risk_pct: float, n_parts: int = 5, grid_atr_frac: float = 0.5,
             grid_mult: float = 2.0, intraday_frac: float = 1.0 / 3.0, capture: float = 0.20,
             coverage_anchor: float = 0.15, r: float = 0.045, atr_period: int = 14,
             intraday: pd.DataFrame | None = None, intraday_label: str = "1m",
             f_chop: float = 2.0 / 3.0, trades_per_day: float = 10.0, scalp_eff: float = 0.5,
             flat_frac: float = 0.25, vol_label: str = "") -> PiSimResult:
    """Run ONE ПИ period: entry → grid → outcome, every figure exposed. `intraday` (optional 1m/60m
    OHLC) MEASURES the scalp on a real path; the scalp is also reported as a BAND: a realistic anchor
    (coverage_anchor × theta — the vol-invariant primitive, calibrated to the ETH-1m + guru 10–15%/mo)
    and an OPTIMISTIC ceiling (capture × Σ daily-range × limit, no losing days)."""
    res = PiSimResult(ticker=ticker, vol_model=vol_label, deposit=deposit, risk_pct=risk_pct,
                      dte_days=dte_days, intraday_frac=intraday_frac, n_parts=n_parts,
                      grid_mult=grid_mult, scalp_capture=capture, coverage_anchor=coverage_anchor, r=r)
    df = daily.loc[daily.index >= pd.Timestamp(start)]
    if df.empty or len(df) < 2:
        raise ValueError("no data at/after the start date")
    entry = df.index[0]
    expiry_ts = entry + pd.Timedelta(days=dte_days)
    period = df.loc[df.index <= expiry_ts]
    if len(period) < 2:
        raise ValueError("not enough bars inside the period (shorter DTE or earlier start)")
    S0 = float(period["Close"].iloc[0]); K = S0
    S_T = float(period["Close"].iloc[-1])
    T = dte_days / 365.0
    iv = float(vol_model.sigma(entry, T, K, S0)) if vol_model is not None else 0.20

    # --- 1. entry snapshot: synthetic straddle sizing -----------------------------------
    c0 = float(opt.call_price(S0, K, T, r, iv))
    p0 = float(opt.put_price(S0, K, T, r, iv))
    unit_cost = 2.0 * c0                                   # one 2C−1F straddle unit
    budget = risk_pct * deposit
    M = budget / unit_cost if unit_cost > 0 else 0.0       # straddle units
    res.entry_date = entry.date().isoformat(); res.expiry_date = period.index[-1].date().isoformat()
    res.S0, res.K, res.iv, res.call_price, res.put_price = S0, K, iv, c0, p0
    res.straddle_unit_cost = unit_cost; res.premium_budget = budget
    res.straddle_units = M; res.n_calls = 2.0 * M; res.n_futures = M
    res.breakeven_move = unit_cost; res.breakeven_pct = 100.0 * unit_cost / S0 if S0 else 0.0

    # --- 2. three-thirds + exponential grid --------------------------------------------
    atr = _atr(daily, atr_period, entry)
    res.daily_atr = atr
    first_step = grid_atr_frac * atr if atr > 0 else S0 * 0.01
    limit = max(2.0 * M * intraday_frac, 0.0)              # ⅓ of CALLS = the scalp limit (in ETH)
    part_lots = limit / max(1, n_parts)
    res.intraday_limit_lots = limit; res.part_lots = part_lots; res.first_step = first_step
    res.grid = _build_grid(S0, first_step, grid_mult, n_parts, part_lots)

    # --- 3a. straddle core outcome (real price path) -----------------------------------
    move = abs(S_T - S0)
    res.S_T, res.move_abs, res.move_pct = S_T, move, (100.0 * (S_T - S0) / S0 if S0 else 0.0)
    res.n_days = len(period)
    res.straddle_gross = M * move                          # M units × intrinsic |S_T−S0|
    res.theta_cost = budget                                # premium paid = the rent = max loss
    res.straddle_net = res.straddle_gross - budget

    # --- 3b. scalp BAND: realistic anchor · optimistic ceiling · measured floor --------
    rng = (period["High"] - period["Low"]).clip(lower=0).sum()
    res.sum_daily_range = float(rng)
    # OPTIMISTIC ceiling — books `capture` of EVERY day's full range on the WHOLE limit, no losers.
    res.scalp_scenario = float(capture * rng * limit)
    # ADAPTIVE CHOP model (the trader's know-how): ~`trades_per_day` round-trips booking `scalp_eff` of a
    # local flat (`flat_frac` of the daily range) on ONE working part, only while chopping (`f_chop`).
    res.chop = chop_coverage_model(daily_range=atr, part_lots=part_lots, theta=res.theta_cost,
                                   n_days=len(period), f_chop=f_chop, trades_per_day=trades_per_day,
                                   eff=scalp_eff, flat_frac=flat_frac)
    res.chop_diag = measure_chop_diag(
        intraday if (intraday is not None and not intraday.empty) else period,
        is_daily=(intraday is None or intraday.empty))
    # FEASIBILITY: does the real intraday path supply the motion the assumed cadence needs? Only TRUST
    # this on true 1-min bars — 60m bars (≈7/day) badly undercount the path, so the check is unreliable.
    avg_path = res.chop_diag.get("avg_path")
    if avg_path is not None and intraday_label == "1m":
        res.chop["feasible"] = bool(avg_path >= res.chop["path_needed_per_day"])
        res.chop["path_headroom"] = round(avg_path / max(res.chop["path_needed_per_day"], 1e-9), 2)
    measured_chop = res.chop_diag.get("chop_frac")
    res.chop["measured_chop_frac"] = measured_chop
    # measure the scalp on a real path (1m crypto / 60m else) → booked round-trips + the STUCK working
    # parts (open_mtm) — the «net of working parts» the trend strands.
    if intraday is not None and not intraday.empty:
        islice = intraday.loc[(intraday.index >= entry) & (intraday.index <= period.index[-1] + pd.Timedelta(days=1))]
        realized, open_mtm, rts, net_lots = measure_scalp_1m(islice, S0, res.grid, n_parts)
        res.scalp_realized = round(realized, 2); res.scalp_open_mtm = round(open_mtm, 2)
        res.scalp_round_trips = rts; res.scalp_net_lots = round(net_lots, 4)
        res.scalp_floor = round(realized + open_mtm, 2); res.scalp_floor_label = intraday_label
    # NET OF WORKING PARTS: the chop OSCILLATION harvest (scaled to the MEASURED chop fraction, not the
    # assumed f_chop) MINUS the trend-stranded working parts. Prefer the measured stuck mark (gate-managed);
    # else the fixed-grid estimate (all parts the move passed — the «if you keep fading the breakout» case).
    fc_eff = measured_chop if measured_chop is not None else f_chop
    chop_eff = chop_coverage_model(daily_range=atr, part_lots=part_lots, theta=res.theta_cost,
                                   n_days=len(period), f_chop=fc_eff, trades_per_day=trades_per_day,
                                   eff=scalp_eff, flat_frac=flat_frac)["income"]
    stuck_fixed = _stuck_drag_fixed(res.grid, S0, S_T)
    stuck_used = res.scalp_open_mtm if res.scalp_open_mtm is not None else stuck_fixed
    chop_net = chop_eff + min(0.0, stuck_used)
    res.chop["income_effective"] = round(chop_eff, 2)        # oscillation harvest at the MEASURED chop frac
    res.chop["stuck_fixed"] = round(stuck_fixed, 2)          # fixed-grid stranded parts (no flat-gate)
    res.chop["stuck_used"] = round(stuck_used, 2)            # the drag actually netted (measured if available)
    res.chop["net"] = round(chop_net, 2)                     # oscillation − stuck = realistic net scalp
    res.scalp_realistic = res.chop["net"]
    if intraday is not None and not intraday.empty and intraday_label == "1m":
        res.scalp_source = "1m-measured"                    # crypto: the 1m walk IS the measurement
        res.scalp_income = res.scalp_realized + res.scalp_open_mtm
    else:
        res.scalp_source = "anchor"                         # else: chop model NET of working parts
        res.scalp_income = res.scalp_realistic
    res.coverage = (res.scalp_income / res.theta_cost) if res.theta_cost > 0 else 0.0

    # --- 3c. terminal payoff: straddle V vs the scalp-futures-TILTED V ------------------
    res.payoff = _payoff_curves(res)

    # --- 4. totals + verdict ------------------------------------------------------------
    res.total_net = res.straddle_net + res.scalp_income
    res.total_pct = 100.0 * res.total_net / deposit if deposit else 0.0
    res.verdict = _verdict(res)
    res.steps = _narrate(res)
    cl = period["Close"]
    res.timeline = {"dates": [d.date().isoformat() for d in cl.index], "close": [round(float(x), 2) for x in cl.values]}
    return res


def _payoff_curves(r: PiSimResult, n: int = 81) -> dict:
    """Terminal P&L (at expiry) vs underlying S_T for: (a) the clean synthetic straddle (symmetric V =
    M·|S−S0| − premium); (b) the same straddle TILTED by the scalp's net futures position q·(S−S0) —
    the «перекошенный» V. For a measured run q = the actual net stuck lots; otherwise we draw the ENVELOPE
    (q = ±limit) — the band the V can tilt within as parts get stuck short (bearish tilt) or long."""
    S0, M, prem = r.S0, r.straddle_units, r.premium_budget
    span = max(0.30, 1.6 * r.breakeven_pct / 100.0 + abs(r.move_pct) / 100.0)
    S = [S0 * (1.0 - span + 2.0 * span * i / (n - 1)) for i in range(n)]
    straddle = [M * abs(s - S0) - prem for s in S]
    limit = r.intraday_limit_lots
    out = {"S": [round(s, 2) for s in S], "S0": S0, "S_T": r.S_T,
           "straddle": [round(v, 1) for v in straddle], "limit": round(limit, 4)}
    if r.scalp_source == "1m-measured":
        q = r.scalp_net_lots                               # actual net stuck futures (signed)
        out["tilted"] = [round(M * abs(s - S0) - prem + q * (s - S0), 1) for s in S]
        out["q"] = round(q, 4); out["mode"] = "actual"
    else:                                                  # envelope: ±full limit short/long
        out["tilt_short"] = [round(M * abs(s - S0) - prem - limit * (s - S0), 1) for s in S]
        out["tilt_long"] = [round(M * abs(s - S0) - prem + limit * (s - S0), 1) for s in S]
        out["mode"] = "envelope"
    return out


@dataclass
class RollingEdge:
    """Aggregate ПИ economics over many NON-overlapping monthly windows — the 'is there an edge' view.
    The straddle CORE (gamma − theta) is REAL (prices + IV, no assumption); the scalp is the flat anchor
    coverage×theta, so `total` adds one knob. `c_star` = coverage that breaks the core even."""
    ticker: str = ""
    label: str = ""
    group: str = ""
    n_months: int = 0
    deposit: float = 0.0
    premium: float = 0.0                 # per-month premium (= theta = risk·deposit)
    core_mean: float = 0.0               # mean monthly straddle-core P&L ($) — the REAL edge
    core_win_pct: float = 0.0
    core_net: float = 0.0
    scalp_mean: float = 0.0              # = coverage_anchor·premium (flat)
    total_mean: float = 0.0              # core_mean + scalp_mean
    total_win_pct: float = 0.0
    total_net: float = 0.0
    best: float = 0.0
    worst: float = 0.0
    ann_return_pct: float = 0.0          # total_net annualised over the span, % of deposit
    c_star: float = 0.0                  # coverage to break the CORE even (−core_mean/premium)
    rv_over_iv: float = 0.0              # mean realized/implied (the long-vol edge primitive)
    verdict: str = ""
    core_samples: list = field(default_factory=list)   # monthly core P&L (for the histogram)


def rolling_edge(daily: pd.DataFrame, vol_model, *, ticker: str, label: str = "", group: str = "",
                 deposit: float, dte_days: int, risk_pct: float, coverage_anchor: float,
                 r: float, start: str, end: str | None = None) -> RollingEdge:
    """Run NON-overlapping `dte_days` windows from `start` and aggregate the per-month ПИ economics.
    Straddle core = M·|S_T−S0| − premium (real prices + real IV); scalp = coverage_anchor·premium."""
    df = daily.loc[daily.index >= pd.Timestamp(start)]
    if end:
        df = df.loc[df.index <= pd.Timestamp(end)]
    e = RollingEdge(ticker=ticker, label=label, group=group, deposit=deposit,
                    premium=risk_pct * deposit)
    if df.empty or len(df) < 30:
        return e
    T = dte_days / 365.0
    cores, totals, rvs = [], [], []
    cur = df.index[0]
    last = df.index[-1]
    while cur + pd.Timedelta(days=dte_days) <= last:
        win = df.loc[(df.index >= cur) & (df.index <= cur + pd.Timedelta(days=dte_days))]
        if len(win) < 2:
            break
        S0 = float(win["Close"].iloc[0]); S_T = float(win["Close"].iloc[-1])
        iv = float(vol_model.sigma(cur, T, S0, S0)) if vol_model is not None else 0.20
        c0 = float(opt.call_price(S0, S0, T, r, iv))
        unit = 2.0 * c0
        if unit <= 0:
            cur = cur + pd.Timedelta(days=dte_days); continue
        M = e.premium / unit
        core = M * abs(S_T - S0) - e.premium
        cores.append(core); totals.append(core + coverage_anchor * e.premium)
        # realized vol over the window (annualised) vs the IV paid → the long-vol edge primitive
        lr = np.diff(np.log(win["Close"].to_numpy(float)))
        if len(lr) > 1 and np.std(lr) > 0:
            rvs.append(float(np.std(lr) * np.sqrt(252)) / max(iv, 1e-9))
        cur = cur + pd.Timedelta(days=dte_days)
    if not cores:
        return e
    n = len(cores)
    e.n_months = n
    e.core_samples = [round(x, 1) for x in cores]
    e.core_mean = float(np.mean(cores)); e.core_net = float(np.sum(cores))
    e.core_win_pct = 100.0 * sum(1 for x in cores if x > 0) / n
    e.scalp_mean = coverage_anchor * e.premium
    e.total_mean = float(np.mean(totals)); e.total_net = float(np.sum(totals))
    e.total_win_pct = 100.0 * sum(1 for x in totals if x > 0) / n
    e.best = float(np.max(totals)); e.worst = float(np.min(totals))
    span_years = max((df.index[-1] - df.index[0]).days / 365.0, 1e-6)
    e.ann_return_pct = 100.0 * e.total_net / deposit / span_years
    e.c_star = (-e.core_mean / e.premium) if e.premium > 0 else 0.0
    e.rv_over_iv = float(np.mean(rvs)) if rvs else 0.0
    cs = e.c_star
    if cs <= 0:
        e.verdict = "EDGE даже без скальпа (RV>IV)"
    elif cs <= 0.20:
        e.verdict = "edge при реалистичном скальпе"
    elif cs <= 0.50:
        e.verdict = "нужен сильный скальп"
    else:
        e.verdict = "нет edge (тету не отбить)"
    return e


def _stuck_drag_fixed(grid: list[dict], S0: float, S_T: float) -> float:
    """«Net of working parts» for a FIXED (un-adjusted) grid: every working part the net move passed got
    filled counter-trend and is stranded at expiry. Up-move → sell-levels below S_T are stuck short
    (loss = lots·(S_T−level)); down-move → buy-levels above S_T stuck long. This is the «if you keep
    fading the breakout / don't re-center» upper-bound drag (a disciplined flat-gate caps it far lower —
    that's the measured number). Returns ≤ 0."""
    up = S_T > S0
    drag = 0.0
    for g in grid:
        lvl = g["sell"] if up else g["buy"]
        adverse = (S_T - lvl) if up else (lvl - S_T)
        if adverse > 0:
            drag -= g["lots"] * adverse
    return drag


def chop_coverage_model(*, daily_range: float, part_lots: float, theta: float, n_days: int,
                        f_chop: float = 2.0 / 3.0, trades_per_day: float = 10.0, eff: float = 0.5,
                        flat_frac: float = 0.25) -> dict:
    """ADAPTIVE CHOP-SCALP model (the trader's know-how, made conservative & vol-invariant).

    Premise: in a chop phase the trader READS the current realized flat and re-sizes the grid to it —
    booking `eff` (≈50%) of each swing on ONE working part, ~`trades_per_day` times, only while chopping
    (`f_chop` of the time, statistically ~⅔). The local flat the trader scalps is `flat_frac` of the
    instrument's daily range (it ADAPTS per instrument & per regime — when the range widens 0.10→0.30 the
    next working part takes over with a wider TP). So:

        flat_width = flat_frac · daily_range           # the consolidation the trader works (adapts)
        tp         = eff · flat_width                  # take-profit per trade (½ of the swing)
        $/chop-day = trades_per_day · tp · part_lots
        $/period   = n_days · f_chop · $/chop-day
        coverage   = $/period ÷ theta                  # does the flat pay the rent?

    Vol-invariant (INVARIANT #7): flat_width ∝ σ·S and part_lots ∝ premium/(σ·S), so the product ∝
    premium ⇒ coverage depends on trades/day × eff × flat_frac × f_chop, NOT on the instrument's vol.
    Conservative levers: eff (skill), flat_frac (how much of the range is a clean flat), f_chop. The
    `path_needed_per_day = trades_per_day·tp·2` is the FEASIBILITY check — the real intraday path must
    supply at least this much motion (validated against the measured 1m/60m path)."""
    flat_width = flat_frac * max(daily_range, 0.0)
    tp = eff * flat_width
    per_chop_day = trades_per_day * tp * max(part_lots, 0.0)
    income = n_days * f_chop * per_chop_day
    return {
        "flat_frac": flat_frac, "f_chop": f_chop, "trades_per_day": trades_per_day, "eff": eff,
        "flat_width": round(flat_width, 4), "tp": round(tp, 4),
        "per_chop_day": round(per_chop_day, 2), "income": round(income, 2),
        "coverage": round(income / theta, 4) if theta > 0 else 0.0,
        "path_needed_per_day": round(trades_per_day * tp * 2.0, 4),
    }


def measure_chop_diag(bars: pd.DataFrame, *, is_daily: bool, chop_er: float = 0.5) -> dict:
    """Ground the chop model in REAL data: per calendar day compute range (H−L), intraday PATH (Σ|Δclose|,
    only meaningful for intraday bars), and the efficiency ratio ER=|C−O|/path (low ER = oscillation =
    chop). Returns the measured chop-day fraction, avg range, avg path, and path/range — so the user can
    check whether their `f_chop`, `flat_frac` and `trades_per_day` assumptions are realistic here."""
    if bars is None or bars.empty:
        return {}
    g = bars.groupby(bars.index.normalize())
    n = 0; chop = 0; sum_rng = 0.0; sum_path = 0.0; sum_er = 0.0
    for _, day in g:
        H = float(day["High"].max()); L = float(day["Low"].min()); W = H - L
        c = day["Close"].to_numpy(float); o = float(day["Open"].iloc[0])
        if is_daily or len(c) < 3:
            path = W                                          # daily bar: path unknown → use the range
            er = abs(c[-1] - o) / max(W, 1e-9)
        else:
            path = float(np.abs(np.diff(c)).sum())
            er = abs(c[-1] - o) / max(path, 1e-9)
        n += 1; sum_rng += W; sum_path += path; sum_er += er
        if er < chop_er:
            chop += 1
    if n == 0:
        return {}
    return {"n_days": n, "chop_days": chop, "chop_frac": round(chop / n, 3),
            "avg_range": round(sum_rng / n, 4), "avg_path": round(sum_path / n, 4),
            "path_over_range": round((sum_path / n) / max(sum_rng / n, 1e-9), 2),
            "avg_er": round(sum_er / n, 3), "is_daily": is_daily}


def _atr(daily: pd.DataFrame, period: int, asof) -> float:
    """Plain Wilder-ish ATR (mean true range) over `period` bars ending at/just before `asof`."""
    d = daily.loc[daily.index <= asof]
    if len(d) < 2:
        d = daily
    h, l, c = d["High"].to_numpy(float), d["Low"].to_numpy(float), d["Close"].to_numpy(float)
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    n = min(period, len(tr))
    return float(np.mean(tr[-n:])) if n > 0 else 0.0


def _verdict(r: PiSimResult) -> str:
    cov = r.coverage
    asset = r.ticker.split("-")[0]
    head = (f"ПЛЮС +${r.total_net:,.0f}" if r.total_net >= 0 else f"МИНУС ${r.total_net:,.0f}") + \
           f" ({r.total_pct:+.1f}% депозита за {r.n_days} дн)."
    move_txt = (f"{asset} прошёл {r.move_pct:+.1f}% (нужно ≥±{r.breakeven_pct:.1f}%, чтобы стреддл сам вышел в плюс) → "
                f"гамма-ядро {'+' if r.straddle_net>=0 else ''}${r.straddle_net:,.0f}.")
    if r.scalp_source == "1m-measured":
        stuck = r.scalp_open_mtm or 0.0
        cov_txt = (f"Скальп ИЗМЕРЕН по реальному 1-мин пути: {r.scalp_round_trips} кругов +${r.scalp_realized:,.0f} "
                   f"({(max(r.scalp_realized or 0,0))/r.theta_cost*100:.0f}% теты)")
        if stuck < -1.0:
            cov_txt += (f", НО залипшие контр-трендовые части −${abs(stuck):,.0f}: на тренде ФИКСИРОВАННАЯ сетка "
                        f"истекает, а зарабатывает гамма (доктрина). Итог фикс-скальпа ${r.scalp_income:,.0f}.")
        else:
            cov_txt += (". Флет — почти без залипания: "
                        + ("сам окупает тету (≥100%)." if cov >= 1.0 else "часть теты добивает гамма."))
        cm = r.chop
        cov_txt += (f" ⟶ АДАПТИВНО (если ре-центрировать сетку по чопу — know-how трейдера): "
                    f"${cm.get('income',0):,.0f} = {cm.get('coverage',0)*100:.0f}% теты"
                    + (f", и реальный путь это ТЯНЕТ (×{cm.get('path_headroom')} запас, {cm.get('trades_per_day',10):.0f} сделок/день достижимо)"
                       if cm.get('feasible') else "")
                    + f". Разница ${cm.get('income',0)-r.scalp_income:,.0f} = цена ручной подстройки vs «поставил и забыл».")
    else:
        cm = r.chop
        cov_txt = (f"Скальп — АДАПТИВНАЯ ЧОП-МОДЕЛЬ, НЕТТО рабочих частей: осцилляция +${cm.get('income_effective',0):,.0f} "
                   f"(при ИЗМЕРЕННЫХ {(cm.get('measured_chop_frac') or 0)*100:.0f}% чоп-дней) − залипшие части ${cm.get('stuck_used',0):,.0f} "
                   f"= ${r.scalp_realistic:,.0f} = {r.coverage*100:.0f}% теты. "
                   f"Если НЕ следовать flat-гейту (фейдить пробой), части стянуло бы на ${cm.get('stuck_fixed',0):,.0f} — "
                   f"гейт «не фейдь разгон» именно это и спасает.")
    return f"{head} {move_txt} {cov_txt} Макс. риск всё время ограничен премией ${r.theta_cost:,.0f}."


def _narrate(r: PiSimResult) -> list[dict]:
    """The step-by-step «в деньгах» walkthrough the frontend renders as numbered cards."""
    return [
        {"n": 1, "title": "Депозит и бюджет на премию",
         "body": (f"Депозит ${r.deposit:,.0f}. По доктрине на риск за период выделяем {r.risk_pct*100:.0f}% → "
                  f"бюджет на опционы ${r.premium_budget:,.0f}. Это же — наш МАКСИМАЛЬНЫЙ убыток за период.")},
        {"n": 2, "title": "Что покупаем (синтетический стреддл 2C−1F)",
         "body": (f"{r.ticker} на {r.entry_date} = ${r.S0:,.0f}. Берём ATM-страйк ${r.K:,.0f}. "
                  f"Месячный центральный Колл (IV {r.iv*100:.0f}%) стоит ${r.call_price:,.2f}. "
                  f"1 ед. стреддла = 2 Колла − 1 Фьюч = ${r.straddle_unit_cost:,.2f}. "
                  f"На ${r.premium_budget:,.0f} берём {r.straddle_units:.2f} ед. → "
                  f"КУПИТЬ {r.n_calls:.2f} Колла, ПРОДАТЬ {r.n_futures:.2f} Фьюча (дельта-нейтраль).")},
        {"n": 3, "title": "Правило трёх третей → лимит на скальп",
         "body": (f"Из {r.n_calls:.2f} коллов под интрадей выделяем {r.intraday_frac*100:.0f}% = "
                  f"{r.intraday_limit_lots:.2f} {r.ticker.split('-')[0]} лимита, дробим на {r.n_parts} рабочих части по "
                  f"{r.part_lots:.3f} каждая. Остальное — дельта-нейтральная база (тянет в плюс на тренде).")},
        {"n": 4, "title": "Экспоненциальная сетка (реальные цены)",
         "body": ("Расставляем части от центра ${:,.0f}, шаг ×{:g} (первый ≈ {:.1f}% от цены): ".format(
                      r.S0, r.grid_mult, 100.0 * r.first_step / r.S0 if r.S0 else 0.0)
                  + "; ".join(f"ч.{g['part']}: прод ${g['sell']:,.0f} / покуп ${g['buy']:,.0f}" for g in r.grid))},
        {"n": 5, "title": f"Прогон периода ({r.entry_date} → {r.expiry_date})",
         "body": (f"{r.ticker} ушёл с ${r.S0:,.0f} до ${r.S_T:,.0f} ({r.move_pct:+.1f}%). "
                  f"Стреддл-ядро на экспирации: {r.straddle_units:.2f}×${r.move_abs:,.0f} интринсика "
                  f"− ${r.theta_cost:,.0f} премии = {'+' if r.straddle_net>=0 else ''}${r.straddle_net:,.0f}.")},
        {"n": 6, "title": ("Скальпинг — ИЗМЕРЕНО по реальному 1-мин пути" if r.scalp_source == "1m-measured"
                           else "Скальпинг — ОЦЕНКА (полосой: пол → реалистично → потолок)"),
         "body": (
             (f"Прогнали сетку по реальному 1-минутному пути ({r.scalp_round_trips} закрытых кругов): "
              f"booked +${r.scalp_realized:,.0f} = {(max(r.scalp_realized or 0,0))/r.theta_cost*100:.0f}% теты. "
              + (f"Залипшие контр-трендовые части по рынку: ${r.scalp_open_mtm:,.0f} "
                 f"(на тренде скальп в минусе — отрабатывает гамма; части НЕ закрывают силой). "
                 if (r.scalp_open_mtm or 0.0) < -1.0 else
                 f"Залипания почти нет (${r.scalp_open_mtm:,.0f}) — это флет. ")
              + f"Итоговый вклад скальпа: ${r.scalp_income:,.0f}.")
             if r.scalp_source == "1m-measured" else
             (f"Без free 1-мин фида скальп НЕ измеряется напрямую — даю АДАПТИВНУЮ ЧОП-МОДЕЛЬ, НЕТТО рабочих частей:\n"
              f"• осцилляция в чопе: +${r.chop.get('income_effective',0):,.0f} "
              f"({r.chop.get('trades_per_day',10):.0f} сд/день × {r.chop.get('eff',0.5)*100:.0f}% хода, TP ${r.chop.get('tp',0):,.2f} = "
              f"{r.chop.get('flat_frac',0)*100:.0f}% диапазона; ИЗМЕРЕНО чоп {(r.chop.get('measured_chop_frac') or 0)*100:.0f}% дней — НЕ допущение ⅔)\n"
              f"• − залипшие рабочие части: ${r.chop.get('stuck_used',0):,.0f} "
              + ("(flat-гейт сдержал: не фейдим пробой)" if (r.chop.get('stuck_used',0) >= -1) else "(стянуты трендом)")
              + f"  [фикс-сетка без гейта была бы ${r.chop.get('stuck_fixed',0):,.0f} — части на уровнях у входа vs ушедшая цена]\n"
              f"• = НЕТТО скальп ${r.chop.get('net',0):,.0f} = {r.coverage*100:.0f}% теты  ← в итог\n"
              f"• потолок (оптимизм): ${r.scalp_scenario:,.0f}\n"
              f"⚠ покрытие vol-инвариантно (INV #7): сделки×eff×флет×чоп, не воля.")) },
        {"n": 6.5, "title": "🌊 Чоп-модель — проверка реальными данными",
         "body": (
             f"Замер по {'1-мин' if not r.chop_diag.get('is_daily') else 'дневным'} барам: "
             f"в чопе {(r.chop_diag.get('chop_frac') or 0)*100:.0f}% дней (ER<0.5 = колебания > хода); "
             f"средний дневной диапазон ${r.chop_diag.get('avg_range',0):,.2f}"
             + (f", реальный путь/день ${r.chop_diag.get('avg_path',0):,.2f} = ×{r.chop_diag.get('path_over_range',0):.1f} от диапазона. "
                f"Нужно для {r.chop.get('trades_per_day',10):.0f} сделок: ${r.chop.get('path_needed_per_day',0):,.2f}/день → "
                + ("✅ ДОСТИЖИМО" if r.chop.get('feasible') else "⚠ путь маловат")
                + f" (запас ×{r.chop.get('path_headroom','—')})."
                if not r.chop_diag.get('is_daily') else
                ". Путь внутри дня дневные бары не видят — достижимость 10 сделок/день НЕ проверена (нужна крипта 1-мин).")
             + f" Вывод: чоп-модель {'подтверждается' if r.chop.get('feasible') else 'правдоподобна, но не подтверждена'} — "
             f"покрытие {r.coverage*100:.0f}% теты при ЭТИХ допущениях трейдера.")},
        {"n": 7, "title": "Итог в деньгах",
         "body": (f"Ядро {'+' if r.straddle_net>=0 else ''}${r.straddle_net:,.0f} + скальп "
                  f"{'+' if r.scalp_income>=0 else ''}${r.scalp_income:,.0f} = "
                  f"ИТОГО {'+' if r.total_net>=0 else ''}${r.total_net:,.0f} ({r.total_pct:+.1f}% депозита) за {r.n_days} дн. "
                  f"Риск всё это время был ограничен премией ${r.theta_cost:,.0f} (это и есть «безрисковость» — "
                  f"маркетинг: реальный потолок убытка = вся премия).")},
    ]
