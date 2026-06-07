"""Tests for the antimartingale overlay (Tab 12) applied to a period-P&L sequence."""
import numpy as np
import pytest

from antimg import am_overlay as amov


def test_pyramid_doubles_on_win_resets_on_loss():
    # base size 1; after each win mult doubles until target_streak, then resets; loss resets.
    pnls = [10, 10, 10, -5, 10]
    r = amov.apply_overlay(pnls, target_streak=3, n_shuffles=0)
    mults = [row["mult"] for row in r.table]
    assert mults == [1, 2, 4, 1, 1]          # win,win,win(target3 hit→reset),loss(reset),win(base)
    # contributions: 1*10, 2*10, 4*10, 1*-5, 1*10
    assert [row["contribution"] for row in r.table] == [10, 20, 40, -5, 10]
    assert r.flat_total == pytest.approx(sum(pnls))
    assert r.am_total == pytest.approx(10 + 20 + 40 - 5 + 10)
    assert r.max_mult == 4 and r.max_win_streak == 3


def test_loss_after_streak_gives_back_at_high_mult():
    """The give-back form: a loss while the multiplier is high costs mult×|loss| (no intra-period stop)."""
    pnls = [10, 10, -100]                      # win,win → mult 4 on the loss
    r = amov.apply_overlay(pnls, target_streak=5, n_shuffles=0)
    assert [row["mult"] for row in r.table] == [1, 2, 4]
    assert r.table[-1]["contribution"] == pytest.approx(-400.0)   # 4 × −100


def test_shuffle_detects_clustering():
    """Clustered wins → real time-order beats shuffles (high percentile); shuffled distribution lower."""
    pnls = ([10, 10, 10, -5, -5, -5] * 4)
    r = amov.apply_overlay(pnls, target_streak=3, n_shuffles=200, seed=1)
    assert r.real_pctile >= 90                 # real clustering exploited by the pyramid
    assert r.am_total > r.shuffle_median_am
    assert len(r.shuffle_samples) == 200


def test_shuffle_neutral_when_iid():
    """On i.i.d. (no clustering) the real percentile is ~uniform → averaged over draws it lands mid
    (no systematic streak edge). A single draw can be anywhere, so average over several."""
    pctiles = []
    for seed in range(8):
        rng = np.random.default_rng(seed)
        pnls = rng.normal(0.5, 5.0, 400).round(2).tolist()
        pctiles.append(amov.apply_overlay(pnls, target_streak=4, n_shuffles=120, seed=seed + 100).real_pctile)
    assert 25 < (sum(pctiles) / len(pctiles)) < 75   # no systematic clustering edge across i.i.d. draws


def test_flat_total_is_order_independent():
    pnls = [3, -1, 4, -1, 5, -9, 2]
    a = amov.apply_overlay(pnls, target_streak=3, n_shuffles=0)
    b = amov.apply_overlay(list(reversed(pnls)), target_streak=3, n_shuffles=0)
    assert a.flat_total == pytest.approx(b.flat_total)   # flat = Σ pnl regardless of order


def test_api_antimartingale_endpoint():
    from fastapi.testclient import TestClient
    from antimg.web.api import app
    c = TestClient(app)
    r = c.post("/api/hedged-intraday/antimartingale", json={
        "ticker": "SPY", "start": "2012-01-01", "end": "2022-01-01", "am_period": "monthly",
        "target_streak": 4, "n_shuffles": 30, "iv_source": "constant", "iv_const": 0.18,
        "scalp_model": "capture", "scalp_capture": 0.2})
    if r.status_code == 502:
        pytest.skip("price data unavailable")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["summary"]["n_periods"] > 3
    assert {"flat_total", "am_total", "alpha", "real_pctile", "max_mult"} <= d["summary"].keys()
    assert len(d["table"]) == d["summary"]["n_periods"]
    assert len(d["shuffle_samples"]) == 30
