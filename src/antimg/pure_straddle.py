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
    prem_per_unit: float     # BS straddle premium per 1 unit of underlying
    units: float             # units bought = premium budget / prem_per_unit
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
    win_rate: float = 0.0
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
        prem_per_unit = float(options.straddle_price(S0, K, T_years, r, sigma))
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
    res.n_periods = len(res.table)
    if res.n_periods:
        res.win_rate = res.n_wins / res.n_periods
        res.net_pnl = res.final_bank - res.starting_bank
        res.avg_pnl = res.net_pnl / res.n_periods
        wins = sum(t.pnl for t in res.table if t.pnl > 0)
        losses = sum(-t.pnl for t in res.table if t.pnl < 0)
        res.profit_factor = (wins / losses) if losses > 1e-9 else float("inf")
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
    return res
