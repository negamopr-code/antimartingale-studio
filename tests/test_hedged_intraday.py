"""Tests for the Прикрытый Интрадей (hedged intraday) backtest engine."""
import numpy as np
import pandas as pd
import pytest

from antimg import data as datamod
from antimg import hedged_intraday as hi


def _frame(close, rng_pct=0.02, drift_open=0.0):
    """OHLC frame from a close path; High/Low straddle Close by rng_pct, Open offset by drift."""
    idx = pd.bdate_range("2018-01-01", periods=len(close))
    close = pd.Series(close, index=idx, dtype=float)
    op = close.shift(1).fillna(close.iloc[0]) * (1 + drift_open)
    return pd.DataFrame({"Open": op, "High": np.maximum(close, op) * (1 + rng_pct),
                         "Low": np.minimum(close, op) * (1 - rng_pct), "Close": close,
                         "Volume": 0.0}, index=idx)


def test_smoke_flat_market():
    df = _frame(np.full(300, 100.0) + np.random.default_rng(1).normal(0, 0.5, 300))
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14),
                                 realized_vol=datamod.realized_vol(df["Close"], 20))
    assert res.n_days > 0
    assert res.table, "should resolve at least one straddle period"
    assert res.total_theta <= 0.0, "a long straddle pays (negative) theta"
    # total = bank + straddle + scalp identity holds at the end
    assert res.final_bank == pytest.approx(res.starting_bank + res.straddle_pnl + res.scalp_pnl, rel=1e-6)


def test_straddle_loss_floored_at_premium():
    """With NO scalping (efficiency 0, no drag) a dead-flat market bleeds the straddle, but a
    single straddle period's straddle leg cannot lose more than the premium paid for it."""
    df = _frame(np.full(200, 100.0), rng_pct=0.001)   # almost no range → near-pure theta bleed
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14),
                                 realized_vol=datamod.realized_vol(df["Close"], 20),
                                 scalp_efficiency=0.0, stuck_penalty=0.0, dte_days=30)
    for row in res.table:
        # straddle P&L for the period is bounded below by −premium (the construction's loss cap)
        assert row["straddle_pnl"] >= -row["premium"] - 1e-6, row


def test_trend_makes_straddle_gamma():
    """A strong sustained trend should make the straddle leg PROFIT (long gamma)."""
    up = 100.0 * np.cumprod(1 + np.full(300, 0.004))   # ~0.4%/day uptrend
    df = _frame(up, rng_pct=0.005, drift_open=0.0)
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14),
                                 realized_vol=datamod.realized_vol(df["Close"], 20),
                                 scalp_efficiency=0.0, stuck_penalty=0.0, dte_days=30)
    assert res.straddle_pnl > 0, "a trend should pay the long straddle via gamma"


def test_range_model_scales_with_efficiency():
    """range model only: scalp income scales with the efficiency knob (frac of reversed range)."""
    rng = np.random.default_rng(3)
    path = 100.0 + np.cumsum(rng.normal(0, 0.3, 300))
    df = _frame(path, rng_pct=0.02)
    lo = hi.run_hedged_intraday(df, datamod.atr(df, 14), scalp_model="range",
                                realized_vol=datamod.realized_vol(df["Close"], 20),
                                scalp_efficiency=0.1)
    himore = hi.run_hedged_intraday(df, datamod.atr(df, 14), scalp_model="range",
                                    realized_vol=datamod.realized_vol(df["Close"], 20),
                                    scalp_efficiency=0.9)
    assert himore.scalp_pnl > lo.scalp_pnl


def test_grid_books_round_trips_on_oscillation():
    """grid model: an oscillating (mean-reverting) market books counter-trend round-trips and
    earns positive scalp P&L; the daily bar is the execution timeframe (no efficiency knob)."""
    rng = np.random.default_rng(7)
    # oscillate around 100 with a wide daily range so the grid completes round-trips
    osc = 100.0 + 6.0 * np.sin(np.arange(400) / 3.0) + rng.normal(0, 0.5, 400)
    df = _frame(osc, rng_pct=0.015)
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14), scalp_model="grid",
                                 realized_vol=datamod.realized_vol(df["Close"], 20),
                                 grid_atr_frac=1.0, dte_days=180)
    assert res.scalp_round_trips > 0, "an oscillating market should complete round-trips"
    assert res.scalp_pnl > 0, "counter-trend round-trips in a range should book positive scalp P&L"


def test_grid_position_bounded_by_intraday_limit():
    """grid model: each working part holds ≤1 leg, so a hard one-way trend can leave parts stuck
    but the straddle loss cap (per period) is still respected — total never goes naked."""
    up = 100.0 * np.cumprod(1 + np.full(260, 0.005))    # relentless uptrend → shorts get stuck
    df = _frame(up, rng_pct=0.004)
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14), scalp_model="grid",
                                 realized_vol=datamod.realized_vol(df["Close"], 20), dte_days=180)
    for row in res.table:
        assert row["straddle_pnl"] >= -row["premium"] - 1e-6, row   # straddle leg cap intact


def test_rolls_happen():
    df = _frame(100.0 + np.random.default_rng(2).normal(0, 0.5, 400))
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14),
                                 realized_vol=datamod.realized_vol(df["Close"], 20),
                                 dte_days=30, roll_buffer_days=5)
    assert res.n_rolls >= 5, "≈400 days / 30-day straddle ⇒ many rolls"
