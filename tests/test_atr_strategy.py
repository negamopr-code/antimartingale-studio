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


def test_costs_roundtrip_pct_commission_and_slippage():
    # both commission and slippage are % of notional, charged per fill x2 (entry+exit).
    # notional = (bet/atr)*price.
    t1 = strat.Trial(pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-02"),
                     100.0, 110.0, 10.0, "win", 1)
    t2 = strat.Trial(pd.Timestamp("2020-01-08"), pd.Timestamp("2020-01-09"),
                     100.0, 90.0, 10.0, "loss", 1)
    res = strat.run_linear([t1, t2], base_bet=100, target_streak=10,
                           commission_pct=0.1, slippage_pct=0.1)
    # t1 bet=100 -> notional 1000 -> 2*0.001*1000 = 2 each ; t2 bet=200 -> notional 2000 -> 4 each
    assert res.total_commission == pytest.approx(6.0)
    assert res.total_slippage == pytest.approx(6.0)
    assert res.total_cost == pytest.approx(12.0)
    assert res.n_cycles == 1
    assert res.cost_as_prob > 0 and res.breakeven_p_with_cost > 0.5


def test_no_costs_zero_prob_drag():
    t = strat.Trial(pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-02"),
                    100.0, 90.0, 10.0, "loss", 1)
    res = strat.run_linear([t], base_bet=100, target_streak=10)
    assert res.total_cost == 0.0
    assert res.cost_as_prob == 0.0 and res.breakeven_p_with_cost == 0.5


def test_long_call_holds_through_dip_where_linear_stops():
    # price dips below -1ATR (would stop a linear pos) then rallies above +1ATR.
    weekly = _week("2020-01-03", 100.0)
    watr = pd.Series([10.0], index=weekly.index)            # up=110, dn=90
    daily = _daily([
        ("2019-12-30", 105, 88),    # dips to 88 (< dn) — linear STOP, call ignores
        ("2019-12-31", 112, 104),   # rallies to 112 (>= up) — target
    ])
    lin = strat.resolve_trials(daily, weekly, watr, mult=1.0)
    call = strat.resolve_trials_long_call(daily, weekly, watr, dte_days=30, mult=1.0)
    assert lin[0].outcome == "loss" and lin[0].exit_reason in ("stop", "straddle")
    assert call[0].outcome == "win" and call[0].exit_reason == "target"


def test_long_call_expiry_is_loss():
    weekly = _week("2020-01-03", 100.0)
    watr = pd.Series([10.0], index=weekly.index)            # up=110
    daily = _daily([
        ("2019-12-30", 104, 96),
        ("2020-03-01", 108, 95),    # never reaches 110; well past a 30-day expiry
    ])
    call = strat.resolve_trials_long_call(daily, weekly, watr, dte_days=30, mult=1.0)
    assert call[0].outcome == "loss" and call[0].exit_reason == "expiry"


def test_campaign_shares_stop_loses_exactly_base():
    # build up 1@100, 2@101, 4@102 (ATR=1), then a -1ATR dip to the trailing stop.
    # by construction the whole stack loses exactly base_bet at the stop.
    import pandas as pd
    wk = pd.DatetimeIndex([pd.Timestamp("2020-01-03")])
    weekly = pd.DataFrame({"Open": [100.0], "High": [100.0], "Low": [100.0],
                           "Close": [100.0], "Volume": [0]}, index=wk)
    watr = pd.Series([1.0], index=wk)            # ATR step = 1
    rows = [
        ("2019-12-30", 100.4, 99.6),   # entry bar, no step, no stop (stop=99)
        ("2019-12-31", 101.4, 100.6),  # +1 step -> add 2 @101
        ("2020-01-02", 102.4, 101.6),  # +1 step -> add 4 @102 ; stop now avg-1/Q
        ("2020-01-06", 102.0, 100.0),  # dips to stop (avg-1/7 = 101.29) -> stop-out
    ]
    daily = _daily(rows)
    res = strat.run_campaign(daily, weekly, watr, base_bet=100, target_streak=10,
                             instrument="shares")
    assert res.n_cycles >= 1
    # the stopped campaign must lose ~ -base_bet (the initial risk), not more
    stop_rows = [r for r in res.table if r.get("reason") == "stop"]
    assert stop_rows, "expected a stop-out"
    assert stop_rows[0]["pnl"] == pytest.approx(-100.0, abs=1.0)


def test_campaign_shares_target_is_big_win():
    import pandas as pd
    wk = pd.DatetimeIndex([pd.Timestamp("2020-01-03")])
    weekly = pd.DataFrame({"Open": [100.0], "High": [100.0], "Low": [100.0],
                           "Close": [100.0], "Volume": [0]}, index=wk)
    watr = pd.Series([1.0], index=wk)
    # a clean run up to +3 steps with target_streak=3 -> big positive
    rows = [("2019-12-30", 100.4, 99.6), ("2019-12-31", 101.2, 100.1),
            ("2020-01-02", 102.2, 101.1), ("2020-01-06", 103.2, 102.1)]
    daily = _daily(rows)
    res = strat.run_campaign(daily, weekly, watr, base_bet=100, target_streak=3,
                             instrument="shares")
    win = [r for r in res.table if r.get("reason") == "target"]
    assert win and win[0]["pnl"] > 100.0    # captured more than one base bet


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
