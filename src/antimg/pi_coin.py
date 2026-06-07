"""ПИ Coin Estimator (Tab 13) — estimate the net monthly win-rate `p_net` of Прикрытый Интрадей for an
instrument, IN ADVANCE, from observable data, so we can tell whether the antimartingale is justified.

Model (delta-hedged ПИ = variance exposure; see hedgedintraday skill `coin-flip-decomposition.md`):
  per period, in units of the theta (rent):  PnL/θ = (RV/IV)² + c_net − 1
  where (RV/IV)² = the straddle gamma capture (realized vs implied variance), c_net = scalp coverage of
  theta NET of costs. A period WINS ⟺ `(RV/IV)² > 1 − c_net` ⟺ `RV > IV·√(1 − c_net)`.

So `p_net` = the fraction of historical periods whose realized vol beat that breakeven — computed
empirically from the instrument's own RV distribution and its IV (no lognormal assumption). The ONE
lever is `c_net`; we report p_net as a function of it, the critical c* to reach 0.55/0.60, and a
data-driven SUGGESTION for achievable c from intraday-mean-reversion proxies (wickiness, variance ratio).

⚠ Honesty: `c` (intraday scalp coverage) is NOT measurable from daily bars — the suggestion is a proxy.
The robust output is the **curve p_net(c) and the critical c*** ("to be a 0.6 coin you need coverage X");
whether the instrument can deliver X is the mean-reversion question the diagnostics inform, not prove.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import data as datamod
from . import pi_model

TRADING_DAYS = 252


@dataclass
class CoinEstimate:
    ticker: str = ""
    vol_model: str = ""
    n_periods: int = 0
    dte_days: int = 30
    c_net: float = 0.0
    p_net: float = 0.0            # win-rate at c_net
    ev_per_theta: float = 0.0     # mean PnL per period in θ units
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: float = 0.0     # b = avg_win/|avg_loss|
    breakeven_p: float = 0.0      # p* = 1/(1+b)
    # diagnostics (forward-observable)
    rv_mean: float = 0.0
    iv_mean: float = 0.0
    rv_over_iv: float = 0.0       # median RV/IV (>1 ⇒ cheap options ⇒ favourable)
    wickiness: float = 0.0        # mean(H−L)/mean(|C−O|) — intraday-reversion proxy (high ⇒ scalp-able)
    variance_ratio: float = 0.0   # daily VR(63): <1 mean-reverting (good), >1 trending (bad)
    c_suggest: float = 0.0        # heuristic achievable scalp coverage from wickiness+VR
    c_star_055: float = 0.0       # coverage needed for p_net = 0.55
    c_star_060: float = 0.0       # coverage needed for p_net = 0.60
    curve: list = field(default_factory=list)   # [{c, p}] p_net vs coverage
    # walk-forward stability
    p_in: float = 0.0             # p_net on the first half of history
    p_out: float = 0.0            # p_net on the second half (out-of-sample feel)


def _period_g(daily: pd.DataFrame, vol_model, dte_days: int):
    """(RV/IV)² per non-overlapping period of ~dte_days trading days. Returns list of g = (RV/IV)²."""
    close = daily["Close"].astype(float)
    logret = np.log(close / close.shift(1))
    dates = daily.index
    step = max(int(round(dte_days * TRADING_DAYS / 365.0)), 5)   # calendar dte → ~trading bars
    T = dte_days / 365.0
    gs, rvs, ivs = [], [], []
    for p in range(0, len(close) - step, step):
        seg = logret.iloc[p + 1: p + step + 1].dropna()
        if len(seg) < 3:
            continue
        rv = float(seg.std(ddof=1) * np.sqrt(TRADING_DAYS))      # annualized realized vol of the period
        if not np.isfinite(rv) or rv <= 0:
            continue
        iv = None
        if vol_model is not None:
            iv = vol_model.atm(dates[p], T)
        if iv is None or not np.isfinite(iv) or iv <= 0:
            iv = rv                                              # fall back: assume fair (RV/IV=1)
        gs.append((rv / iv) ** 2)
        rvs.append(rv)
        ivs.append(iv)
    return np.array(gs), np.array(rvs), np.array(ivs)


def _coin_at(gs: np.ndarray, c_net: float):
    """Given g=(RV/IV)² per period and net coverage c_net, return (p, ev, avg_win, avg_loss)."""
    pnl = gs + c_net - 1.0                       # PnL/θ per period
    wins = pnl > 0
    p = float(wins.mean()) if len(pnl) else 0.0
    ev = float(pnl.mean()) if len(pnl) else 0.0
    aw = float(pnl[wins].mean()) if wins.any() else 0.0
    al = float(pnl[~wins].mean()) if (~wins).any() else 0.0
    return p, ev, aw, al


def wickiness(daily: pd.DataFrame) -> float:
    """mean(High−Low) / mean(|Close−Open|) — how much price oscillates intraday vs its net daily move.
    High ⇒ lots of intraday reversal ('сопли'/wicks) ⇒ more scalp round-trips available."""
    hi, lo, op, cl = (daily[c].astype(float) for c in ("High", "Low", "Open", "Close"))
    rng = (hi - lo).mean()
    net = (cl - op).abs().mean()
    return float(rng / net) if net > 1e-12 else 0.0


def suggest_c(w: float, vr: float) -> float:
    """HEURISTIC achievable scalp coverage from the intraday-reversion proxies. Anchored so a calm/
    trending name (w≈1.5, VR≥1) → ~0.2 and a wicky mean-reverting name (w≥3, VR<0.8) → ~0.55. NOT a
    measurement — daily bars can't see the intraday scalp; this is a plausibility estimate."""
    base = 0.15 + 0.13 * max(w - 1.5, 0.0)       # wickiness lever (each +1 in w ≈ +0.13 coverage)
    mr = 1.0 if vr <= 0.85 else (0.7 if vr <= 1.0 else 0.4)   # mean-reverting boosts, trending discounts
    return float(min(0.70, max(0.0, base * mr)))


def estimate_coin(daily: pd.DataFrame, vol_model, *, dte_days: int = 30, c: float = 0.35,
                  cost_drag: float = 0.05) -> CoinEstimate:
    """Estimate p_net (and the p_net(c) curve + diagnostics) for one instrument."""
    res = CoinEstimate(dte_days=dte_days)
    gs, rvs, ivs = _period_g(daily, vol_model, dte_days)
    res.n_periods = len(gs)
    if res.n_periods < 4:
        return res
    c_net = max(0.0, c - cost_drag)
    res.c_net = round(c_net, 3)
    p, ev, aw, al = _coin_at(gs, c_net)
    res.p_net, res.ev_per_theta, res.avg_win, res.avg_loss = round(p, 4), round(ev, 4), round(aw, 4), round(al, 4)
    res.payoff_ratio = round(aw / abs(al), 3) if al < -1e-9 else float("inf")
    res.breakeven_p = round(1.0 / (1.0 + res.payoff_ratio), 4) if al < -1e-9 else 0.0
    res.rv_mean = round(float(rvs.mean()), 4)
    res.iv_mean = round(float(ivs.mean()), 4)
    res.rv_over_iv = round(float(np.median(rvs / ivs)), 3)
    res.wickiness = round(wickiness(daily), 3)
    res.variance_ratio = round(pi_model.variance_ratio(daily["Close"].astype(float), 63), 3)
    res.c_suggest = round(suggest_c(res.wickiness, res.variance_ratio), 3)
    # curve p_net(c) and critical coverages for 0.55 / 0.60
    grid = [round(x, 2) for x in np.arange(0.0, 0.81, 0.05)]
    res.curve = [{"c": cc, "p": round(_coin_at(gs, max(0.0, cc - cost_drag))[0], 4)} for cc in grid]
    res.c_star_055 = _c_star(gs, cost_drag, 0.55)
    res.c_star_060 = _c_star(gs, cost_drag, 0.60)
    # walk-forward stability: first vs second half
    h = len(gs) // 2
    res.p_in = round(_coin_at(gs[:h], c_net)[0], 4)
    res.p_out = round(_coin_at(gs[h:], c_net)[0], 4)
    res.ticker = getattr(vol_model, "label", "") and res.ticker  # filled by caller
    return res


def _c_star(gs: np.ndarray, cost_drag: float, target_p: float) -> float:
    """Smallest gross coverage c such that p_net(c) ≥ target_p (None→ -1 if unreachable below 1.0)."""
    for cc in np.arange(0.0, 1.001, 0.01):
        if _coin_at(gs, max(0.0, cc - cost_drag))[0] >= target_p:
            return round(float(cc), 3)
    return -1.0
