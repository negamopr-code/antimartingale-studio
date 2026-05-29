"""Coin-flip antimartingale core (Tab 1).

Improved port of the user's original Tkinter `Simulation` class:
  - bet doubles after every WIN, resets to base after every LOSS;
  - a cycle ends either on a loss (failure) or on reaching `streak_target` (success);
  - on success we BOOK the pyramid and CONTINUE (the original `break`-on-first-target
    stopped the whole run — that hid the long-run behaviour, so it's replaced by a
    proper cycle loop). Set `stop_at_first_target=True` to recover the old behaviour.

Bugs fixed vs the original:
  - `series_counter` is no longer pre-seeded with a misleading {1..50} dict; it is a
    plain Counter over the streak length at which each cycle ended (0 == immediate loss).
  - `series_counter` is actually surfaced in the result (the GUI never showed it before).

Doctrine (see SKILL.md):
  every failed cycle costs exactly -b (b*(2^k-1) - 2^k*b = -b, independent of k),
  so E[cycle] = p^N * b*(2^N - 1) + (1 - p^N)*(-b) = b * ((2p)^N - 1).
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class CoinFlipResult:
    history: list[float] = field(default_factory=list)        # bank after each trial
    series_counter: Counter = field(default_factory=Counter)  # terminal streak -> #cycles
    last_series: list[float] = field(default_factory=list)    # bank path of last successful cycle
    cumulative_bank: float = 0.0
    total_iterations: int = 0
    cycles: int = 0
    successes: int = 0          # cycles that reached streak_target

    # analytics (filled by analytics())
    empirical_p: float = 0.0
    closed_form_ev_cycle: float = 0.0
    empirical_ev_cycle: float = 0.0


def closed_form_ev_cycle(base_bet: float, streak_target: int, win_prob: float) -> float:
    """E[cycle] = b * ((2p)^N - 1)."""
    return base_bet * ((2.0 * win_prob) ** streak_target - 1.0)


def expected_trades_per_cycle(streak_target: int, win_prob: float) -> float:
    """E[#trials in a cycle] for the truncated-geometric pyramid.

    A cycle is a run of wins terminated by either a loss or by reaching N wins.
    E[trials] = sum_{k=0}^{N-1} p^k  =  (1 - p^N)/(1 - p)   (p != 1)
    """
    p = win_prob
    if p >= 1.0:
        return float(streak_target)
    return (1.0 - p ** streak_target) / (1.0 - p)


class Simulation:
    """Stateful so `continuous` mode can chain runs (preserves cumulative_bank)."""

    def __init__(self) -> None:
        self.reset_all()

    def reset_all(self) -> None:
        self.total_iterations = 0
        self.cumulative_bank = 0.0
        self.history: list[float] = []
        self.series_counter: Counter = Counter()
        self.last_series: list[float] = []
        self.cycles = 0
        self.successes = 0

    def simulate(
        self,
        iterations: int,
        streak_target: int,
        base_bet: float,
        win_prob: float,
        mode: str = "separate",
        stop_at_first_target: bool = False,
        seed: int | None = None,
    ) -> CoinFlipResult:
        if mode == "separate":
            self.reset_all()

        rng = random.Random(seed)
        streak = 0
        bank = self.cumulative_bank
        bet = base_bet
        progress: list[float] = []
        cycle_start_idx = len(self.history)

        for _ in range(iterations):
            self.total_iterations += 1
            win = 1 if rng.random() < win_prob else -1
            bank += bet * win
            self.cumulative_bank = bank
            progress.append(bank)

            if win == 1:
                streak += 1
                bet *= 2
                if streak == streak_target:
                    self.series_counter[streak] += 1
                    self.cycles += 1
                    self.successes += 1
                    self.last_series = progress[cycle_start_idx:]
                    streak = 0
                    bet = base_bet
                    cycle_start_idx = len(progress)
                    if stop_at_first_target:
                        break
            else:
                self.series_counter[streak] += 1   # loss after `streak` wins (0 == immediate)
                self.cycles += 1
                streak = 0
                bet = base_bet
                cycle_start_idx = len(progress)

        self.history.extend(progress)
        return self.analytics(base_bet, streak_target, win_prob)

    def analytics(self, base_bet: float, streak_target: int, win_prob: float) -> CoinFlipResult:
        res = CoinFlipResult(
            history=self.history,
            series_counter=self.series_counter,
            last_series=self.last_series,
            cumulative_bank=self.cumulative_bank,
            total_iterations=self.total_iterations,
            cycles=self.cycles,
            successes=self.successes,
        )
        res.closed_form_ev_cycle = closed_form_ev_cycle(base_bet, streak_target, win_prob)
        res.empirical_p = win_prob
        if self.cycles:
            res.empirical_ev_cycle = self.cumulative_bank / self.cycles
        return res
