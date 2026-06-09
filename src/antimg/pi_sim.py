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
             vol_label: str = "") -> PiSimResult:
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
    # REALISTIC anchor — coverage is the vol-invariant profitability primitive (INVARIANT #7); anchor it
    # to the one hard 1m calibration (ETH ~0.16 booked) + guru's 10–15%-of-premium/mo. So scalp ≈ c·theta.
    res.scalp_realistic = float(coverage_anchor * res.theta_cost)
    if intraday is not None and not intraday.empty:
        islice = intraday.loc[(intraday.index >= entry) & (intraday.index <= period.index[-1] + pd.Timedelta(days=1))]
        realized, open_mtm, rts, net_lots = measure_scalp_1m(islice, S0, res.grid, n_parts)
        res.scalp_realized = round(realized, 2); res.scalp_open_mtm = round(open_mtm, 2)
        res.scalp_round_trips = rts; res.scalp_net_lots = round(net_lots, 4)
        res.scalp_floor = round(realized + open_mtm, 2); res.scalp_floor_label = intraday_label
        if intraday_label == "1m":                         # crypto: the 1m walk is a REAL measurement
            res.scalp_source = "1m-measured"
            res.scalp_income = realized + open_mtm          # booked round-trips + stuck-leg mark (real)
        else:                                              # 60m undercounts the fine grid → only a floor
            res.scalp_source = "anchor"
            res.scalp_income = res.scalp_realistic
    else:
        res.scalp_source = "anchor"
        res.scalp_income = res.scalp_realistic              # conservative headline (no free intraday feed)
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
            cov_txt += (f", НО залипшие контр-трендовые части −${abs(stuck):,.0f}: на тренде скальп истекает, "
                        f"а зарабатывает гамма стреддла (доктрина, не баг). Итог скальпа ${r.scalp_income:,.0f}.")
        else:
            cov_txt += (". Флет — почти без залипания: "
                        + ("сам окупает тету (≥100%)." if cov >= 1.0 else "часть теты добивает гамма."))
    else:
        floor_txt = (f" Замер на 60-мин барах (грубо недооценивает мелкую сетку) даёт пол ≈${r.scalp_floor:,.0f}."
                     if r.scalp_floor is not None else "")
        cov_txt = (f"Скальп — БЕЗ free 1-мин фида НЕ измеряется. Беру РЕАЛИСТИЧНЫЙ якорь: покрытие "
                   f"{r.coverage_anchor*100:.0f}% теты = ${r.scalp_realistic:,.0f} (калибровка по ETH-1м + гуру 10–15%/мес). "
                   f"Оптимистичный потолок (захват {r.scalp_capture*100:.0f}% дневн.хода на всём лимите, без минусовых дней) "
                   f"= ${r.scalp_scenario:,.0f}.{floor_txt} Беру консервативно якорь.")
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
             (f"Без free 1-мин фида скальп НЕ измеряется честно, поэтому даю ПОЛОСУ:\n"
              + (f"• ПОЛ (замер на 60-мин барах, грубо недооценивает мелкую сетку): ${r.scalp_floor:,.0f}\n" if r.scalp_floor is not None else "")
              + f"• РЕАЛИСТИЧНО (якорь покрытия {r.coverage_anchor*100:.0f}% теты — калибровка по ETH-1м + гуру 10–15%/мес): "
              f"${r.scalp_realistic:,.0f}  ← беру это в итог\n"
              f"• ПОТОЛОК (оптимизм: захват {r.scalp_capture*100:.0f}% дневн.хода на ВСЁМ лимите, без минусовых дней): "
              f"${r.scalp_scenario:,.0f}\n"
              f"⚠ покрытие — vol-инвариантный примитив (INV #7): зависит от кругов/мес × доли захвата, не от воли инструмента.")) },
        {"n": 7, "title": "Итог в деньгах",
         "body": (f"Ядро {'+' if r.straddle_net>=0 else ''}${r.straddle_net:,.0f} + скальп "
                  f"{'+' if r.scalp_income>=0 else ''}${r.scalp_income:,.0f} = "
                  f"ИТОГО {'+' if r.total_net>=0 else ''}${r.total_net:,.0f} ({r.total_pct:+.1f}% депозита) за {r.n_days} дн. "
                  f"Риск всё это время был ограничен премией ${r.theta_cost:,.0f} (это и есть «безрисковость» — "
                  f"маркетинг: реальный потолок убытка = вся премия).")},
    ]
