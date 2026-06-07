"""Antimartingale (pyramid-on-wins) overlay applied to a sequence of PERIOD outcomes.

The core project strategy (skill `/antimartingal-strategy`): after a WINNING period double the
position; after a LOSS reset to base; stop (take profit, reset to base) when the win streak hits a
target N. Here we lay that overlay on top of the Hedged-Intraday (ПИ) backtest's per-period P&L
(monthly/quarterly straddle-period results) to ask: does scaling risk on hot streaks add ALPHA?

⚠ SKILL DOCTRINE (sanity-check #3): the antimartingale structure manufactures NO edge on its own —
at a fair coin (p=0.5) the EV identity (2p)^N−1 = 0. Pyramiding helps ONLY if winning periods
genuinely CLUSTER (positive autocorrelation). To separate real clustering from "just leverage on a
positive-mean distribution", we SHUFFLE the period order (destroy time-order, keep the multiset) many
times and compare the real (time-ordered) overlay result to the shuffle distribution. Real ≫ shuffles
⇒ genuine streak alpha; real ≈ shuffle median ⇒ ordering is irrelevant (no streak edge).

Unlike the ATR-pyramid (trailing stop caps every loss at −b), here each period is atomic: a loss while
the multiplier is high costs m×|loss| — the "give-back" form the user asked for ("double the RISK on a
win"). That give-back is exactly why ordering matters and why the shuffle test is the honest check.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class OverlayResult:
    n_periods: int = 0
    target_streak: int = 0
    win_rate: float = 0.0
    flat_total: float = 0.0          # Σ period pnl at base size (ordering-independent)
    am_total: float = 0.0            # antimartingale-scaled total (real time order)
    alpha: float = 0.0               # am_total − flat_total
    flat_max_dd: float = 0.0         # most negative drawdown of the cumulative curve
    am_max_dd: float = 0.0
    max_win_streak: int = 0
    max_mult: float = 1.0            # largest position multiplier reached
    flat_equity: list = field(default_factory=list)
    am_equity: list = field(default_factory=list)
    table: list = field(default_factory=list)
    # shuffle test (ordering = real streak alpha vs artifact)
    n_shuffles: int = 0
    shuffle_median_am: float = 0.0
    shuffle_p05: float = 0.0
    shuffle_p95: float = 0.0
    real_pctile: float = 0.0         # percentile of the real am_total within the shuffle distribution
    shuffle_samples: list = field(default_factory=list)   # AM totals on shuffled order (for histogram)


def _run(pnls: list[float], target_streak: int):
    """Walk the pyramid-on-wins overlay. Returns (am_total, am_dd, rows, max_streak, max_mult)."""
    m = 1
    streak = 0
    am_cum = 0.0
    am_peak = 0.0
    am_dd = 0.0
    rows = []
    max_streak = 0
    max_mult = 1
    for i, pnl in enumerate(pnls):
        contribution = m * pnl
        am_cum += contribution
        am_peak = max(am_peak, am_cum)
        am_dd = min(am_dd, am_cum - am_peak)
        rows.append({"mult": m, "pnl": round(pnl, 2), "contribution": round(contribution, 2),
                     "am_cum": round(am_cum, 2), "streak_before": streak})
        max_mult = max(max_mult, m)
        if pnl > 0:
            streak += 1
            max_streak = max(max_streak, streak)
            if streak >= target_streak:                  # target hit → take profit, reset to base
                m = 1
                streak = 0
            else:
                m *= 2                                   # ride the win: double the risk
        else:
            m = 1                                        # loss → reset to base
            streak = 0
    return am_cum, am_dd, rows, max_streak, max_mult


def apply_overlay(pnls: list[float], target_streak: int = 4, n_shuffles: int = 50,
                  seed: int = 12345) -> OverlayResult:
    res = OverlayResult(n_periods=len(pnls), target_streak=target_streak, n_shuffles=n_shuffles)
    if not pnls:
        return res
    pnls = [float(x) for x in pnls]                  # coerce away numpy types (clean JSON downstream)
    am_total, am_dd, rows, max_streak, max_mult = _run(pnls, target_streak)
    # flat baseline curve + drawdown
    flat_cum = 0.0
    flat_peak = 0.0
    flat_dd = 0.0
    flat_eq = []
    for r in rows:
        flat_cum += r["pnl"]
        flat_peak = max(flat_peak, flat_cum)
        flat_dd = min(flat_dd, flat_cum - flat_peak)
        flat_eq.append(round(flat_cum, 2))
        r["flat_cum"] = round(flat_cum, 2)

    res.flat_total = round(flat_cum, 2)
    res.am_total = round(am_total, 2)
    res.alpha = round(am_total - flat_cum, 2)
    res.flat_max_dd = round(flat_dd, 2)
    res.am_max_dd = round(am_dd, 2)
    res.max_win_streak = max_streak
    res.max_mult = max_mult
    res.win_rate = round(sum(1 for p in pnls if p > 0) / len(pnls), 4)
    res.table = rows
    res.flat_equity = flat_eq
    res.am_equity = [r["am_cum"] for r in rows]

    # shuffle test: same P&Ls, randomized order → does real time-ordering (clustering) beat chance?
    if n_shuffles > 0:
        rng = np.random.default_rng(seed)
        arr = np.array(pnls, dtype=float)
        sims = []
        for _ in range(n_shuffles):
            perm = rng.permutation(arr).tolist()
            sims.append(_run(perm, target_streak)[0])
        sims = np.array(sims)
        res.shuffle_median_am = round(float(np.median(sims)), 2)
        res.shuffle_p05 = round(float(np.percentile(sims, 5)), 2)
        res.shuffle_p95 = round(float(np.percentile(sims, 95)), 2)
        res.real_pctile = round(float((sims < am_total).mean() * 100.0), 1)
        res.shuffle_samples = [round(float(x), 2) for x in sims]
    return res
