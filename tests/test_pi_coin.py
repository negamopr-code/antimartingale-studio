"""Tests for the ПИ coin estimator (Tab 13)."""
import numpy as np
import pandas as pd
import pytest

from antimg import pi_coin
from antimg import vol as volmod


def _frame(close, rng_pct=0.02):
    idx = pd.bdate_range("2015-01-01", periods=len(close))
    close = pd.Series(close, index=idx, dtype=float)
    op = close.shift(1).fillna(close.iloc[0])
    return pd.DataFrame({"Open": op, "High": np.maximum(close, op) * (1 + rng_pct),
                         "Low": np.minimum(close, op) * (1 - rng_pct), "Close": close,
                         "Volume": 0.0}, index=idx)


def _const_vol(sigma=0.20):
    idx = pd.date_range("2015-01-01", periods=2, freq="D")
    return volmod.VolModel({1.0: pd.Series(sigma, index=idx)}, 0.0, label="constant")


def test_p_net_monotonic_in_coverage():
    """More scalp coverage lowers the breakeven ⇒ p_net is non-decreasing in c."""
    rng = np.random.default_rng(1)
    df = _frame(100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 1500))))
    e = pi_coin.estimate_coin(df, _const_vol(0.3), dte_days=30, c=0.35, cost_drag=0.05)
    ps = [pt["p"] for pt in e.curve]
    assert all(ps[i] <= ps[i + 1] + 1e-9 for i in range(len(ps) - 1))   # monotone non-decreasing
    assert 0.0 <= e.p_net <= 1.0


def test_cheap_vol_raises_winrate():
    """If options are cheap (IV below realized) the breakeven is low ⇒ higher p_net than rich options."""
    rng = np.random.default_rng(2)
    df = _frame(100.0 * np.exp(np.cumsum(rng.normal(0, 0.025, 1500))))   # ~realized 40%/yr
    cheap = pi_coin.estimate_coin(df, _const_vol(0.25), dte_days=30, c=0.35)   # IV<RV → cheap
    rich = pi_coin.estimate_coin(df, _const_vol(0.60), dte_days=30, c=0.35)    # IV>>RV → rich
    assert cheap.rv_over_iv > rich.rv_over_iv
    assert cheap.p_net > rich.p_net


def test_c_star_thresholds_and_loss_bounded():
    rng = np.random.default_rng(3)
    df = _frame(100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 1200))))
    e = pi_coin.estimate_coin(df, _const_vol(0.3), dte_days=30, c=0.35, cost_drag=0.05)
    # c* for 0.60 ≥ c* for 0.55 (need more coverage for a higher win-rate), when both reachable
    if e.c_star_055 >= 0 and e.c_star_060 >= 0:
        assert e.c_star_060 >= e.c_star_055 - 1e-9
    # loss per period is bounded near −(1−c_net): a dead-quiet period loses at most the uncovered rent
    assert e.avg_loss >= -(1.0 - e.c_net) - 0.01


def test_wickiness_and_suggest_monotone():
    calm = _frame(100.0 + np.cumsum(np.random.default_rng(4).normal(0, 0.5, 400)), rng_pct=0.003)
    wicky = _frame(100.0 + np.cumsum(np.random.default_rng(4).normal(0, 0.5, 400)), rng_pct=0.05)
    assert pi_coin.wickiness(wicky) > pi_coin.wickiness(calm)
    assert pi_coin.suggest_c(3.5, 0.7) > pi_coin.suggest_c(1.5, 1.2)   # wicky+MR ⇒ higher suggested c


def test_real_iv_flag_and_vrp_haircut():
    """Real vol-index ⇒ iv_is_real True, no haircut. Proxied (realized) IV ⇒ haircut lowers p_net so it
    isn't falsely flattered (the SPY-vs-everything-else honesty fix)."""
    rng = np.random.default_rng(7)
    df = _frame(100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, 1500))))
    real = volmod.VolModel({1.0: pd.Series(0.25, index=pd.date_range("2015-01-01", periods=2))},
                           0.0, label="index:sp500")
    proxy = volmod.VolModel({1.0: pd.Series(0.25, index=pd.date_range("2015-01-01", periods=2))},
                            0.0, label="realized")
    e_real = pi_coin.estimate_coin(df, real, dte_days=30, c=0.35, vrp_proxy=0.15)
    e_noh = pi_coin.estimate_coin(df, proxy, dte_days=30, c=0.35, vrp_proxy=0.0)
    e_hc = pi_coin.estimate_coin(df, proxy, dte_days=30, c=0.35, vrp_proxy=0.15)
    assert e_real.iv_is_real is True and e_real.vrp_applied == 0.0
    assert e_noh.iv_is_real is False
    assert e_hc.p_net <= e_noh.p_net + 1e-9            # haircut never raises the (proxied) win-rate
    assert e_hc.vrp_applied == pytest.approx(0.15)


def test_api_pi_coin_single_and_scan_keys():
    from fastapi.testclient import TestClient
    from antimg.web.api import app
    c = TestClient(app)
    r = c.post("/api/pi-coin", json={"ticker": "SPY", "start": "2015-01-01", "end": "2022-01-01",
                                     "dte_days": 30, "c": 0.35, "iv_source": "constant", "iv_const": 0.18})
    if r.status_code == 502:
        pytest.skip("price data unavailable")
    assert r.status_code == 200, r.text
    e = r.json()["estimate"]
    assert {"p_net", "c_net", "curve", "c_star_060", "rv_over_iv", "wickiness", "variance_ratio",
            "p_in", "p_out", "payoff_ratio"} <= e.keys()
    assert len(e["curve"]) > 5
