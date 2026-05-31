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
    exit_price: float          # barrier level hit (or expiry close for the no-stop call)
    atr_entry: float
    outcome: str               # 'win' | 'loss'
    days_held: int
    exit_reason: str = ""      # 'target' | 'stop' | 'straddle' | 'expiry'


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
    # cost decomposition (per-trial cumulative, aligned with equity_dates)
    cum_commission: list[float] = field(default_factory=list)
    cum_slippage: list[float] = field(default_factory=list)
    cum_cost: list[float] = field(default_factory=list)
    total_commission: float = 0.0
    total_slippage: float = 0.0
    total_cost: float = 0.0
    n_cycles: int = 0
    cost_per_cycle: float = 0.0
    # cost expressed as a win-probability drag (breakeven shift) — see cost_as_probability
    cost_as_prob: float = 0.0          # total cost
    commission_as_prob: float = 0.0
    slippage_as_prob: float = 0.0
    breakeven_p_with_cost: float = 0.5
    # per-trial detail table (JSON-able dicts) for the UI
    table: list[dict] = field(default_factory=list)
    # options-only
    delta_dates: list[pd.Timestamp] = field(default_factory=list)
    delta_path: list[float] = field(default_factory=list)
    # scale-in (ladder add) markers for the price chart — one per +1·ATR rung added
    add_dates: list[pd.Timestamp] = field(default_factory=list)
    add_levels: list[float] = field(default_factory=list)


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
        reason = ""
        for d, row in future.iterrows():
            hi, lo = row["High"], row["Low"]
            hit_up = hi >= up
            hit_dn = lo <= dn
            if hit_up and hit_dn:
                outcome, exit_price, reason = "loss", dn, "straddle"   # B-2 loss-first
            elif hit_dn:
                outcome, exit_price, reason = "loss", dn, "stop"
            elif hit_up:
                outcome, exit_price, reason = "win", up, "target"
            if outcome:
                exit_date = d
                break
        if outcome is None:
            break  # ran out of data with an open position

        days_held = max((exit_date - future.index[0]).days, 0)
        trials.append(Trial(future.index[0], exit_date, float(entry), float(exit_price),
                            float(atr_e), outcome, days_held, reason))
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


def resolve_trials_long_call(daily: pd.DataFrame, weekly: pd.DataFrame, weekly_atr: pd.Series,
                             dte_days: int, mult: float = 1.0) -> list[Trial]:
    """Win/loss sequence for a LONG CALL — the whole point: there is **NO −1·ATR stop**.

    A linear position gets whipsawed out on every −1·ATR pullback; a long call does not —
    its downside is the premium, so we HOLD through pullbacks. A trial resolves as:
      - WIN  : price reaches entry + mult·ATR (the +1·ATR target) before the option expires;
      - LOSS : the option reaches expiry (entry_day + dte_days) without hitting the target.
    Because pullbacks no longer end the trade, an up-trend that whipsaws a linear stop is
    captured far more often here — exactly the call's edge. `exit_price` is the target level
    on a win, or the expiry close on a loss; `exit_reason` is 'target' or 'expiry'.
    """
    trials: list[Trial] = []
    daily = daily.sort_index()
    wk_index = weekly.index
    pos = 0
    while pos < len(wk_index):
        wk_date = wk_index[pos]
        atr_e = weekly_atr.get(wk_date, np.nan)
        entry = weekly["Open"].get(wk_date, np.nan)
        if not np.isfinite(atr_e) or atr_e <= 0 or not np.isfinite(entry):
            pos += 1
            continue
        up = entry + mult * atr_e
        week_start = wk_date - pd.Timedelta(days=6)
        future = daily.loc[daily.index >= week_start]
        if future.empty:
            break
        entry_day = future.index[0]
        expiry = entry_day + pd.Timedelta(days=dte_days)

        outcome = exit_date = None
        exit_price = np.nan
        reason = ""
        for d, row in future.iterrows():
            if row["High"] >= up:                      # target reached — no stop in between
                outcome, exit_price, reason = "win", up, "target"
                exit_date = d
                break
            if d >= expiry:                            # held to expiry without the target
                outcome, exit_price, reason = "loss", float(row["Close"]), "expiry"
                exit_date = d
                break
        if outcome is None:
            break  # ran out of data before target or expiry

        days_held = max((exit_date - entry_day).days, 0)
        trials.append(Trial(entry_day, exit_date, float(entry), float(exit_price),
                            float(atr_e), outcome, days_held, reason))
        later = wk_index[(wk_index - pd.Timedelta(days=6)) > exit_date]
        if len(later) == 0:
            break
        new_pos = wk_index.get_loc(later[0])
        pos = new_pos if new_pos > pos else pos + 1
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


def _trial_costs(bet: float, atr_entry: float, entry_price: float,
                 commission_pct: float, slippage_pct: float) -> tuple[float, float]:
    """Round-trip transaction costs for one trial (entry + exit = 2 fills).

    Both commission and slippage are PERCENT of position notional PER FILL, charged twice
    (entry + exit). Notional comes from the Δ=1 sizing: shares = bet/ATR (so a 1·ATR move
    == bet $), notional = shares * price.
    Returns (commission_cost, slippage_cost).
    """
    notional = (bet / atr_entry) * entry_price if atr_entry else 0.0
    commission_cost = 2.0 * (commission_pct / 100.0) * notional
    slippage_cost = 2.0 * (slippage_pct / 100.0) * notional
    return commission_cost, slippage_cost


def cost_as_probability(cost_dollars: float, n_cycles: int, base_bet: float,
                        target_streak: int) -> tuple[float, float]:
    """Translate a total cost into a win-probability drag (Δp) and the breakeven p.

    Per-cycle EV (no cost) = b·((2p)^N − 1); breakeven is p=0.5. With an average cost κ per
    cycle the breakeven becomes (2p*)^N = 1 + κ/b ⇒ p* = 0.5·(1 + κ/b)^(1/N).
    Δp = p* − 0.5 is "how much win-probability the cost eats": if your edge (p−0.5) < Δp,
    costs flip the strategy net-negative. (Approximate attribution when split by component.)
    """
    if n_cycles <= 0 or base_bet <= 0 or target_streak <= 0:
        return 0.0, 0.5
    kappa_over_b = (cost_dollars / n_cycles) / base_bet
    p_be = 0.5 * (1.0 + kappa_over_b) ** (1.0 / target_streak)
    return p_be - 0.5, p_be


def _drawdown(equity: list[float]) -> float:
    peak = -np.inf
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return mdd


def run_linear(trials: list[Trial], base_bet: float, target_streak: int,
               commission_pct: float = 0.0, slippage_pct: float = 0.0,
               starting_bank: float = 0.0, cap_mult: float | None = None) -> BacktestResult:
    """Δ=1 linear P&L: a win is +bet, a loss is -bet (1 ATR move == bet).

    commission = $/fill (×2 round-trip); slippage_pct = % of notional/fill (×2).
    """
    res = BacktestResult(trials=trials)
    bank = starting_bank
    streak, bet = 0, base_bet
    wins = cum_comm = cum_slip = n_cycles = 0
    for i, t in enumerate(trials):
        used_bet = bet
        comm_c, slip_c = _trial_costs(bet, t.atr_entry, t.entry_price, commission_pct, slippage_pct)
        cost = comm_c + slip_c
        pnl = (bet if t.outcome == "win" else -bet) - cost
        bank += pnl
        streak, bet = _apply_pyramid(t.outcome, streak, bet, base_bet, target_streak, cap_mult)
        if streak == 0:
            n_cycles += 1
        wins += t.outcome == "win"
        cum_comm += comm_c
        cum_slip += slip_c
        res.records.append(TrialRecord(t, bet, pnl, bank, streak))
        res.equity_dates.append(t.exit_date)
        res.equity.append(bank)
        res.cum_commission.append(cum_comm)
        res.cum_slippage.append(cum_slip)
        res.cum_cost.append(cum_comm + cum_slip)
        res.table.append({
            "i": i + 1, "entry": t.entry_date.date().isoformat(),
            "exit": t.exit_date.date().isoformat(), "days": t.days_held,
            "entry_px": round(t.entry_price, 2), "exit_px": round(t.exit_price, 2),
            "atr": round(t.atr_entry, 2),
            "up": round(t.entry_price + t.atr_entry, 2),
            "dn": round(t.entry_price - t.atr_entry, 2),
            "reason": t.exit_reason, "outcome": t.outcome,
            "bet": round(used_bet, 2), "cost": round(cost, 2),
            "pnl": round(pnl, 2), "bank": round(bank, 2),
        })
    _finalize(res, base_bet, target_streak, wins, bank, n_cycles)
    return res


def run_options(trials: list[Trial], daily: pd.DataFrame, realized_vol: pd.Series,
                base_bet: float, target_streak: int, *, r: float = 0.045,
                dte_days: int = 365, target_delta: float = 0.95, q: float = 0.0,
                default_sigma: float = 0.20, commission_pct: float = 0.0,
                slippage_pct: float = 0.0, starting_bank: float = 0.0,
                cap_mult: float | None = None) -> BacktestResult:
    """Same win/loss sequence, but P&L from a modeled deep-ITM call (BS, IV=realized vol).

    Also records the auto-computed delta path across all holding periods for plotting.
    Max loss per trial is the premium (BS price floor) — the doctrine's left-tail lever.
    """
    res = BacktestResult(trials=trials)
    bank = starting_bank
    streak, bet = 0, base_bet
    wins = cum_comm = cum_slip = n_cycles = 0
    close = daily["Close"]
    for i, t in enumerate(trials):
        used_bet = bet
        S0 = t.entry_price
        sig0 = _sigma_at(realized_vol, t.entry_date, default_sigma)
        T0 = dte_days / 365.0
        # Everything about the option is fixed AT ENTRY (as you really buy it): the IV (sig0,
        # = realized vol observed at entry), the strike, and the delta. Only the underlying
        # price S and the time-to-expiry T move forward — sig0 is NOT re-estimated later.
        K = opt.strike_for_delta(S0, T0, r, sig0, target_delta, q)
        units = bet / t.atr_entry            # 1 ATR underlying move ~ bet of exposure
        price0 = float(opt.call_price(S0, K, T0, r, sig0, q))
        delta0 = float(opt.call_delta(S0, K, T0, r, sig0, q))   # delta at the moment of entry

        # plot the ENTRY delta held flat for the trade's duration (the delta you bought)
        window = close.loc[(close.index >= t.entry_date) & (close.index <= t.exit_date)]
        for d in window.index:
            res.delta_dates.append(d)
            res.delta_path.append(delta0)

        elapsed = (t.exit_date - t.entry_date).days
        T1 = max((dte_days - elapsed) / 365.0, 1e-6)
        price1 = float(opt.call_price(t.exit_price, K, T1, r, sig0, q))   # entry IV held

        comm_c, slip_c = _trial_costs(bet, t.atr_entry, t.entry_price, commission_pct, slippage_pct)
        cost = comm_c + slip_c
        opt_pnl = (price1 - price0) * units
        pnl = opt_pnl - cost
        bank += pnl
        streak, bet = _apply_pyramid(t.outcome, streak, bet, base_bet, target_streak, cap_mult)
        if streak == 0:
            n_cycles += 1
        wins += t.outcome == "win"
        cum_comm += comm_c
        cum_slip += slip_c
        res.records.append(TrialRecord(t, bet, pnl, bank, streak))
        res.equity_dates.append(t.exit_date)
        res.equity.append(bank)
        res.cum_commission.append(cum_comm)
        res.cum_slippage.append(cum_slip)
        res.cum_cost.append(cum_comm + cum_slip)
        res.table.append({
            "i": i + 1, "entry": t.entry_date.date().isoformat(),
            "exit": t.exit_date.date().isoformat(), "days": t.days_held,
            "entry_px": round(S0, 2), "exit_px": round(t.exit_price, 2),
            "atr": round(t.atr_entry, 2), "target_up": round(S0 + t.atr_entry, 2),
            "reason": t.exit_reason, "outcome": t.outcome,
            "strike": round(K, 2), "delta_entry": round(delta0, 3),
            "prem_in": round(price0, 2), "prem_out": round(price1, 2),
            "units": round(units, 3), "opt_pnl": round(opt_pnl, 2),
            "cost": round(cost, 2), "pnl": round(pnl, 2), "bank": round(bank, 2),
        })
    _finalize(res, base_bet, target_streak, wins, bank, n_cycles)
    return res


def _sigma_at(rv: pd.Series, date: pd.Timestamp, default: float) -> float:
    try:
        v = rv.asof(date)
    except Exception:
        v = np.nan
    return float(v) if v is not None and np.isfinite(v) and v > 0 else default


def _calls_campaign_pnl(daily, entry_day, exit_date, exit_px, batches, per_pt, *,
                        r, dte_days, target_delta, qdiv, realized_vol, default_sigma,
                        roll_buffer, commission_pct, slippage_pct, vol_model=None,
                        trace=None, camp=None):
    """Mark-to-market P&L of a long-call campaign WITH auto-rolling.

    Walks daily entry->exit. Buys delta-normalised calls at each ladder add (priced at the
    add level). When the held option comes within `roll_buffer` days of expiry it ROLLS:
    crystallise the current calls, re-strike to `target_delta` at the current price for a
    fresh DTE, keep the same lot exposure. Every fill (add / roll close+open / final close)
    pays commission+slippage on its notional. Returns (gross_pnl, commission, slippage, n_rolls).

    If `vol_model` is given the IV is read off the real ATM term-structure at the option's
    nominal tenor T0 and skew-adjusted at the chosen strike; otherwise it falls back to the
    flat per-date `realized_vol` series (the original behaviour).
    """
    seg = daily.loc[(daily.index >= entry_day) & (daily.index <= exit_date)]
    batches = sorted(batches, key=lambda b: (b[1], b[0]))
    realized = comm = slip = contracts = book = 0.0
    n_rolls = 0
    K = expiry = sig = None
    T0 = dte_days / 365.0
    bi = 0

    def trem(d):
        return max((expiry - d).days / 365.0, 1e-6)

    def pick(date, S):
        """(strike, skew-adjusted IV) for a fresh option struck at spot S on `date`.

        Strike is solved with the ATM term-structure IV; the returned IV adds the skew at
        that strike, so a deep-ITM (K<S) strike picks up the smile premium (β<0 ⇒ higher IV).
        """
        if vol_model is not None:
            a = vol_model.atm(date, T0)
            if a is None:
                a = _sigma_at(realized_vol, date, default_sigma)
            k = opt.strike_for_delta(S, T0, r, a, target_delta, qdiv)
            return k, vol_model.sigma(date, T0, k, S, default_sigma)
        s = _sigma_at(realized_vol, date, default_sigma)
        return opt.strike_for_delta(S, T0, r, s, target_delta, qdiv), s

    def fill(qty, price):
        nonlocal comm, slip
        notion = abs(qty) * price
        comm += commission_pct / 100.0 * notion
        slip += slippage_pct / 100.0 * notion

    for d, row in seg.iterrows():
        sclose = float(row["Close"])
        if K is not None and (expiry - d).days <= roll_buffer:        # roll near expiry
            p = float(opt.call_price(sclose, K, trem(d), r, sig, qdiv))
            realized += contracts * p - book
            fill(contracts, p)                                        # close leg
            K, sig = pick(d, sclose)
            expiry = d + pd.Timedelta(days=dte_days)
            lots_held = sum(l for _, _, l in batches[:bi])
            contracts = lots_held * (per_pt / target_delta)
            p2 = float(opt.call_price(sclose, K, T0, r, sig, qdiv))
            book = contracts * p2
            fill(contracts, p2)                                       # open leg
            n_rolls += 1
        while bi < len(batches) and batches[bi][1] <= d:              # ladder adds on this bar
            L, dt, lots = batches[bi]; step_i = bi; bi += 1
            if K is None:                                             # entry add sets up the option
                expiry = entry_day + pd.Timedelta(days=dte_days)
                K, sig = pick(entry_day, L)
            dlt = max(float(opt.call_delta(L, K, trem(dt), r, sig, qdiv)), 1e-6)
            addc = lots * (per_pt / dlt)
            p = float(opt.call_price(L, K, trem(dt), r, sig, qdiv))
            contracts += addc; book += addc * p
            fill(addc, p)
            if trace is not None:
                mtm = contracts * p                                   # value of whole stack now
                trace.append({"t": "opt_add", "camp": camp, "step": step_i,
                              "date": dt.date().isoformat(), "level": round(L, 4),
                              "strike": round(K, 4), "iv": round(sig, 4),
                              "delta": round(dlt, 4), "premium_per": round(p, 4),
                              "contracts_added": round(addc, 2),
                              "premium_paid": round(addc * p, 2),
                              "contracts": round(contracts, 2),
                              "premium_book": round(book, 2),
                              "stack_value": round(mtm, 2),
                              "unreal": round(mtm - book, 2)})
        if trace is not None and K is not None:                       # daily mark-to-market path
            pm = float(opt.call_price(sclose, K, trem(d), r, sig, qdiv))
            trace.append({"t": "opt_mark", "camp": camp, "date": d.date().isoformat(),
                          "spot": round(sclose, 4), "premium_per": round(pm, 4),
                          "delta": round(float(opt.call_delta(sclose, K, trem(d), r, sig, qdiv)), 4),
                          "contracts": round(contracts, 2),
                          "stack_value": round(contracts * pm, 2),
                          "unreal": round(contracts * pm - book, 2)})
    p = float(opt.call_price(exit_px, K, trem(exit_date), r, sig, qdiv))  # final close
    realized += contracts * p - book
    fill(contracts, p)
    if trace is not None:
        trace.append({"t": "opt_exit", "camp": camp,
                      "date": exit_date.date().isoformat(), "price": round(float(exit_px), 4),
                      "premium_per": round(p, 4), "contracts": round(contracts, 2),
                      "stack_value": round(contracts * p, 2),
                      "premium_book": round(book, 2), "gross": round(realized, 2),
                      "comm": round(comm, 2), "slip": round(slip, 2), "rolls": n_rolls})
    return realized, comm, slip, n_rolls


def run_campaign(daily: pd.DataFrame, weekly: pd.DataFrame, weekly_atr: pd.Series, *,
                 base_bet: float, target_streak: int, mult: float = 1.0,
                 instrument: str = "shares", mode: str = "pyramid",
                 realized_vol: pd.Series | None = None, r: float = 0.045,
                 dte_days: int = 365, target_delta: float = 0.5, qdiv: float = 0.0,
                 default_sigma: float = 0.20, commission_pct: float = 0.0,
                 slippage_pct: float = 0.0, starting_bank: float = 0.0,
                 cap_mult: float | None = None, roll_buffer_days: int = 5,
                 vol_model=None, trace: list | None = None) -> BacktestResult:
    """Scale-into-ONE-position campaign on the ATR grid (the validated model).

    Step h = mult*ATR (ATR fixed at entry). From entry R0, each +1 step UP adds lots on a
    doubling ladder (1, 2, 4, 8 …) — `pyramid` mode; `scalp` mode books +b each step and
    re-enters (no compounding). The stop is AVERAGE-BASED, not a classic peak-minus-ATR trail:
    S = avg − h/Q, chosen so the realised loss measured FROM THE AVERAGE is exactly the initial
    risk b at every step — Q·(avg−S)·per_pt = h·per_pt = b, independent of Q. (The stop does
    ratchet up as avg rises and h/Q shrinks, but its DEFINITION is constant-risk-from-average,
    not "follow the peak".) So every stop-out ≈ −b, every target-N run is the big convex win —
    the coin-flip distribution.

    instrument='shares' → linear P&L; 'calls' → BS-repriced long call, delta-normalised so
    1 lot moves ~b per step regardless of delta (units = (b/h)/Δ_entry contracts), IV fixed
    at entry (realized vol), strike chosen for `target_delta`. Calls soften the −b losses
    (convexity) and fatten the win tail (gamma).
    """
    res = BacktestResult()
    daily = daily.sort_index()
    wk_index = weekly.index
    bank = starting_bank
    is_calls = instrument == "calls"
    cumc = cums = 0.0
    n_cycles = wins = 0
    pos = 0
    while pos < len(wk_index):
        wk = wk_index[pos]
        atr = weekly_atr.get(wk, np.nan)
        R0 = weekly["Open"].get(wk, np.nan)
        if not np.isfinite(atr) or atr <= 0 or not np.isfinite(R0):
            pos += 1
            continue
        h = mult * atr                       # ATR step in price
        per_pt = base_bet / h                # $ per underlying point, per lot (delta-normalised)
        week_start = wk - pd.Timedelta(days=6)
        future = daily.loc[daily.index >= week_start]
        if future.empty:
            break
        entry_day = future.index[0]
        R0 = float(R0)
        entry_price0 = R0                    # campaign entry (scalp reassigns R0 later)

        if is_calls:
            T0 = dte_days / 365.0
            if vol_model is not None:                     # ATM term-structure → strike → skew IV
                atm0 = vol_model.atm(entry_day, T0) or _sigma_at(realized_vol, entry_day, default_sigma)
                K = opt.strike_for_delta(R0, T0, r, atm0, target_delta, qdiv)
                sig0 = vol_model.sigma(entry_day, T0, K, R0, default_sigma)
            else:
                sig0 = _sigma_at(realized_vol, entry_day, default_sigma)
                K = opt.strike_for_delta(R0, T0, r, sig0, target_delta, qdiv)
            d0 = float(opt.call_delta(R0, K, T0, r, sig0, qdiv))
            contracts_per_lot = per_pt / max(d0, 1e-6)   # delta-normalised: 1 lot ~ b per step
            expiry_day = entry_day + pd.Timedelta(days=dte_days)

        # ladder state
        batches: list[tuple[float, pd.Timestamp, float]] = [(R0, entry_day, 1.0)]  # (level, date, lots)
        Q = 1.0
        step = 0
        lot_cap = cap_mult if cap_mult else float("inf")

        def avg_price():
            return sum(L * lots for L, _, lots in batches) / sum(lots for _, _, lots in batches)

        stop = R0 - h / Q
        exit_px = exit_date = reason = None
        peak_step = 0

        if trace is not None:                       # step-by-step trace for the Explain tab
            ev = {"t": "entry", "camp": n_cycles + 1,
                  "date": entry_day.date().isoformat(), "price": round(R0, 4),
                  "atr": round(atr, 4), "h": round(h, 4), "per_pt": round(per_pt, 6),
                  "lots": 1.0, "Q": 1.0, "avg": round(R0, 4), "stop": round(stop, 4),
                  "risk": round(1.0 * (R0 - stop) * per_pt, 2)}
            if not is_calls:                         # shares-only molecular money (calls use opt_* events)
                ev.update({"units": round(per_pt, 4), "notional": round(per_pt * R0, 2),
                           "unreal": 0.0})
            trace.append(ev)

        for d, row in future.iterrows():
            hi, lo = float(row["High"]), float(row["Low"])
            if lo <= stop:                                   # stop-out (loss-first)
                exit_px, exit_date, reason = stop, d, "stop"
                break
            # NOTE: a finite option life is handled by ROLLING inside _calls_campaign_pnl
            # (re-strike within roll_buffer of expiry), NOT by ending the campaign. The
            # campaign rides the ATR grid (stop/target/open) exactly like shares — otherwise
            # a short DTE would close every campaign after one week at Q=1 and the pyramid
            # could never build (and the roll logic would be dead).
            if mode == "scalp":
                # book +b each +1 step, re-enter; loss handled by the stop above (= -b)
                while hi >= R0 + (step + 1) * h:
                    step += 1
                    R0 = R0 + h                              # advance reference, single lot
                    batches = [(R0, d, 1.0)]; Q = 1.0
                    stop = R0 - h / Q
                    res.table.append({"i": len(res.table) + 1, "entry": entry_day.date().isoformat(),
                                      "step": step, "level": round(R0, 2), "lots": 1.0,
                                      "avg": round(R0, 2), "stop": round(stop, 2),
                                      "reason": "step+", "pnl": round(base_bet, 2)})
                    bank += base_bet
                    res.equity_dates.append(d); res.equity.append(bank)
                    res.cum_commission.append(cumc); res.cum_slippage.append(cums)
                    res.cum_cost.append(cumc + cums)
                    wins += 1
                    if trace is not None:
                        trace.append({"t": "step", "camp": n_cycles + 1, "step": step,
                                      "date": d.date().isoformat(), "level": round(R0, 4),
                                      "booked": round(base_bet, 2)})
                    if step >= target_streak:
                        exit_px, exit_date, reason = R0, d, "target"
                        break
                if reason:
                    break
                continue
            # pyramid mode: add doubling lots on each up-step
            while hi >= R0 + (step + 1) * h:
                step += 1
                add = min(2.0 ** step, lot_cap)
                level = R0 + step * h
                batches.append((level, d, add))
                res.add_dates.append(d); res.add_levels.append(level)   # scale-in marker
                Q += add
                stop = avg_price() - h / Q
                peak_step = step
                if trace is not None:
                    a_ = avg_price()
                    ev = {"t": "add", "camp": n_cycles + 1, "step": step,
                          "date": d.date().isoformat(),
                          "trigger": round(R0 + step * h, 4), "level": round(level, 4),
                          "lots_added": add, "Q": round(Q, 1), "avg": round(a_, 4),
                          "stop": round(stop, 4),
                          "risk": round(Q * (a_ - stop) * per_pt, 2)}
                    if not is_calls:
                        unreal = sum(lots * (level - L) * per_pt for L, _, lots in batches)
                        ev.update({"units": round(Q * per_pt, 4),
                                   "notional": round(Q * per_pt * level, 2),
                                   "unreal": round(unreal, 2)})
                    trace.append(ev)
                if step >= target_streak:
                    exit_px, exit_date, reason = level, d, "target"
                    break
            if reason:
                break
        if exit_px is None:
            break  # ran out of data with an open position

        days_held = max((exit_date - entry_day).days, 0)
        if mode == "scalp":
            # final loss leg if we exited on stop without a step this campaign
            if reason in ("stop", "expiry"):
                bank -= base_bet
                res.table.append({"i": len(res.table) + 1, "entry": entry_day.date().isoformat(),
                                  "step": step, "level": round(R0, 2), "lots": 1.0,
                                  "avg": round(R0, 2), "stop": round(stop, 2),
                                  "reason": reason, "pnl": round(-base_bet, 2)})
                res.equity_dates.append(exit_date); res.equity.append(bank)
                res.cum_commission.append(cumc); res.cum_slippage.append(cums)
                res.cum_cost.append(cumc + cums)
            if trace is not None:
                trace.append({"t": "exit", "camp": n_cycles + 1,
                              "date": exit_date.date().isoformat(), "reason": reason,
                              "price": round(float(exit_px), 4), "steps": int(step),
                              "bank": round(bank, 2)})
            n_cycles += 1
        else:
            # pyramid P&L of the whole stack
            n_rolls = 0
            if is_calls:
                gross, comm_c, slip_c, n_rolls = _calls_campaign_pnl(
                    daily, entry_day, exit_date, exit_px, batches, per_pt,
                    r=r, dte_days=dte_days, target_delta=target_delta, qdiv=qdiv,
                    realized_vol=realized_vol, default_sigma=default_sigma,
                    roll_buffer=roll_buffer_days, commission_pct=commission_pct,
                    slippage_pct=slippage_pct, vol_model=vol_model,
                    trace=trace, camp=(n_cycles + 1))
            else:
                gross = sum(lots * (exit_px - L) * per_pt for L, _, lots in batches)
                notional = sum(lots * per_pt * (L + exit_px) for L, _, lots in batches)
                comm_c = commission_pct / 100.0 * notional
                slip_c = slippage_pct / 100.0 * notional
            cost = comm_c + slip_c
            cumc += comm_c
            cums += slip_c
            pnl = gross - cost
            bank += pnl
            wins += reason == "target"
            n_cycles += 1
            a = avg_price()
            res.equity_dates.append(exit_date); res.equity.append(bank)
            res.cum_commission.append(cumc); res.cum_slippage.append(cums)
            res.cum_cost.append(cumc + cums)
            row = {"i": n_cycles, "entry": entry_day.date().isoformat(),
                   "exit": exit_date.date().isoformat(), "days": days_held,
                   "entry_px": round(R0, 2), "exit_px": round(exit_px, 2),
                   "atr_step": round(h, 2), "steps": int(peak_step), "lots_Q": round(Q, 1),
                   "avg": round(a, 2), "stop": round(stop, 2), "reason": reason,
                   "gross": round(gross, 2), "cost": round(cost, 2),
                   "pnl": round(pnl, 2), "bank": round(bank, 2)}
            if is_calls:
                row["strike"] = round(K, 2)
                row["delta_entry"] = round(d0, 3)
                row["rolls"] = n_rolls
            res.table.append(row)
            if trace is not None:
                ev = {"t": "exit", "camp": n_cycles,
                      "date": exit_date.date().isoformat(), "reason": reason,
                      "price": round(float(exit_px), 4), "steps": int(peak_step),
                      "Q": round(Q, 1), "avg": round(a, 4), "stop": round(stop, 4),
                      "gross": round(gross, 2), "cost": round(cost, 2),
                      "pnl": round(pnl, 2), "bank": round(bank, 2)}
                if not is_calls:
                    ev.update({"units": round(Q * per_pt, 4),
                               "notional": round(Q * per_pt * float(exit_px), 2)})
                trace.append(ev)

        # entry marker for the price chart (green=target win, red=stop/expiry loss)
        res.trials.append(Trial(entry_day, exit_date, entry_price0, float(exit_px),
                                float(h), "win" if reason == "target" else "loss",
                                days_held, reason))

        later = wk_index[(wk_index - pd.Timedelta(days=6)) > exit_date]
        if len(later) == 0:
            break
        new_pos = wk_index.get_loc(later[0])
        pos = new_pos if new_pos > pos else pos + 1

    res.n_trials = n_cycles
    res.n_cycles = n_cycles
    res.wins = wins
    res.final_bank = bank
    res.empirical_p = wins / n_cycles if n_cycles else 0.0
    res.max_drawdown = _drawdown(res.equity)
    from .simcore import closed_form_ev_cycle
    res.closed_form_ev_cycle = closed_form_ev_cycle(base_bet, target_streak, res.empirical_p)
    res.total_commission = cumc
    res.total_slippage = cums
    res.total_cost = cumc + cums
    res.cost_per_cycle = res.total_cost / n_cycles if n_cycles else 0.0
    res.cost_as_prob, res.breakeven_p_with_cost = cost_as_probability(
        res.total_cost, n_cycles, base_bet, target_streak)
    res.commission_as_prob = cost_as_probability(cumc, n_cycles, base_bet, target_streak)[0]
    res.slippage_as_prob = cost_as_probability(cums, n_cycles, base_bet, target_streak)[0]
    return res


def _finalize(res: BacktestResult, base_bet: float, target_streak: int,
              wins: int, bank: float, n_cycles: int = 0) -> None:
    from .simcore import closed_form_ev_cycle
    res.n_trials = len(res.trials)
    res.wins = wins
    res.final_bank = bank
    res.empirical_p = wins / res.n_trials if res.n_trials else 0.0
    res.max_drawdown = _drawdown(res.equity)
    res.closed_form_ev_cycle = closed_form_ev_cycle(base_bet, target_streak, res.empirical_p)
    # cost aggregates
    res.total_commission = res.cum_commission[-1] if res.cum_commission else 0.0
    res.total_slippage = res.cum_slippage[-1] if res.cum_slippage else 0.0
    res.total_cost = res.total_commission + res.total_slippage
    res.n_cycles = n_cycles
    res.cost_per_cycle = res.total_cost / n_cycles if n_cycles else 0.0
    res.cost_as_prob, res.breakeven_p_with_cost = cost_as_probability(
        res.total_cost, n_cycles, base_bet, target_streak)
    res.commission_as_prob = cost_as_probability(
        res.total_commission, n_cycles, base_bet, target_streak)[0]
    res.slippage_as_prob = cost_as_probability(
        res.total_slippage, n_cycles, base_bet, target_streak)[0]


def run_call_coinflip(daily: pd.DataFrame, weekly: pd.DataFrame, weekly_atr: pd.Series, *,
                      base_bet: float, target_streak: int, mult: float = 1.0,
                      double_target: float = 2.0, target_delta: float = 0.5,
                      dte_days: int = 365, iv: float = 0.20, r: float = 0.045, qdiv: float = 0.0,
                      commission_pct: float = 0.0, slippage_pct: float = 0.0,
                      starting_bank: float = 0.0, realized_vol: "pd.Series | None" = None,
                      trace: list | None = None) -> BacktestResult:
    """Long-call COIN-FLIP: the bet is the PREMIUM, so risk is capped at b by construction.

    A long call cannot lose more than its premium, so if each bet spends exactly the current
    stake on premium and we RIDE the proceeds (never inject fresh capital mid-cycle), the whole
    cycle can lose at most the initial b — a true coin-flip with risk ≡ 1 unit, no stop needed.

    One ROUND: buy calls (strike at `target_delta`) for `stake` of premium. WIN = the call
    reaches `double_target`× its premium (it doubled) before expiry → sell, roll ALL proceeds
    into the next round (stake ×= double_target), streak++. The price level where it doubles is
    solved from Black–Scholes (`price_for_value`) and reported as a DYNAMIC `m·ATR` — it is NOT
    a fixed 2·ATR; a higher entry delta needs a different move. A streak of N wins ends the cycle
    with ≈ b·(double_target^N − 1). If a round reaches expiry without doubling → cycle ends, sell
    the salvage; since proceeds ≥ 0 and only b was ever injected, cycle P&L ≥ −b.
    """
    res = BacktestResult()
    daily = daily.sort_index()
    idx = daily.index
    hi_s, cl_s = daily["High"].to_numpy(float), daily["Close"].to_numpy(float)
    T0 = dte_days / 365.0
    bank = starting_bank
    n_cycles = wins = 0
    cumc = 0.0
    fee = (commission_pct + slippage_pct) / 100.0

    def atr_at(date):
        v = weekly_atr.asof(date)
        return float(v) * mult if v is not None and np.isfinite(v) and float(v) > 0 else float("nan")

    i = 0
    while i < len(idx) and not np.isfinite(atr_at(idx[i])):
        i += 1

    while i < len(idx):
        h = atr_at(idx[i])
        if not np.isfinite(h) or h <= 0:
            i += 1
            continue
        # ---- cycle ----
        stake = base_bet
        streak = 0
        cycle_cost = 0.0
        S = float(cl_s[i])
        entry_date = idx[i]
        cur = i
        outcome = None
        proceeds = 0.0
        last_sell_i = i
        while True:
            # IV per round: real per-date volatility if provided (backtest), else the constant (demo)
            iv_r = _sigma_at(realized_vol, entry_date, iv) if realized_vol is not None else iv
            K = float(opt.strike_for_delta(S, T0, r, iv_r, target_delta, qdiv))
            prem_per = float(opt.call_price(S, K, T0, r, iv_r, qdiv))
            if prem_per <= 1e-9:
                outcome = "degenerate"
                break
            contracts = stake / prem_per
            d0 = float(opt.call_delta(S, K, T0, r, iv_r, qdiv))
            target_per = double_target * prem_per
            S_star = float(opt.price_for_value(target_per, K, T0, r, iv_r, qdiv))
            m_star = (S_star - S) / h
            expiry = entry_date + pd.Timedelta(days=dte_days)
            cycle_cost += fee * stake                       # buy fill

            round_win = False
            sell_S = float(cl_s[-1]); sell_date = idx[-1]; T_rem = 1e-6
            j = cur
            while j < len(idx):
                d = idx[j]
                if d < entry_date:
                    j += 1
                    continue
                T_rem = max((expiry - d).days / 365.0, 1e-6)
                if float(opt.call_price(hi_s[j], K, T_rem, r, iv_r, qdiv)) >= target_per:
                    round_win, sell_S, sell_date = True, S_star, d
                    break
                if d >= expiry:
                    sell_S, sell_date, T_rem = float(cl_s[j]), d, 1e-6
                    break
                j += 1

            # WIN books exactly the doubling value (the win condition IS "worth ≥ 2× premium",
            # so we conservatively realise 2× even if the bar overshot); LOSS books the real
            # expiry/salvage value. Either way proceeds ≥ 0 ⇒ cycle P&L ≥ −b.
            val_per = target_per if round_win else float(opt.call_price(sell_S, K, T_rem, r, iv_r, qdiv))
            proceeds = contracts * val_per
            cycle_cost += fee * proceeds                    # sell fill
            last_sell_i = j if j < len(idx) else len(idx) - 1

            if trace is not None:
                trace.append({"t": "cf_round", "camp": n_cycles + 1, "round": streak + 1,
                              "date": entry_date.date().isoformat(), "entry": round(S, 4),
                              "atr": round(h, 4), "strike": round(K, 4), "iv": round(iv_r, 4),
                              "delta": round(d0, 4), "prem_per": round(prem_per, 4),
                              "contracts": round(contracts, 2), "stake": round(stake, 2),
                              "double_at": round(S_star, 4), "m_atr": round(m_star, 3),
                              "sell": round(sell_S, 4), "sell_date": sell_date.date().isoformat(),
                              "val_per": round(val_per, 4), "proceeds": round(proceeds, 2),
                              "win": round_win})

            if round_win:
                streak += 1
                wins += 1
                stake = proceeds                            # RIDE: reinvest all proceeds
                S, entry_date, cur = sell_S, sell_date, last_sell_i
                if streak >= target_streak:
                    outcome = "target"
                    break
            else:
                outcome = "expiry"
                break

        cycle_pnl = proceeds - base_bet - cycle_cost
        bank += cycle_pnl
        cumc += cycle_cost
        n_cycles += 1
        won = outcome == "target"
        res.equity_dates.append(sell_date)
        res.equity.append(bank)
        res.cum_commission.append(cumc)
        res.cum_slippage.append(0.0)
        res.cum_cost.append(cumc)
        res.table.append({"i": n_cycles, "entry": idx[i].date().isoformat(),
                          "exit": sell_date.date().isoformat(), "streak": streak,
                          "reason": outcome, "outcome": "win" if won else "loss",
                          "premium_in": round(base_bet, 2), "proceeds": round(proceeds, 2),
                          "cost": round(cycle_cost, 2), "pnl": round(cycle_pnl, 2),
                          "bank": round(bank, 2)})
        res.trials.append(Trial(idx[i], sell_date, float(cl_s[i]), float(sell_S),
                                float(h), "win" if won else "loss",
                                max((sell_date - idx[i]).days, 0), outcome or ""))
        if trace is not None:
            trace.append({"t": "cf_exit", "camp": n_cycles, "date": sell_date.date().isoformat(),
                          "reason": outcome, "streak": streak, "pnl": round(cycle_pnl, 2),
                          "proceeds": round(proceeds, 2), "bank": round(bank, 2)})

        nxt = idx[idx > sell_date]
        if len(nxt) == 0:
            break
        i = int(idx.get_loc(nxt[0]))

    res.n_trials = n_cycles
    res.wins = wins
    res.n_cycles = n_cycles
    res.final_bank = bank
    res.empirical_p = wins / n_cycles if n_cycles else 0.0
    res.max_drawdown = _drawdown(res.equity)
    res.total_commission = cumc
    res.total_cost = cumc
    return res
