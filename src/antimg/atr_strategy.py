"""Weekly-entry / daily-resolution ATR antimartingale backtest.

Resolution rule (user-confirmed 2026-05-29):
  - entry = weekly OPEN; ATR = ATR(14) on WEEKLY bars; barriers FIXED at entry:
        up = open + mult*ATR_entry ,  dn = open - mult*ATR_entry  (B-1: fixed)
  - walk DAILY bars chronologically vs the fixed barriers: first high>=up -> WIN,
    first low<=dn -> LOSS; a single day straddling both -> LOSS-first (B-2, conservative);
  - next entry = the next weekly bar opening AFTER the resolution day.

Sizing is antimartingale: bet doubles after a win, resets after a loss, resets after a
streak of `target_streak` wins (booked). `cap_mult` caps the bet (a loss-side lever).

`resolve_trials` is instrument-agnostic (pure win/loss sequence from price). `run_linear`
applies the Δ=1 linear P&L (Tab 2); `run_options` reprices a modeled deep-ITM call and
also returns the auto-computed delta path (Tab 3).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import options as opt


@dataclass
class Trial:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float          # barrier level hit
    atr_entry: float
    outcome: str               # 'win' | 'loss'
    days_held: int


@dataclass
class TrialRecord:
    trial: Trial
    bet: float
    pnl: float                 # net of costs
    bank: float
    streak_after: int


@dataclass
class BacktestResult:
    records: list[TrialRecord] = field(default_factory=list)
    equity_dates: list[pd.Timestamp] = field(default_factory=list)
    equity: list[float] = field(default_factory=list)
    trials: list[Trial] = field(default_factory=list)
    empirical_p: float = 0.0
    n_trials: int = 0
    wins: int = 0
    final_bank: float = 0.0
    max_drawdown: float = 0.0
    closed_form_ev_cycle: float = 0.0
    # options-only
    delta_dates: list[pd.Timestamp] = field(default_factory=list)
    delta_path: list[float] = field(default_factory=list)


def resolve_trials(daily: pd.DataFrame, weekly: pd.DataFrame, weekly_atr: pd.Series,
                   mult: float = 1.0) -> list[Trial]:
    """Produce the data-driven win/loss sequence. No sizing, no instrument."""
    trials: list[Trial] = []
    daily = daily.sort_index()
    wk_index = weekly.index
    pos = 0  # pointer into weekly bars
    while pos < len(wk_index):
        wk_date = wk_index[pos]
        atr_e = weekly_atr.get(wk_date, np.nan)
        entry = weekly["Open"].get(wk_date, np.nan)
        if not np.isfinite(atr_e) or atr_e <= 0 or not np.isfinite(entry):
            pos += 1
            continue
        up = entry + mult * atr_e
        dn = entry - mult * atr_e

        # daily bars from the start of this weekly bar forward
        week_start = wk_date - pd.Timedelta(days=6)   # W-FRI label is the Friday
        future = daily.loc[daily.index >= week_start]
        outcome = None
        exit_date = None
        exit_price = np.nan
        for d, row in future.iterrows():
            hi, lo = row["High"], row["Low"]
            hit_up = hi >= up
            hit_dn = lo <= dn
            if hit_up and hit_dn:
                outcome, exit_price = "loss", dn      # B-2 loss-first
            elif hit_dn:
                outcome, exit_price = "loss", dn
            elif hit_up:
                outcome, exit_price = "win", up
            if outcome:
                exit_date = d
                break
        if outcome is None:
            break  # ran out of data with an open position

        days_held = max((exit_date - future.index[0]).days, 0)
        trials.append(Trial(future.index[0], exit_date, float(entry), float(exit_price),
                            float(atr_e), outcome, days_held))
        # advance to the first weekly bar whose week START is strictly after the
        # resolution day (compare on week-start, NOT the Friday label, else the
        # current week's own label > exit_date re-enters the same week forever).
        week_starts = wk_index - pd.Timedelta(days=6)
        later = wk_index[week_starts > exit_date]
        if len(later) == 0:
            break
        new_pos = wk_index.get_loc(later[0])
        if new_pos <= pos:                  # safety: guarantee forward progress
            new_pos = pos + 1
        pos = new_pos
    return trials


def _apply_pyramid(outcome: str, streak: int, bet: float, base_bet: float,
                   target_streak: int, cap_mult: float | None):
    """Return (next_streak, next_bet) after a resolved trial."""
    if outcome == "win":
        streak += 1
        bet *= 2.0
        if cap_mult is not None:
            bet = min(bet, base_bet * cap_mult)
        if streak >= target_streak:
            streak, bet = 0, base_bet
    else:
        streak, bet = 0, base_bet
    return streak, bet


def _drawdown(equity: list[float]) -> float:
    peak = -np.inf
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return mdd


def run_linear(trials: list[Trial], base_bet: float, target_streak: int,
               commission: float = 0.0, slippage_frac: float = 0.0,
               starting_bank: float = 0.0, cap_mult: float | None = None) -> BacktestResult:
    """Δ=1 linear P&L: a win is +bet, a loss is -bet (1 ATR move == bet)."""
    res = BacktestResult(trials=trials)
    bank = starting_bank
    streak, bet = 0, base_bet
    wins = 0
    for t in trials:
        cost = commission + slippage_frac * bet
        pnl = (bet if t.outcome == "win" else -bet) - cost
        bank += pnl
        streak, bet = _apply_pyramid(t.outcome, streak, bet, base_bet, target_streak, cap_mult)
        wins += t.outcome == "win"
        res.records.append(TrialRecord(t, bet, pnl, bank, streak))
        res.equity_dates.append(t.exit_date)
        res.equity.append(bank)
    _finalize(res, base_bet, target_streak, wins, bank)
    return res


def run_options(trials: list[Trial], daily: pd.DataFrame, realized_vol: pd.Series,
                base_bet: float, target_streak: int, *, r: float = 0.045,
                dte_days: int = 365, target_delta: float = 0.95, q: float = 0.0,
                default_sigma: float = 0.20, commission: float = 0.0,
                slippage_frac: float = 0.0, starting_bank: float = 0.0,
                cap_mult: float | None = None) -> BacktestResult:
    """Same win/loss sequence, but P&L from a modeled deep-ITM call (BS, IV=realized vol).

    Also records the auto-computed delta path across all holding periods for plotting.
    Max loss per trial is the premium (BS price floor) — the doctrine's left-tail lever.
    """
    res = BacktestResult(trials=trials)
    bank = starting_bank
    streak, bet = 0, base_bet
    wins = 0
    close = daily["Close"]
    for t in trials:
        S0 = t.entry_price
        sig0 = _sigma_at(realized_vol, t.entry_date, default_sigma)
        T0 = dte_days / 365.0
        K = opt.strike_for_delta(S0, T0, r, sig0, target_delta, q)
        units = bet / t.atr_entry            # 1 ATR underlying move ~ bet of exposure
        price0 = float(opt.call_price(S0, K, T0, r, sig0, q))

        # delta path over the holding window
        window = close.loc[(close.index >= t.entry_date) & (close.index <= t.exit_date)]
        for d, S in window.items():
            elapsed = (d - t.entry_date).days
            T = max((dte_days - elapsed) / 365.0, 1e-6)
            sig = _sigma_at(realized_vol, d, default_sigma)
            res.delta_dates.append(d)
            res.delta_path.append(float(opt.call_delta(S, K, T, r, sig, q)))

        elapsed = (t.exit_date - t.entry_date).days
        T1 = max((dte_days - elapsed) / 365.0, 1e-6)
        sig1 = _sigma_at(realized_vol, t.exit_date, default_sigma)
        price1 = float(opt.call_price(t.exit_price, K, T1, r, sig1, q))

        cost = commission + slippage_frac * bet
        pnl = (price1 - price0) * units - cost
        bank += pnl
        streak, bet = _apply_pyramid(t.outcome, streak, bet, base_bet, target_streak, cap_mult)
        wins += t.outcome == "win"
        res.records.append(TrialRecord(t, bet, pnl, bank, streak))
        res.equity_dates.append(t.exit_date)
        res.equity.append(bank)
    _finalize(res, base_bet, target_streak, wins, bank)
    return res


def _sigma_at(rv: pd.Series, date: pd.Timestamp, default: float) -> float:
    try:
        v = rv.asof(date)
    except Exception:
        v = np.nan
    return float(v) if v is not None and np.isfinite(v) and v > 0 else default


def _finalize(res: BacktestResult, base_bet: float, target_streak: int,
              wins: int, bank: float) -> None:
    from .simcore import closed_form_ev_cycle
    res.n_trials = len(res.trials)
    res.wins = wins
    res.final_bank = bank
    res.empirical_p = wins / res.n_trials if res.n_trials else 0.0
    res.max_drawdown = _drawdown(res.equity)
    res.closed_form_ev_cycle = closed_form_ev_cycle(base_bet, target_streak, res.empirical_p)
