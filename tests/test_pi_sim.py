"""Tests for the single-period ПИ worked-example simulator (Tab 14, pi_sim).

Encodes the skill INVARIANTS as automated guards so a regression can't pass silently:
  #1  a counter-trend grid on a driftless MEAN-REVERTING (OU) path is net-positive before costs;
  #2  a single straddle period can never lose more than the premium paid (loss cap).
"""
import numpy as np
import pandas as pd
import pytest

from antimg import pi_sim as pisim
from antimg import vol as volmod


def _daily(close):
    idx = pd.bdate_range("2015-01-01", periods=len(close))
    c = pd.Series(close, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                         "Volume": 0.0}, index=idx)


def _const_vol(sigma=0.50):
    idx = pd.date_range("2015-01-01", periods=2, freq="D")
    return volmod.VolModel({1.0: pd.Series(sigma, index=idx)}, 0.0, label="constant")


def _bars_from_points(x, freq="min"):
    """Turn a point series into OHLC bars: each bar spans prev→cur (so it has real intrabar range
    the grid can cross), High/Low = the segment extremes."""
    o = np.concatenate([[x[0]], x[:-1]])
    c = np.asarray(x, float)
    hi = np.maximum(o, c); lo = np.minimum(o, c)
    idx = pd.date_range("2015-01-01", periods=len(x), freq=freq)
    return pd.DataFrame({"Open": o, "High": hi, "Low": lo, "Close": c, "Volume": 0.0}, index=idx)


def _ou_intraday(center=100.0, days=15, theta=0.05, sigma=0.45, seed=7):
    """Driftless Ornstein–Uhlenbeck 1-minute path around `center` (negative return autocorrelation)."""
    rng = np.random.default_rng(seed)
    n = days * 24 * 60
    x = np.empty(n); x[0] = center
    for t in range(1, n):
        x[t] = x[t - 1] + theta * (center - x[t - 1]) + sigma * rng.standard_normal()
    return _bars_from_points(x)


# ---- INVARIANT #1: counter-trend scalp harvests a mean-reverter -------------------------------
def test_ou_meanreverter_scalp_is_positive():
    """The defining test: a counter-trend grid on a driftless mean-reverting series MUST book
    net-positive realized round-trips before costs (it's the definition of harvesting reversion)."""
    intr = _ou_intraday(center=100.0, days=15)
    grid = pisim._build_grid(100.0, first_step=0.8, grid_mult=1.6, n_parts=5, part_lots=1.0)
    realized, open_mtm, rts, net_lots = pisim.measure_scalp_1m(intr, 100.0, grid, n_parts=5)
    assert rts > 20, f"expected many round-trips on an oscillating path, got {rts}"
    assert realized > 0, f"counter-trend scalp must be net-positive on an OU mean-reverter, got {realized:.2f}"
    # stuck legs of a centred mean-reverter mark near flat (it returns to centre)
    assert abs(open_mtm) < realized * 3
    assert abs(net_lots) <= 5 * 1.0 + 1e-9                  # net stuck position bounded by the 5 parts


# ---- INVARIANT #2: loss is capped at the premium paid -----------------------------------------
def test_loss_capped_at_premium_flat_market():
    """A dead-flat market (no move, no scalp) loses EXACTLY the premium — never more."""
    daily = _daily([100.0] * 80)
    res = pisim.simulate(daily, _const_vol(), ticker="TEST", deposit=10_000, start="2015-01-01",
                         dte_days=30, risk_pct=0.10, capture=0.0)
    assert res.straddle_net == pytest.approx(-res.premium_budget, rel=1e-6)
    assert res.straddle_net >= -res.premium_budget - 1e-6
    assert res.total_net >= -res.premium_budget - 1e-6


def test_loss_cap_holds_on_random_paths():
    rng = np.random.default_rng(0)
    for _ in range(8):
        path = 100.0 * np.exp(np.cumsum(rng.standard_normal(120) * 0.02))
        res = pisim.simulate(_daily(path), _const_vol(), ticker="T", deposit=10_000,
                             start="2015-01-01", dte_days=30, risk_pct=0.10, capture=0.0)
        # straddle core (gamma − premium) can never breach the premium floor
        assert res.straddle_net >= -res.premium_budget - 1e-6


# ---- construction / sizing identities ---------------------------------------------------------
def test_synthetic_straddle_sizing_identities():
    daily = _daily(np.linspace(100, 130, 90))
    res = pisim.simulate(daily, _const_vol(), ticker="T", deposit=10_000, start="2015-01-01",
                         dte_days=30, risk_pct=0.10, capture=0.0)
    assert res.premium_budget == pytest.approx(0.10 * 10_000)          # spend = risk% × deposit
    assert res.n_calls == pytest.approx(2.0 * res.straddle_units)      # 2 calls per straddle unit
    assert res.n_futures == pytest.approx(res.straddle_units)          # 1 short future per unit
    assert res.straddle_unit_cost == pytest.approx(2.0 * res.call_price)
    assert res.breakeven_move == pytest.approx(res.straddle_unit_cost)
    # scalp limit = ⅓ of the calls, split into the working parts
    assert res.intraday_limit_lots == pytest.approx(2.0 * res.straddle_units * res.intraday_frac)
    assert res.part_lots == pytest.approx(res.intraday_limit_lots / res.n_parts)


def test_grid_is_exponential_and_symmetric():
    grid = pisim._build_grid(100.0, first_step=1.0, grid_mult=2.0, n_parts=4, part_lots=0.5)
    offs = [g["offset"] for g in grid]
    assert offs == pytest.approx([1.0, 3.0, 7.0, 15.0])               # cumulative 1+2+4+8
    for g in grid:
        assert g["sell"] - 100.0 == pytest.approx(100.0 - g["buy"])   # symmetric around centre


def test_scalp_band_and_conservative_anchor_headline():
    """No intraday feed → headline scalp = the realistic ANCHOR (coverage × theta), not the optimistic
    scenario; the band exposes both, anchor ≤ ceiling here."""
    daily = _daily(np.linspace(100, 112, 90))
    res = pisim.simulate(daily, _const_vol(), ticker="T", deposit=10_000, start="2015-01-01",
                         dte_days=30, risk_pct=0.10, capture=0.20, coverage_anchor=0.15)
    assert res.scalp_source == "anchor"
    assert res.scalp_realistic == pytest.approx(0.15 * res.theta_cost)
    assert res.scalp_income == pytest.approx(res.scalp_realistic)      # conservative headline
    assert res.coverage == pytest.approx(0.15, abs=1e-9)
    assert res.scalp_scenario >= 0


def test_payoff_tilt_envelope_skews_the_v():
    """The scalp's net futures TILT the symmetric straddle V: a net-short overlay lowers the up-wing and
    raises the down-wing; net-long does the opposite (envelope mode when unmeasured)."""
    daily = _daily(np.linspace(100, 100, 90))
    res = pisim.simulate(daily, _const_vol(), ticker="T", deposit=10_000, start="2015-01-01",
                         dte_days=30, risk_pct=0.10, capture=0.0, coverage_anchor=0.15)
    p = res.payoff
    assert p["mode"] == "envelope" and len(p["S"]) == len(p["straddle"])
    iU = max(range(len(p["S"])), key=lambda i: p["S"][i])              # far up
    iD = min(range(len(p["S"])), key=lambda i: p["S"][i])              # far down
    # net-short overlay: worse than the plain straddle on the up move, better on the down move
    assert p["tilt_short"][iU] < p["straddle"][iU]
    assert p["tilt_short"][iD] > p["straddle"][iD]
    assert p["tilt_long"][iU] > p["straddle"][iU]


def test_rolling_edge_aggregates_and_c_star():
    """rolling_edge rolls non-overlapping windows; a flat market → core bleeds (every month −premium),
    c_star = coverage needed to break the core even = mean(−core)/premium."""
    daily = _daily([100.0] * 400)                          # dead flat → every straddle loses its premium
    e = pisim.rolling_edge(daily, _const_vol(), ticker="FLAT", deposit=10_000, dte_days=30,
                           risk_pct=0.10, coverage_anchor=0.15, r=0.045, start="2015-01-01")
    assert e.n_months >= 8
    assert e.core_mean == pytest.approx(-e.premium, rel=1e-6)   # flat → full premium lost each month
    assert e.core_win_pct == 0.0
    assert e.c_star == pytest.approx(1.0, rel=1e-6)            # need 100% coverage to break a dead market even
    assert e.verdict.startswith("нет edge")
    # a strong trender (every 30d window moves far past the ~11% breakeven) → core wins, c_star ≤ 0
    big = _daily([100.0 * (1.02 ** i) for i in range(400)])
    e2 = pisim.rolling_edge(big, _const_vol(), ticker="MOVE", deposit=10_000, dte_days=30,
                            risk_pct=0.10, coverage_anchor=0.15, r=0.045, start="2015-01-01")
    assert e2.core_mean > 0 and e2.c_star <= 0


def test_uptrend_straddle_wins_scalp_bleeds():
    """INVARIANT #3 on the measured path: a strong one-way trend → straddle gamma wins, the
    counter-trend scalp's stuck legs bleed (open_mtm < 0)."""
    # daily uptrend + an intraday path that only rises (pure trend, no reversion)
    daily = _daily(np.linspace(100, 160, 80))
    rng = np.random.default_rng(3)
    up = 100.0 + np.cumsum(np.abs(rng.standard_normal(20 * 24 * 60)) * 0.01)
    intr = _bars_from_points(up)
    res = pisim.simulate(daily, _const_vol(), ticker="T", deposit=10_000, start="2015-01-01",
                         dte_days=30, risk_pct=0.10, intraday=intr, grid_atr_frac=0.05)
    assert res.straddle_net > 0                                       # gamma monetises the trend
    assert (res.scalp_open_mtm or 0.0) <= 0.0                         # stuck shorts in an uptrend bleed
