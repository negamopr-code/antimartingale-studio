import numpy as np
import pandas as pd
import pytest

from antimg import atr_strategy as strat


def _week(friday: str, opn: float) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(friday)])
    return pd.DataFrame({"Open": [opn], "High": [opn], "Low": [opn],
                         "Close": [opn], "Volume": [0]}, index=idx)


def _daily(rows):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, *_ in rows])
    return pd.DataFrame({
        "High": [h for _, h, _ in rows],
        "Low": [l for _, _, l in rows],
        "Open": [h for _, h, _ in rows],
        "Close": [h for _, h, _ in rows],
        "Volume": [0] * len(rows),
    }, index=idx)


def test_resolve_win():
    weekly = _week("2020-01-03", 100.0)
    watr = pd.Series([10.0], index=weekly.index)   # up=110, dn=90
    daily = _daily([
        ("2019-12-30", 105, 98),   # no hit
        ("2019-12-31", 112, 104),  # hits up -> win
    ])
    trials = strat.resolve_trials(daily, weekly, watr, mult=1.0)
    assert len(trials) == 1
    assert trials[0].outcome == "win"
    assert trials[0].exit_price == pytest.approx(110)


def test_resolve_loss_first_on_straddle():
    weekly = _week("2020-01-03", 100.0)
    watr = pd.Series([10.0], index=weekly.index)
    daily = _daily([("2019-12-30", 115, 85)])   # touches both -> loss-first
    trials = strat.resolve_trials(daily, weekly, watr, mult=1.0)
    assert len(trials) == 1
    assert trials[0].outcome == "loss"
    assert trials[0].exit_price == pytest.approx(90)


def test_pyramid_win_doubles_loss_resets():
    s, b = strat._apply_pyramid("win", streak=0, bet=1.0, base_bet=1.0,
                                target_streak=10, cap_mult=None)
    assert (s, b) == (1, 2.0)
    s, b = strat._apply_pyramid("loss", streak=3, bet=8.0, base_bet=1.0,
                                target_streak=10, cap_mult=None)
    assert (s, b) == (0, 1.0)


def test_pyramid_target_books_and_resets():
    s, b = strat._apply_pyramid("win", streak=9, bet=512.0, base_bet=1.0,
                                target_streak=10, cap_mult=None)
    assert (s, b) == (0, 1.0)


def test_pyramid_cap():
    s, b = strat._apply_pyramid("win", streak=5, bet=32.0, base_bet=1.0,
                                target_streak=20, cap_mult=8.0)
    assert b == 8.0  # capped below 64


def test_run_linear_all_wins_grows_bank():
    # synthetic uptrend -> every weekly trial wins
    dates = pd.bdate_range("2020-01-01", periods=120)
    price = pd.Series(np.linspace(100, 200, len(dates)), index=dates)
    daily = pd.DataFrame({"Open": price, "High": price * 1.01,
                          "Low": price * 0.999, "Close": price, "Volume": 0})
    from antimg import data
    weekly = data.weekly(daily)
    watr = data.atr(weekly, 5)
    trials = strat.resolve_trials(daily, weekly, watr, mult=1.0)
    assert trials, "expected some trials"
    res = strat.run_linear(trials, base_bet=100, target_streak=10, starting_bank=10000)
    assert res.empirical_p > 0.8
    assert res.final_bank > 10000
