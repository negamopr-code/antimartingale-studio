"""Tests for the pure long-straddle backtest engine (Tab 10)."""
import numpy as np
import pandas as pd
import pytest

from antimg import data as datamod
from antimg import options
from antimg import pure_straddle as ps
from antimg import vol as volmod


def _frame(close):
    idx = pd.bdate_range("2015-01-01", periods=len(close))
    close = pd.Series(close, index=idx, dtype=float)
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                         "Volume": 0.0}, index=idx)


def _const_vol(sigma=0.20):
    idx = pd.date_range("2015-01-01", periods=2, freq="D")
    return volmod.VolModel({1.0: pd.Series(sigma, index=idx)}, 0.0, label="constant")


# ---- put / straddle pricing -------------------------------------------------------------------

def test_put_call_parity():
    """C − P = S·e^{-qT} − K·e^{-rT} (European, q=0)."""
    S, K, T, r, sig = 100.0, 95.0, 0.5, 0.04, 0.25
    c = float(options.call_price(S, K, T, r, sig))
    p = float(options.put_price(S, K, T, r, sig))
    assert (c - p) == pytest.approx(S - K * np.exp(-r * T), abs=1e-6)


def test_straddle_price_is_call_plus_put():
    S, K, T, r, sig = 100.0, 100.0, 0.25, 0.04, 0.3
    straddle = float(options.straddle_price(S, K, T, r, sig))
    assert straddle == pytest.approx(float(options.call_price(S, K, T, r, sig))
                                     + float(options.put_price(S, K, T, r, sig)), rel=1e-9)
    assert straddle > 0


# ---- engine -----------------------------------------------------------------------------------

def test_flat_market_loses_the_premium():
    """A dead-flat underlying never moves → every straddle expires worthless → we lose ~all premium,
    and the loss is FLOORED at the premium paid (a long option can't lose more than it cost)."""
    df = _frame(np.full(400, 100.0))
    res = ps.run_pure_straddle(df, _const_vol(0.2), risk_pct=0.05, dte_days=30,
                               starting_bank=10_000.0, commission_pct=0, slippage_pct=0)
    assert res.n_periods > 0
    assert res.total_payoff == pytest.approx(0.0, abs=1e-6)     # |S_T−K|=0 every period
    assert res.net_pnl < 0                                       # bled the premium
    for t in res.table:
        assert t.pnl == pytest.approx(-t.premium_paid, abs=1e-6)  # loss == premium (floored)
        assert not t.win


def test_big_move_can_win():
    """A trending underlying that moves far past the breakeven makes the straddle profitable."""
    df = _frame(100.0 * (1.02 ** np.arange(200)))               # +2%/bar compounding ramp
    res = ps.run_pure_straddle(df, _const_vol(0.15), risk_pct=0.05, dte_days=30,
                               starting_bank=10_000.0)
    assert res.n_periods > 0
    assert res.n_wins > 0                                        # large moves beat the premium
    assert res.total_payoff > 0


def test_pnl_identity_and_premium_recovered():
    rng = np.random.default_rng(3)
    df = _frame(100.0 + np.cumsum(rng.normal(0, 1.0, 500)))
    res = ps.run_pure_straddle(df, _const_vol(0.2), risk_pct=0.02, dte_days=45,
                               starting_bank=10_000.0)
    assert res.n_periods > 0
    # final bank = start + sum of per-period pnl (t.pnl is rounded to 2dp → allow rounding slack)
    assert res.final_bank == pytest.approx(res.starting_bank + sum(t.pnl for t in res.table),
                                           abs=0.01 * res.n_periods + 0.01)
    # premium recovered % = payoff / premium (raw accumulators → exact)
    assert res.premium_recovered_pct == pytest.approx(
        100.0 * res.total_payoff / res.total_premium, rel=1e-6)


def test_premium_splits_into_call_and_put_legs():
    """The risk_pct budget buys the WHOLE straddle: call_cost + put_cost == premium_paid (no fees),
    and for ATM the two legs are close to equal."""
    rng = np.random.default_rng(11)
    df = _frame(100.0 + np.cumsum(rng.normal(0, 0.7, 200)))
    res = ps.run_pure_straddle(df, _const_vol(0.2), risk_pct=0.01, dte_days=30,
                               starting_bank=10_000.0, commission_pct=0, slippage_pct=0)
    assert res.n_periods > 0
    for t in res.table:
        assert t.call_cost + t.put_cost == pytest.approx(t.premium_paid, abs=0.02)
        # ATM call & put are within ~25% of each other (call a touch richer via carry)
        assert t.call_cost == pytest.approx(t.put_cost, rel=0.25)
    # and the first period spends ~1% of the bank, NOT 100% (the units bug guard)
    assert res.table[0].premium_paid == pytest.approx(0.01 * 10_000.0, rel=1e-6)


def test_risk_pct_scales_premium():
    """Doubling risk_pct doubles the premium spent on the first straddle (linear in the budget)."""
    df = _frame(100.0 + np.cumsum(np.random.default_rng(7).normal(0, 0.8, 200)))
    a = ps.run_pure_straddle(df, _const_vol(0.2), risk_pct=0.01, dte_days=30,
                             starting_bank=10_000.0, compounding=False)
    b = ps.run_pure_straddle(df, _const_vol(0.2), risk_pct=0.02, dte_days=30,
                             starting_bank=10_000.0, compounding=False)
    assert b.table[0].premium_paid == pytest.approx(2.0 * a.table[0].premium_paid, rel=1e-9)
    # additive (no compounding) → whole P&L stream just doubles
    assert b.net_pnl == pytest.approx(2.0 * a.net_pnl, rel=1e-6)


def test_breakeven_is_premium_over_spot():
    df = _frame(np.full(120, 50.0))
    res = ps.run_pure_straddle(df, _const_vol(0.25), risk_pct=0.01, dte_days=30,
                               starting_bank=10_000.0)
    t = res.table[0]
    # both fields are rounded for display (breakeven_pct 3dp, prem_per_unit 4dp) → allow rounding slack
    assert t.breakeven_pct == pytest.approx(100.0 * t.prem_per_unit / t.spot_entry, abs=1e-2)


def test_streak_counts_helper():
    """_streak_counts groups runs of consecutive wins/losses by length."""
    # W W W L L W  → win runs {3:1, 1:1}, loss runs {2:1}
    win, loss = ps._streak_counts([True, True, True, False, False, True])
    assert win == {1: 1, 3: 1}
    assert loss == {2: 1}
    # all losses
    win, loss = ps._streak_counts([False, False, False])
    assert win == {} and loss == {3: 1}
    # alternating → all length-1 runs
    win, loss = ps._streak_counts([True, False, True, False])
    assert win == {1: 2} and loss == {1: 2}
    assert ps._streak_counts([]) == ({}, {})


def test_engine_reports_streaks_and_counts():
    """The result carries n_losses, max streaks, avg win/loss, and streak distributions that are
    self-consistent (Σ over runs of run_len×count == total wins / losses)."""
    rng = np.random.default_rng(9)
    df = _frame(100.0 + np.cumsum(rng.normal(0, 1.0, 400)))
    res = ps.run_pure_straddle(df, _const_vol(0.2), risk_pct=0.01, dte_days=30, starting_bank=10_000.0)
    assert res.n_wins + res.n_losses == res.n_periods
    assert res.max_win_streak == max(res.win_streaks, default=0)
    assert res.max_loss_streak == max(res.loss_streaks, default=0)
    assert sum(k * v for k, v in res.win_streaks.items()) == res.n_wins
    assert sum(k * v for k, v in res.loss_streaks.items()) == res.n_losses
    if res.n_wins:
        assert res.avg_win > 0
    if res.n_losses:
        assert res.avg_loss < 0


def test_single_leg_call_wins_on_uptrend_put_on_downtrend():
    """A call leg profits on a sustained up-move; a put leg profits on a sustained down-move."""
    up = _frame(100.0 * (1.02 ** np.arange(200)))
    down = _frame(100.0 * (0.98 ** np.arange(200)))
    call_up = ps.run_single_leg(up, _const_vol(0.15), leg="call", risk_pct=0.05, dte_days=30)
    put_up = ps.run_single_leg(up, _const_vol(0.15), leg="put", risk_pct=0.05, dte_days=30)
    assert call_up.n_wins > 0 and call_up.net_pnl > 0          # calls cash in on the rally
    assert put_up.total_payoff == pytest.approx(0.0, abs=1e-6)  # puts expire worthless in an uptrend
    call_dn = ps.run_single_leg(down, _const_vol(0.15), leg="call", risk_pct=0.05, dte_days=30)
    put_dn = ps.run_single_leg(down, _const_vol(0.15), leg="put", risk_pct=0.05, dte_days=30)
    assert put_dn.n_wins > 0 and put_dn.net_pnl > 0           # puts cash in on the decline
    assert call_dn.total_payoff == pytest.approx(0.0, abs=1e-6)


def test_single_leg_streaks_and_first_period_is_one_pct():
    rng = np.random.default_rng(13)
    df = _frame(100.0 + np.cumsum(rng.normal(0, 1.0, 400)))
    for leg in ("call", "put"):
        res = ps.run_single_leg(df, _const_vol(0.2), leg=leg, risk_pct=0.01, dte_days=30,
                                starting_bank=10_000.0)
        assert res.n_wins + res.n_losses == res.n_periods
        assert sum(k * v for k, v in res.win_streaks.items()) == res.n_wins
        assert sum(k * v for k, v in res.loss_streaks.items()) == res.n_losses
        assert res.table[0].premium_paid == pytest.approx(0.01 * 10_000.0, rel=1e-6)
        # the leg cost lands in the right column
        t0 = res.table[0]
        if leg == "call":
            assert t0.call_cost == t0.premium_paid and t0.put_cost == 0.0
        else:
            assert t0.put_cost == t0.premium_paid and t0.call_cost == 0.0


def test_api_leg_analysis_endpoint():
    from fastapi.testclient import TestClient
    from antimg.web.api import app
    c = TestClient(app)
    r = c.post("/api/leg-analysis", json={"ticker": "SPY", "start": "2015-01-01", "end": "2020-01-01",
                                          "risk_pct": 0.01, "dte_days": 30, "iv_source": "constant",
                                          "iv_const": 0.18})
    if r.status_code == 502:
        pytest.skip("price data unavailable in this environment")
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d.keys()) >= {"call", "put", "ticker", "vol_model"}
    for leg in ("call", "put"):
        assert d[leg]["summary"]["n_periods"] > 0
        assert "win_streaks" in d[leg] and "loss_streaks" in d[leg]
        assert len(d[leg]["table"]) == d[leg]["summary"]["n_periods"]


def test_coinflip_loss_capped_at_minus_R():
    """In coin-flip mode a flat market never moves → every trial loses its risk R (capped at −R)."""
    df = _frame(np.full(500, 100.0))
    res = ps.run_coinflip_trials(df, _const_vol(0.2), leg="straddle", risk_pct=0.05, dte_days=30,
                                 starting_bank=10_000.0, max_rolls=50)
    assert res.n_trials > 0
    assert res.n_wins == 0                                   # nothing moves → no trial reaches +R
    for t in res.trials:
        assert not t.win
        assert t.cum_pnl == pytest.approx(-t.R, rel=1e-6)    # loss capped at exactly −R
        assert t.n_rolls == 1                                # a fully-worthless straddle wipes R in one roll


def test_coinflip_horizon_closes_partial_and_continues():
    """A small-vol drift makes trials grind without hitting ±R; the horizon must close them partial
    (booking actual cum) so the timeline isn't swallowed by one never-ending trial."""
    rng = np.random.default_rng(2)
    df = _frame(100.0 + np.cumsum(rng.normal(0.0, 0.25, 2000)))   # tiny moves → straddles seldom resolve
    short = ps.run_coinflip_trials(df, _const_vol(0.15), leg="straddle", risk_pct=0.02, dte_days=20,
                                   starting_bank=10_000.0, max_rolls=6)
    long = ps.run_coinflip_trials(df, _const_vol(0.15), leg="straddle", risk_pct=0.02, dte_days=20,
                                  starting_bank=10_000.0, max_rolls=60)
    assert short.n_trials > long.n_trials                    # tighter horizon → more (shorter) trials
    assert short.n_partial > 0                               # some trials closed by the horizon
    for t in short.trials:
        assert t.n_rolls <= 6                                # no trial exceeds the horizon
        if t.partial:
            assert abs(t.cum_pnl) < t.R + 1e-6              # partial = booked before reaching ±R
    # the last trial ends near the end of the data (timeline not swallowed by one endless trial)
    assert pd.Timestamp(short.trials[-1].end_date) > pd.Timestamp(short.trials[0].end_date)


def test_coinflip_wins_can_overshoot_and_streaks_consistent():
    """A win books actual cum (≥ +R, may overshoot); streak counts are self-consistent."""
    rng = np.random.default_rng(21)
    df = _frame(100.0 + np.cumsum(rng.normal(0.05, 1.2, 1500)))   # drifting, volatile → some wins
    res = ps.run_coinflip_trials(df, _const_vol(0.25), leg="straddle", risk_pct=0.02, dte_days=30,
                                 starting_bank=10_000.0, max_rolls=60)
    assert res.n_trials > 0
    assert res.n_wins + res.n_losses == res.n_trials
    for t in res.trials:
        if t.partial:
            continue                                         # horizon close → cum booked as-is (not ±R)
        if t.win:
            assert t.cum_pnl >= t.R - 1e-6                   # win reaches at least +R (can overshoot)
        else:
            assert t.cum_pnl == pytest.approx(-t.R, rel=1e-6)
    assert sum(k * v for k, v in res.win_streaks.items()) == res.n_wins
    assert sum(k * v for k, v in res.loss_streaks.items()) == res.n_losses
    assert res.max_rolls >= 1 and res.avg_rolls >= 1.0
    # bank identity
    assert res.final_bank == pytest.approx(
        res.starting_bank + sum(t.cum_pnl for t in res.trials), abs=0.01 * res.n_trials + 0.01)


def test_coinflip_partial_loss_is_carried_not_a_full_loss():
    """A trial that takes several rolls to reach −R should appear (n_rolls>1 somewhere): a partial
    loss is carried into the next roll instead of resolving immediately."""
    rng = np.random.default_rng(5)
    df = _frame(100.0 + np.cumsum(rng.normal(0, 0.6, 1200)))
    res = ps.run_coinflip_trials(df, _const_vol(0.15), leg="straddle", risk_pct=0.02, dte_days=20,
                                 starting_bank=10_000.0)
    assert res.n_trials > 0
    assert res.max_rolls >= 2                                # at least one trial rolled more than once


def test_api_coinflip_modes_for_straddle_and_legs():
    from fastapi.testclient import TestClient
    from antimg.web.api import app
    c = TestClient(app)
    base = {"ticker": "SPY", "start": "2012-01-01", "end": "2022-01-01", "risk_pct": 0.01,
            "dte_days": 30, "iv_source": "constant", "iv_const": 0.18, "resolution": "coinflip"}
    r = c.post("/api/pure-straddle", json=base)
    if r.status_code == 502:
        pytest.skip("price data unavailable")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] == "coinflip" and d["summary"]["n_trials"] > 0
    assert {"avg_rolls", "max_rolls"} <= d["summary"].keys()
    assert d["table"][0]["R"] > 0 and "cum_pnl" in d["table"][0]
    r2 = c.post("/api/leg-analysis", json=base)
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["mode"] == "coinflip"
    for leg in ("call", "put"):
        assert d2[leg]["summary"]["n_trials"] > 0
        assert "win_streaks" in d2[leg]


def test_api_pure_straddle_endpoint():
    from fastapi.testclient import TestClient
    from antimg.web.api import app
    c = TestClient(app)
    # constant IV avoids any network fetch of vol indices; price data still fetched for the ticker
    r = c.post("/api/pure-straddle", json={"ticker": "SPY", "start": "2015-01-01", "end": "2020-01-01",
                                           "risk_pct": 0.01, "dte_days": 30, "iv_source": "constant",
                                           "iv_const": 0.18})
    if r.status_code == 502:
        pytest.skip("price data unavailable in this environment")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["summary"]["n_periods"] > 0
    assert {"net_pnl", "premium_recovered_pct", "avg_breakeven_pct", "avg_move_pct"} <= d["summary"].keys()
    assert len(d["table"]) == d["summary"]["n_periods"]
    assert len(d["equity"]) >= 2
