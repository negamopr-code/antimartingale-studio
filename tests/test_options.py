import pytest

from antimg.options import call_delta, call_price, strike_for_delta


def test_deep_itm_delta_near_one():
    assert float(call_delta(S=100, K=10, T=1.0, r=0.04, sigma=0.2)) > 0.99


def test_atm_delta_around_half():
    d = float(call_delta(S=100, K=100, T=1.0, r=0.0, sigma=0.2))
    assert 0.5 < d < 0.62


def test_otm_delta_small():
    assert float(call_delta(S=100, K=200, T=0.5, r=0.04, sigma=0.2)) < 0.05


def test_strike_for_delta_roundtrips():
    S, T, r, sig = 600.0, 1.0, 0.045, 0.18
    K = strike_for_delta(S, T, r, sig, target_delta=0.95)
    assert float(call_delta(S, K, T, r, sig)) == pytest.approx(0.95, abs=1e-3)


def test_call_price_within_bounds():
    S, K, T, r, sig = 100.0, 60.0, 1.0, 0.04, 0.2
    price = float(call_price(S, K, T, r, sig))
    lower = S - K  # intrinsic (undiscounted) lower-ish bound
    assert lower < price < S
