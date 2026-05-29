import math

import pytest

from antimg.simcore import (
    Simulation,
    closed_form_ev_cycle,
    expected_trades_per_cycle,
)


@pytest.mark.parametrize("N", [1, 5, 10, 20])
def test_fair_coin_ev_is_zero(N):
    assert closed_form_ev_cycle(1.0, N, 0.5) == pytest.approx(0.0, abs=1e-12)


def test_ev_sign_tracks_edge():
    assert closed_form_ev_cycle(1.0, 10, 0.51) > 0
    assert closed_form_ev_cycle(1.0, 10, 0.49) < 0


def test_expected_trades_formula():
    p, N = 0.5, 10
    assert expected_trades_per_cycle(N, p) == pytest.approx((1 - p**N) / (1 - p))


def test_certain_win_books_full_pyramid():
    # p=1, exactly N flips => one successful cycle worth b*(2^N - 1)
    b, N = 1.0, 8
    sim = Simulation()
    res = sim.simulate(iterations=N, streak_target=N, base_bet=b, win_prob=1.0, seed=0)
    assert res.successes == 1
    assert res.cumulative_bank == pytest.approx(b * (2**N - 1))


def test_certain_loss_costs_base_each_trial():
    # p=0 => every trial is an immediate loss of exactly -b (doctrine: loss == -b)
    b, k = 3.0, 25
    sim = Simulation()
    res = sim.simulate(iterations=k, streak_target=10, base_bet=b, win_prob=0.0, seed=0)
    assert res.cumulative_bank == pytest.approx(-k * b)
    assert res.series_counter[0] == k


def test_continuous_mode_chains_bank():
    sim = Simulation()
    sim.simulate(5, 10, 1.0, 1.0, mode="separate", seed=1)
    first = sim.cumulative_bank
    sim.simulate(5, 10, 1.0, 1.0, mode="continuous", seed=2)
    assert sim.cumulative_bank > first  # bank carried over and grew


def test_fair_coin_empirical_ev_near_zero():
    sim = Simulation()
    # long-run study: keep booking cycles instead of stopping at the first target streak
    res = sim.simulate(200000, 10, 1.0, 0.5, seed=42, stop_at_first_target=False)
    # per-trade EV must hover near zero for a fair coin
    per_trade = res.cumulative_bank / res.total_iterations
    assert abs(per_trade) < 0.2
