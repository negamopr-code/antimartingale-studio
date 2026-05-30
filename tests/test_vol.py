"""Vol surface: term-structure interpolation + fixed-β skew + classification. Offline."""
import numpy as np
import pandas as pd

from antimg import vol


def _flat(val):
    idx = pd.date_range("2020-01-01", periods=5, freq="D")
    return pd.Series(val, index=idx)


def test_term_structure_variance_time_interp():
    vm = vol.VolModel({30 / 365: _flat(0.20), 90 / 365: _flat(0.22), 180 / 365: _flat(0.24)})
    d = pd.Timestamp("2020-01-05")
    # exact tenors
    assert abs(vm.atm(d, 30 / 365) - 0.20) < 1e-9
    assert abs(vm.atm(d, 90 / 365) - 0.22) < 1e-9
    # interpolation is linear in total variance σ²·T, not in σ
    v30, v90 = 0.20 ** 2 * (30 / 365), 0.22 ** 2 * (90 / 365)
    Tq = 60 / 365
    expect = (np.interp(Tq, [30 / 365, 90 / 365], [v30, v90]) / Tq) ** 0.5
    assert abs(vm.atm(d, Tq) - expect) < 1e-9
    # flat-σ extrapolation outside the curve
    assert abs(vm.atm(d, 5 / 365) - 0.20) < 1e-9
    assert abs(vm.atm(d, 365 / 365) - 0.24) < 1e-9


def test_single_tenor_is_flat_in_T():
    vm = vol.VolModel({1.0: _flat(0.19)})
    d = pd.Timestamp("2020-01-05")
    assert abs(vm.atm(d, 0.01) - 0.19) < 1e-9
    assert abs(vm.atm(d, 5.0) - 0.19) < 1e-9


def test_skew_lifts_low_strikes_and_lowers_high():
    vm = vol.VolModel({0.25: _flat(0.22)}, skew_beta=-0.18)
    d = pd.Timestamp("2020-01-05")
    atm = vm.sigma(d, 0.25, 100, 100)
    deep_itm = vm.sigma(d, 0.25, 70, 100)   # K<S, m<0, β<0 → higher IV
    otm = vm.sigma(d, 0.25, 130, 100)       # K>S, m>0, β<0 → lower IV
    assert abs(atm - 0.22) < 1e-9
    assert deep_itm > atm > otm
    # additive in ln-moneyness
    assert abs(deep_itm - (0.22 + (-0.18) * np.log(70 / 100))) < 1e-9


def test_skew_floor():
    vm = vol.VolModel({0.25: _flat(0.05)}, skew_beta=-0.18, floor=1e-3)
    d = pd.Timestamp("2020-01-05")
    # a far OTM strike with strong negative skew would go negative without the floor
    assert vm.sigma(d, 0.25, 1e6, 100) >= 1e-3


def test_zero_beta_is_pure_atm():
    vm = vol.VolModel({0.25: _flat(0.22)}, skew_beta=0.0)
    d = pd.Timestamp("2020-01-05")
    for K in (50, 100, 200):
        assert abs(vm.sigma(d, 0.25, K, 100) - 0.22) < 1e-9


def test_classify_and_default_beta():
    assert vol.classify("SPY") == "sp500"
    assert vol.classify("QQQ") == "nasdaq"
    assert vol.classify("GLD") == "gold"
    assert vol.classify("EURUSD=X") == "eurusd"
    assert vol.classify("AAPL") == "other"
    assert vol.default_skew_beta("SPY") < vol.default_skew_beta("GLD") < 0  # equity steeper than gold


def test_build_constant_and_realized_offline():
    # constant → flat const VolModel, no network
    vm = vol.build("ZZZZ", "2020-01-01", iv_source="constant", iv_const=0.3)
    d = pd.Timestamp("2020-06-01")
    assert abs(vm.atm(d, 1.0) - 0.3) < 1e-9
    # realized fallback for an unknown class with index unavailable
    rv = _flat(0.27)
    vm2 = vol.build("ZZZZ", "2020-01-01", iv_source="realized", realized=rv)
    assert abs(vm2.atm(pd.Timestamp("2020-01-05"), 1.0) - 0.27) < 1e-9
    assert vm2.label == "realized"
