"""Synthetic price scenarios for the Explain tab.

Hand-built daily OHLC paths (flat / uptrend / downtrend) so the REAL engine
(`atr_strategy.run_campaign`) can be traced step-by-step on inputs whose outcome is
obvious by eye. The point is transparency: feed the engine a deliberately simple path
and show exactly where it enters, scales in, and exits — proving the logic, not a
re-implementation of it.

Construction: every week is one ATR `rng` wide (so Wilder ATR settles at ~rng and the
grid step h = mult*rng is one week's range). A lead-in of (atr_period-1) oscillating
weeks seeds the ATR while it is still NaN (no campaign can start), so the first campaign
enters exactly on the first 'body' week — the decisive move of the scenario.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

P0 = 100.0          # entry-area price
RNG = 5.0           # weekly true range → ATR ≈ 5, grid step h ≈ 5 (at mult=1)


def _expand_week(monday: pd.Timestamp, o: float, h: float, l: float, c: float) -> pd.DataFrame:
    """5 daily bars (Mon–Fri) whose weekly resample == (o, h, l, c).

    Closes ramp linearly o→c. The weekly HIGH lands on the LAST day of an up week (price
    climbs into the rung and does not reverse), on the FIRST day of a down week; the LOW
    mirrors it. This avoids a spurious intra-week reversal that would trip the freshly
    trailed stop right after a scale-in.
    """
    days = pd.bdate_range(monday, periods=5)
    closes = np.linspace(o, c, 5)
    opens = np.concatenate([[o], closes[:-1]])
    high = np.maximum(opens, closes)
    low = np.minimum(opens, closes)
    if c > o:        hi_day, lo_day = 4, 0      # up week: high at end, low at start
    elif c < o:      hi_day, lo_day = 0, 4      # down week: low at end
    else:            hi_day, lo_day = 1, 3      # flat week: wiggle mid-week
    high[hi_day] = max(high[hi_day], h)
    low[lo_day] = min(low[lo_day], l)
    df = pd.DataFrame({"Open": opens, "High": high, "Low": low, "Close": closes,
                       "Volume": 0.0}, index=days)
    return df


def _build(weeks: list[tuple[float, float, float]], p0: float = P0) -> pd.DataFrame:
    """weeks = list of (high_off, low_off, close_off) relative to each week's open.
    Weeks are chained (next open = prev close). Returns daily OHLCV."""
    monday = pd.Timestamp("2022-01-03")          # a Monday
    frames, o = [], p0
    for hi_off, lo_off, cl_off in weeks:
        frames.append(_expand_week(monday, o, o + hi_off, o + lo_off, o + cl_off))
        o += cl_off
        monday += pd.Timedelta(days=7)
    return pd.concat(frames)


def scenario(name: str, *, atr_period: int = 4, target_streak: int = 4,
             rng: float = RNG) -> pd.DataFrame:
    """Daily OHLCV for 'flat' | 'uptrend' | 'downtrend'.

    Lead-in = (atr_period-1) oscillating weeks (range=rng, net flat) — Wilder ATR is NaN
    here (min_periods=atr_period), so no campaign starts; it just warms ATR up to ~rng.
    The first campaign enters on body[0].
    """
    lead = [(0.5 * rng, -0.5 * rng, 0.0)] * max(atr_period - 1, 1)
    if name == "uptrend":
        body = [(rng, 0.0, rng)] * (target_streak + 2)        # climb a rung/week → target win
    elif name == "downtrend":
        body = [(0.0, -rng, -rng)] * 3                        # first week pierces the stop → −b
    elif name == "flat":
        body = [(0.8 * rng, -0.2 * rng, 0.0),                 # chop: no rung, no stop
                (0.0, -rng, -rng),                            # then dip to the stop → −b
                (0.0, 0.0, 0.0)]
    else:
        raise ValueError(f"unknown scenario {name!r}")
    return _build(lead + body)
