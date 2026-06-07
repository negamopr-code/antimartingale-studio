"""Pure long-straddle backtest (Tab 10) — NO intraday scalping, NO early rolling.

The simplest possible long-volatility test: every period spend a fixed % of the deposit on an
**ATM straddle** (long call + long put, strike = spot), hold it to **expiration**, and settle at
intrinsic value |S_T − K|. Then roll into a fresh ATM straddle at the new spot and repeat.

This isolates "what does the straddle alone cost us, and is holding it to expiry profitable?" —
the question Tab 8 (Hedged Intraday) answers WITH a scalp overlay. Here there is no overlay, so
the result is the raw economics of being long gamma/vega and paying theta to expiry.

⚠ DATA HONESTY: we have no historical option-chain feed. The entry premium is a **Black-Scholes
model price** using the real underlying price + a modeled implied vol (vol.VolModel: realized vol
or the CBOE term structure). The expiration payoff |S_T − K| uses the REAL price path and needs no
model. Because IV is usually ≥ realized vol (the variance-risk premium), a buy-and-hold straddle is
typically −EV — and this tab shows exactly that, sized to the model's IV.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import options


@dataclass
class StraddlePeriod:
    entry_date: str
    expiry_date: str
    spot_entry: float        # S0 = the ATM strike K
    spot_expiry: float       # S_T at expiration
    iv: float                # the implied vol used to price the straddle
    prem_per_unit: float     # BS straddle premium per 1 unit of underlying (= call + put)
    units: float             # units bought = premium budget / prem_per_unit
    call_cost: float         # $ of the budget that went to the CALL leg (units × call price)
    put_cost: float          # $ of the budget that went to the PUT  leg (units × put price)
    premium_paid: float      # $ spent on the straddle this period (= risk_pct × bank, + entry fee)
    payoff: float            # $ received at expiry = units × |S_T − K|
    pnl: float               # payoff − premium_paid − fees
    bank_after: float
    move_pct: float          # realized |S_T − K| / S0
    breakeven_pct: float     # prem_per_unit / S0  — the move needed just to break even
    win: bool


@dataclass
class PureStraddleResult:
    starting_bank: float = 0.0
    final_bank: float = 0.0
    years: float = 0.0
    n_periods: int = 0
    n_wins: int = 0
    n_losses: int = 0
    win_rate: float = 0.0
    max_win_streak: int = 0          # longest run of consecutive profitable periods
    max_loss_streak: int = 0         # longest run of consecutive losing periods
    avg_win: float = 0.0             # mean P&L of profitable periods
    avg_loss: float = 0.0            # mean P&L of losing periods (negative)
    # run-length → how many such runs occurred, e.g. {1: 12, 2: 5, 3: 2} ("2 wins in a row happened 5×")
    win_streaks: dict = field(default_factory=dict)
    loss_streaks: dict = field(default_factory=dict)
    total_premium: float = 0.0       # Σ premium paid (the "rent")
    total_payoff: float = 0.0        # Σ intrinsic received
    net_pnl: float = 0.0
    ann_return_pct: float = 0.0      # geometric CAGR of the bank
    avg_pnl: float = 0.0
    profit_factor: float = 0.0       # Σ wins / |Σ losses|
    premium_recovered_pct: float = 0.0   # 100 × total_payoff / total_premium
    avg_breakeven_pct: float = 0.0   # mean move % needed to break even (≈ the IV cost)
    avg_move_pct: float = 0.0        # mean realized move % (what the market actually delivered)
    table: list[StraddlePeriod] = field(default_factory=list)
    equity: list[dict] = field(default_factory=list)   # [{date, bank}] for the curve


def _streak_counts(outcomes: list[bool]) -> tuple[dict, dict]:
    """Given the per-period win/loss sequence, return (win_streaks, loss_streaks) where each is
    {run_length: how_many_such_runs}. E.g. outcomes W W W L L W → win {1:1, 3:1}, loss {2:1}.
    Answers "how often did we get 3/4/5 wins (or losses) in a row?"."""
    win: dict[int, int] = {}
    loss: dict[int, int] = {}
    run = 0
    prev = None
    for o in outcomes:
        if o == prev:
            run += 1
        else:
            if prev is not None:
                (win if prev else loss)[run] = (win if prev else loss).get(run, 0) + 1
            prev, run = o, 1
    if prev is not None:                                     # flush the final run
        (win if prev else loss)[run] = (win if prev else loss).get(run, 0) + 1
    return dict(sorted(win.items())), dict(sorted(loss.items()))


def run_pure_straddle(daily: pd.DataFrame, vol_model, *, risk_pct: float = 0.01,
                      dte_days: int = 30, starting_bank: float = 10_000.0, r: float = 0.045,
                      commission_pct: float = 0.0, slippage_pct: float = 0.0,
                      compounding: bool = True) -> PureStraddleResult:
    """Roll ATM straddles to expiry, sizing each to `risk_pct` of the (compounding) bank.

    fee_rate = (commission_pct + slippage_pct)/100 is charged on the premium at entry and on the
    payoff at exit (both legs of an options round-trip). compounding=False keeps the bet sized to
    the STARTING bank (additive P&L), isolating per-period economics from the equity ramp.
    """
    res = PureStraddleResult(starting_bank=starting_bank, final_bank=starting_bank)
    if daily is None or daily.empty:
        return res
    close = daily["Close"].astype(float)
    dates = daily.index
    n = len(dates)
    T_years = max(dte_days / 365.0, 1e-6)
    fee_rate = max(commission_pct + slippage_pct, 0.0) / 100.0
    bank = starting_bank
    res.equity.append({"date": str(dates[0].date()), "bank": round(bank, 2)})

    p = 0
    while p < n - 1:
        entry_date = dates[p]
        S0 = float(close.iloc[p])
        if not np.isfinite(S0) or S0 <= 0:
            p += 1
            continue
        K = S0                                              # ATM
        sigma = float(vol_model.sigma(entry_date, T_years, K, S0))
        call_per_unit = float(options.call_price(S0, K, T_years, r, sigma))
        put_per_unit = float(options.put_price(S0, K, T_years, r, sigma))
        prem_per_unit = call_per_unit + put_per_unit        # full straddle = call + put
        if prem_per_unit <= 0:
            p += 1
            continue
        # expiry = first bar on/after entry_date + dte_days calendar days
        expiry_ts = entry_date + pd.Timedelta(days=dte_days)
        q = int(dates.searchsorted(expiry_ts, side="left"))
        if q >= n:                                          # not enough data left to reach expiry
            break
        if q <= p:
            q = p + 1
        spot_expiry = float(close.iloc[q])

        # size the bet to risk_pct of the current bank (the premium budget)
        sized_bank = bank if compounding else starting_bank
        budget = max(risk_pct * sized_bank, 0.0)
        units = budget / prem_per_unit
        entry_fee = budget * fee_rate
        premium_paid = budget + entry_fee

        payoff_gross = units * abs(spot_expiry - K)
        exit_fee = payoff_gross * fee_rate
        payoff = payoff_gross - exit_fee
        pnl = payoff - premium_paid
        bank += pnl

        win = pnl > 0
        res.table.append(StraddlePeriod(
            entry_date=str(entry_date.date()), expiry_date=str(dates[q].date()),
            spot_entry=round(S0, 4), spot_expiry=round(spot_expiry, 4), iv=round(sigma, 4),
            prem_per_unit=round(prem_per_unit, 4), units=round(units, 4),
            call_cost=round(units * call_per_unit, 2), put_cost=round(units * put_per_unit, 2),
            premium_paid=round(premium_paid, 2), payoff=round(payoff, 2), pnl=round(pnl, 2),
            bank_after=round(bank, 2), move_pct=round(100.0 * abs(spot_expiry - K) / S0, 3),
            breakeven_pct=round(100.0 * prem_per_unit / S0, 3), win=win))
        res.equity.append({"date": str(dates[q].date()), "bank": round(bank, 2)})
        res.total_premium += premium_paid
        res.total_payoff += payoff
        if win:
            res.n_wins += 1
        p = q                                               # roll: next straddle enters at expiry

    res.final_bank = bank
    _finalize(res, compounding)
    return res


def _finalize(res: PureStraddleResult, compounding: bool) -> None:
    """Compute the summary stats / streak distributions / CAGR from a populated table+bank.
    Shared by the straddle and single-leg engines (identical bookkeeping)."""
    res.n_periods = len(res.table)
    if not res.n_periods:
        return
    res.win_rate = res.n_wins / res.n_periods
    res.n_losses = res.n_periods - res.n_wins
    res.net_pnl = res.final_bank - res.starting_bank
    res.avg_pnl = res.net_pnl / res.n_periods
    win_pnls = [t.pnl for t in res.table if t.win]
    loss_pnls = [t.pnl for t in res.table if not t.win]
    res.avg_win = float(np.mean(win_pnls)) if win_pnls else 0.0
    res.avg_loss = float(np.mean(loss_pnls)) if loss_pnls else 0.0
    wins = sum(t.pnl for t in res.table if t.pnl > 0)
    losses = sum(-t.pnl for t in res.table if t.pnl < 0)
    res.profit_factor = (wins / losses) if losses > 1e-9 else float("inf")
    # streak distributions: count runs of consecutive wins / losses by their length
    res.win_streaks, res.loss_streaks = _streak_counts([t.win for t in res.table])
    res.max_win_streak = max(res.win_streaks, default=0)
    res.max_loss_streak = max(res.loss_streaks, default=0)
    res.premium_recovered_pct = (100.0 * res.total_payoff / res.total_premium
                                 if res.total_premium > 1e-9 else 0.0)
    res.avg_breakeven_pct = float(np.mean([t.breakeven_pct for t in res.table]))
    res.avg_move_pct = float(np.mean([t.move_pct for t in res.table]))
    # geometric CAGR of the bank over the elapsed calendar span
    d0 = pd.Timestamp(res.table[0].entry_date)
    d1 = pd.Timestamp(res.table[-1].expiry_date)
    res.years = max((d1 - d0).days / 365.0, 1e-6)
    if compounding and res.starting_bank > 0 and res.final_bank > 0:
        res.ann_return_pct = 100.0 * ((res.final_bank / res.starting_bank) ** (1.0 / res.years) - 1.0)
    else:                                               # additive: simple annualized return on start bank
        res.ann_return_pct = 100.0 * (res.net_pnl / res.starting_bank) / res.years


def run_single_leg(daily: pd.DataFrame, vol_model, *, leg: str = "call", risk_pct: float = 0.01,
                   dte_days: int = 30, starting_bank: float = 10_000.0, r: float = 0.045,
                   commission_pct: float = 0.0, slippage_pct: float = 0.0,
                   compounding: bool = True) -> PureStraddleResult:
    """Roll a SINGLE ATM option leg ('call' or 'put') to expiry, sized to risk_pct of the bank.

    Same mechanics as run_pure_straddle but only ONE leg, so it isolates that leg's win/loss
    behaviour: a CALL wins only on up-moves past its premium, a PUT only on down-moves. Their
    win/loss sequences (and streaks) are near-mirror images of each other."""
    is_call = leg == "call"
    res = PureStraddleResult(starting_bank=starting_bank, final_bank=starting_bank)
    if daily is None or daily.empty:
        return res
    close = daily["Close"].astype(float)
    dates = daily.index
    n = len(dates)
    T_years = max(dte_days / 365.0, 1e-6)
    fee_rate = max(commission_pct + slippage_pct, 0.0) / 100.0
    bank = starting_bank
    res.equity.append({"date": str(dates[0].date()), "bank": round(bank, 2)})

    p = 0
    while p < n - 1:
        entry_date = dates[p]
        S0 = float(close.iloc[p])
        if not np.isfinite(S0) or S0 <= 0:
            p += 1
            continue
        K = S0                                              # ATM
        sigma = float(vol_model.sigma(entry_date, T_years, K, S0))
        prem_per_unit = float(options.call_price(S0, K, T_years, r, sigma) if is_call
                              else options.put_price(S0, K, T_years, r, sigma))
        if prem_per_unit <= 0:
            p += 1
            continue
        expiry_ts = entry_date + pd.Timedelta(days=dte_days)
        q = int(dates.searchsorted(expiry_ts, side="left"))
        if q >= n:
            break
        if q <= p:
            q = p + 1
        spot_expiry = float(close.iloc[q])

        sized_bank = bank if compounding else starting_bank
        budget = max(risk_pct * sized_bank, 0.0)
        units = budget / prem_per_unit
        entry_fee = budget * fee_rate
        premium_paid = budget + entry_fee

        intrinsic = max(spot_expiry - K, 0.0) if is_call else max(K - spot_expiry, 0.0)
        payoff_gross = units * intrinsic
        exit_fee = payoff_gross * fee_rate
        payoff = payoff_gross - exit_fee
        pnl = payoff - premium_paid
        bank += pnl

        win = pnl > 0
        res.table.append(StraddlePeriod(
            entry_date=str(entry_date.date()), expiry_date=str(dates[q].date()),
            spot_entry=round(S0, 4), spot_expiry=round(spot_expiry, 4), iv=round(sigma, 4),
            prem_per_unit=round(prem_per_unit, 4), units=round(units, 4),
            call_cost=round(premium_paid, 2) if is_call else 0.0,
            put_cost=0.0 if is_call else round(premium_paid, 2),
            premium_paid=round(premium_paid, 2), payoff=round(payoff, 2), pnl=round(pnl, 2),
            bank_after=round(bank, 2),
            move_pct=round(100.0 * (spot_expiry - K) / S0, 3),   # SIGNED move (call wants +, put −)
            breakeven_pct=round(100.0 * prem_per_unit / S0, 3), win=win))
        res.equity.append({"date": str(dates[q].date()), "bank": round(bank, 2)})
        res.total_premium += premium_paid
        res.total_payoff += payoff
        if win:
            res.n_wins += 1
        p = q

    res.final_bank = bank
    _finalize(res, compounding)
    return res


# ── coin-flip TRIAL resolution (fixed risk/reward, roll across expiries to ±R) ──────────────────
@dataclass
class Trial:
    start_date: str
    end_date: str
    n_rolls: int             # how many straddles/legs were rolled before the trial resolved
    R: float                 # the fixed risk = reward unit for this trial (risk_pct × bank at start)
    premium_total: float     # Σ premium spent across the rolls
    payoff_total: float      # Σ payoff received across the rolls
    cum_pnl: float           # trial P&L = payoff_total − premium_total (loss = −R; win ≥ +R, can overshoot)
    spot_start: float
    spot_end: float
    win: bool
    partial: bool = False    # True = resolved by the max-roll HORIZON (cum booked as-is), not by ±R


@dataclass
class TrialResult:
    leg: str = "straddle"
    starting_bank: float = 0.0
    final_bank: float = 0.0
    years: float = 0.0
    n_trials: int = 0
    n_wins: int = 0
    n_losses: int = 0
    n_partial: int = 0               # trials closed by the horizon (cum booked as-is) rather than ±R
    win_rate: float = 0.0
    max_win_streak: int = 0
    max_loss_streak: int = 0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    avg_rolls: float = 0.0
    max_rolls: int = 0
    net_pnl: float = 0.0
    ann_return_pct: float = 0.0
    win_streaks: dict = field(default_factory=dict)
    loss_streaks: dict = field(default_factory=dict)
    trials: list = field(default_factory=list)
    equity: list = field(default_factory=list)


def _leg_prem_and_intrinsic(leg, S0, K, T, r, sigma, S_T):
    """(premium per unit, intrinsic at expiry) for 'straddle' | 'call' | 'put'."""
    if leg == "call":
        return float(options.call_price(S0, K, T, r, sigma)), max(S_T - K, 0.0)
    if leg == "put":
        return float(options.put_price(S0, K, T, r, sigma)), max(K - S_T, 0.0)
    return float(options.straddle_price(S0, K, T, r, sigma)), abs(S_T - K)


def run_coinflip_trials(daily: pd.DataFrame, vol_model, *, leg: str = "straddle",
                        risk_pct: float = 0.01, dte_days: int = 30, starting_bank: float = 10_000.0,
                        r: float = 0.045, commission_pct: float = 0.0, slippage_pct: float = 0.0,
                        compounding: bool = True, max_rolls: int = 12) -> TrialResult:
    """Resolve the option backtest as a COIN FLIP with fixed risk/reward, translated to option reality.

    A *trial* keeps rolling the same construction (straddle, or a single call/put leg) to expiry until
    its cumulative P&L reaches **+R (WIN)** or **−R (LOSS)**, where R = risk_pct × bank — the fixed
    risk = reward unit. Each roll's premium = the REMAINING capacity to the −R floor (= R + cum), so a
    partial loss is carried forward (next straddle risks less) and the total loss is capped at exactly
    −R; a partial gain is carried forward too (we wait for the rest of +R). Losses book exactly −R;
    wins book the ACTUAL cum (≥ +R, can overshoot on a big move = long-option convexity).

    `max_rolls` is a HORIZON: a straddle rarely doubles or goes worthless in one expiry, so with the
    remaining-capacity sizing a losing trial would grind toward −R over dozens of ever-smaller rolls
    (years). If a trial hasn't hit ±R within max_rolls rolls, it is closed at its ACTUAL cum (a partial
    win if cum≥0 else a partial loss) and a fresh trial begins."""
    res = TrialResult(leg=leg, starting_bank=starting_bank, final_bank=starting_bank)
    if daily is None or daily.empty:
        return res
    close = daily["Close"].astype(float)
    dates = daily.index
    n = len(dates)
    T_years = max(dte_days / 365.0, 1e-6)
    fee_rate = max(commission_pct + slippage_pct, 0.0) / 100.0
    bank = starting_bank
    res.equity.append({"date": str(dates[0].date()), "bank": round(bank, 2)})

    p = 0
    while p < n - 1:
        sized_bank = bank if compounding else starting_bank
        R = risk_pct * sized_bank
        if R <= 0:
            break
        cum = 0.0
        n_rolls = 0
        prem_tot = 0.0
        pay_tot = 0.0
        start_p = p
        spot_start = float(close.iloc[p])
        outcome = None                                       # 'win' | 'loss' | None(incomplete)
        partial = False
        end_q = p
        while True:
            entry_date = dates[p]
            S0 = float(close.iloc[p])
            if not np.isfinite(S0) or S0 <= 0:
                p += 1
                if p >= n - 1:
                    break
                continue
            K = S0
            sigma = float(vol_model.sigma(entry_date, T_years, K, S0))
            ppu, _i = _leg_prem_and_intrinsic(leg, S0, K, T_years, r, sigma, S0)
            if ppu <= 0:
                p += 1
                if p >= n - 1:
                    break
                continue
            q = int(dates.searchsorted(entry_date + pd.Timedelta(days=dte_days), side="left"))
            if q >= n:
                break                                        # ran out of data → trial incomplete
            if q <= p:
                q = p + 1
            S_T = float(close.iloc[q])
            _ppu2, intrinsic = _leg_prem_and_intrinsic(leg, S0, K, T_years, r, sigma, S_T)

            budget = R + cum                                 # deploy the full remaining risk capacity
            if budget <= 0:
                outcome = "loss"
                end_q = p
                break
            units = budget / ppu
            payoff_gross = units * intrinsic
            pnl = payoff_gross - budget - budget * fee_rate - payoff_gross * fee_rate
            cum += pnl
            prem_tot += budget + budget * fee_rate
            pay_tot += payoff_gross - payoff_gross * fee_rate
            n_rolls += 1
            end_q = q
            p = q                                            # roll: next leg entered at this expiry
            if cum >= R:
                outcome = "win"
                break
            if cum <= -R + 1e-9:
                outcome = "loss"
                break
            if n_rolls >= max_rolls:                         # horizon hit → book the partial cum
                outcome = "win" if cum >= 0 else "loss"
                partial = True
                break
            if p >= n - 1:
                break                                        # no room for another roll → incomplete

        if outcome is None:                                  # ran out of data mid-trial
            if n_rolls == 0:
                break                                        # couldn't complete even one roll → nothing to book
            outcome = "win" if cum >= 0 else "loss"          # book the data-truncated tail as a partial
            partial = True
            truncated = True
        else:
            truncated = False
        bank += cum
        win = outcome == "win"
        res.trials.append(Trial(
            start_date=str(dates[start_p].date()), end_date=str(dates[end_q].date()),
            n_rolls=n_rolls, R=round(R, 2), premium_total=round(prem_tot, 2),
            payoff_total=round(pay_tot, 2), cum_pnl=round(cum, 2),
            spot_start=round(spot_start, 4), spot_end=round(float(close.iloc[end_q]), 4),
            win=win, partial=partial))
        res.equity.append({"date": str(dates[end_q].date()), "bank": round(bank, 2)})
        if win:
            res.n_wins += 1
        if partial:
            res.n_partial += 1
        if truncated:                                        # nothing left after this tail → stop
            break
        p = end_q                                            # next trial starts at this expiry

    res.final_bank = bank
    res.n_trials = len(res.trials)
    if res.n_trials:
        res.n_losses = res.n_trials - res.n_wins
        res.win_rate = res.n_wins / res.n_trials
        res.net_pnl = res.final_bank - res.starting_bank
        wins = [t.cum_pnl for t in res.trials if t.win]
        losses = [t.cum_pnl for t in res.trials if not t.win]
        res.avg_win = float(np.mean(wins)) if wins else 0.0
        res.avg_loss = float(np.mean(losses)) if losses else 0.0
        gw = sum(wins)
        gl = -sum(losses)
        res.profit_factor = (gw / gl) if gl > 1e-9 else float("inf")
        res.win_streaks, res.loss_streaks = _streak_counts([t.win for t in res.trials])
        res.max_win_streak = max(res.win_streaks, default=0)
        res.max_loss_streak = max(res.loss_streaks, default=0)
        rolls = [t.n_rolls for t in res.trials]
        res.avg_rolls = float(np.mean(rolls))
        res.max_rolls = max(rolls)
        d0 = pd.Timestamp(res.trials[0].start_date)
        d1 = pd.Timestamp(res.trials[-1].end_date)
        res.years = max((d1 - d0).days / 365.0, 1e-6)
        if compounding and res.starting_bank > 0 and res.final_bank > 0:
            res.ann_return_pct = 100.0 * ((res.final_bank / res.starting_bank) ** (1.0 / res.years) - 1.0)
        else:
            res.ann_return_pct = 100.0 * (res.net_pnl / res.starting_bank) / res.years
    return res
