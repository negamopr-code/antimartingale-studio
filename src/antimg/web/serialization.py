"""Convert engine objects (pandas, dataclasses) to compact JSON-able payloads.

Series are downsampled to <= max_points so a 30-year daily history stays a light payload.
"""
from __future__ import annotations

import math

import pandas as pd


def _clean(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def downsample(xs: list, ys: list, max_points: int) -> tuple[list, list]:
    n = len(xs)
    if n <= max_points or max_points <= 0:
        return xs, ys
    step = math.ceil(n / max_points)
    return xs[::step], ys[::step]


def series_xy(s: pd.Series, max_points: int) -> dict:
    s = s.dropna()
    xs = [d.isoformat() for d in s.index]
    ys = [_clean(v) for v in s.values]
    xs, ys = downsample(xs, ys, max_points)
    return {"x": xs, "y": ys}


def list_xy(dates: list, values: list, max_points: int) -> dict:
    xs = [d.isoformat() if hasattr(d, "isoformat") else d for d in dates]
    ys = [_clean(v) for v in values]
    xs, ys = downsample(xs, ys, max_points)
    return {"x": xs, "y": ys}


def entries_payload(trials) -> dict:
    wins = [t for t in trials if t.outcome == "win"]
    loss = [t for t in trials if t.outcome == "loss"]
    return {
        "win": {"x": [t.entry_date.isoformat() for t in wins],
                "y": [_clean(t.entry_price) for t in wins]},
        "loss": {"x": [t.entry_date.isoformat() for t in loss],
                 "y": [_clean(t.entry_price) for t in loss]},
    }
