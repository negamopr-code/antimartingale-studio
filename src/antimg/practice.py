"""Practice-tab calculator: a MANUAL options construction in concrete numbers.

The Practice tab's job is to take a concrete example out of a NotebookLM corpus,
a picture, or a Claude analysis and let the user rebuild it: enter futures price,
strike, premiums (or IV), legs — get the payoff graph, breakevens, max loss and
the theta the scalp must cover. Pure functions, no I/O — the web layer just
serializes the result.

Legs supported (covers both ПИ variants):
- LONG `n_calls` calls at strike K (premium points per call);
- LONG `n_puts` puts at the same K (own premium — a CLASSIC straddle/strangle-at-K
  like the real «30 Put + 30 Call MES» ticket, where call/put asks differ);
- SHORT `n_futs` futures from S0 (the synthetic-straddle 2C−1F shape).

Conventions match pi_sim: prices in points, `multiplier` converts points→$.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import options as opt


def implied_vol(premium: float, S: float, K: float, T: float, r: float,
                lo: float = 0.01, hi: float = 5.0, iters: int = 60) -> float | None:
    """Bisection BS implied vol from a call premium (points). None if out of bracket."""
    if premium <= max(0.0, S - K) or T <= 0:          # below intrinsic → no time value
        return None
    if opt.call_price(S, K, T, r, hi) < premium:
        return None
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if opt.call_price(S, K, T, r, mid) < premium:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


@dataclass
class Construction:
    S0: float = 0.0                 # futures/underlying price at entry
    K: float = 0.0                  # strike (calls and puts share it)
    n_calls: float = 2.0
    n_puts: float = 0.0             # LONG puts at K (classic straddle leg)
    n_futs: float = 1.0             # SHORT futures legs
    premium: float = 0.0            # per-call premium, points
    put_premium: float = 0.0        # per-put premium, points (0 when no puts)
    iv: float | None = None         # BS sigma actually used (given or implied from the call)
    dte_days: float = 30.0
    r: float = 0.045
    multiplier: float = 1.0         # $ per point per lot
    lots: float = 1.0               # whole-construction scale
    # derived
    premium_total: float = 0.0      # $ paid for ALL options = the loss cap (when long-covered)
    max_loss: float = 0.0           # worst expiry P&L on the grid, $
    max_loss_at: float = 0.0        # S_T of the worst point (the kink K for a proper straddle)
    be_down: float | None = None
    be_up: float | None = None
    be_down_pct: float | None = None
    be_up_pct: float | None = None
    delta0: float | None = None     # construction delta at entry (futures equivalent)
    theta_day: float | None = None  # $/day decay at entry (negative = bleed)
    theta_period: float | None = None
    scalp_per_day_needed: float | None = None
    payoff: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def build(S0: float, K: float, *, n_calls: float = 2.0, n_puts: float = 0.0,
          n_futs: float = 1.0, premium: float | None = None,
          put_premium: float | None = None, iv: float | None = None,
          dte_days: float = 30.0, r: float = 0.045,
          multiplier: float = 1.0, lots: float = 1.0, n_grid: int = 121) -> Construction:
    """Price (if needed) and fully describe one construction. Either `premium`
    (points per call, from the real example) or `iv` must be given; with both,
    the explicit premium wins and `iv` is re-implied from it. With puts present,
    `put_premium` is used as given (real ask from the ticket) or BS-priced off
    the same sigma when omitted."""
    if S0 <= 0 or K <= 0:
        raise ValueError("S0 and strike must be positive")
    if min(n_calls, n_puts, n_futs) < 0 or n_calls + n_puts + n_futs == 0:
        raise ValueError("need at least one leg")
    T = max(dte_days, 0.5) / 365.0
    c = Construction(S0=S0, K=K, n_calls=n_calls, n_puts=n_puts, n_futs=n_futs,
                     dte_days=dte_days, r=r, multiplier=multiplier, lots=lots)
    if premium is not None and premium > 0:
        c.premium = float(premium)
        c.iv = implied_vol(premium, S0, K, T, r)
        if c.iv is None:
            c.notes.append("премия ≤ внутренней стоимости или вне диапазона — IV не извлечь; "
                           "греки (Δ, тета) не считаются")
    elif iv is not None and iv > 0:
        c.iv = float(iv)
        c.premium = float(opt.call_price(S0, K, T, r, c.iv)) if n_calls > 0 else 0.0
    else:
        raise ValueError("give either premium (points per call) or iv")
    if n_puts > 0:
        if put_premium is not None and put_premium > 0:
            c.put_premium = float(put_premium)
        elif c.iv is not None:
            c.put_premium = float(opt.put_price(S0, K, T, r, c.iv))
            c.notes.append(f"премия пута не задана — оценена по BS от той же IV: "
                           f"{c.put_premium:.2f} пп")
        else:
            c.put_premium = c.premium
            c.notes.append("премия пута не задана и IV не извлечь — взята равной премии колла")

    scale = multiplier * lots
    prem_pts = c.n_calls * c.premium + c.n_puts * c.put_premium    # total premium, points
    c.premium_total = prem_pts * scale

    # --- expiry payoff -----------------------------------------------------------------
    # pnl(S) = scale·[n_c·max(S−K,0) + n_p·max(K−S,0) − n_f·(S−S0) − prem_pts]
    def pnl(s: float) -> float:
        return scale * (c.n_calls * max(s - K, 0.0) + c.n_puts * max(K - s, 0.0)
                        - c.n_futs * (s - S0) - prem_pts)

    # analytic breakevens (piecewise linear: slope −(n_p+n_f) below K, n_c−n_f above)
    dn_slope = c.n_puts + c.n_futs
    if dn_slope > 0:
        bd = (c.n_puts * K + c.n_futs * S0 - prem_pts) / dn_slope
        c.be_down = bd if bd < K else None     # the down-leg only exists below the kink
    if c.n_calls > c.n_futs:
        bu = (prem_pts + c.n_calls * K - c.n_futs * S0) / (c.n_calls - c.n_futs)
        c.be_up = bu if bu > K else None
    c.be_down_pct = round(100.0 * (c.be_down / S0 - 1.0), 2) if c.be_down else None
    c.be_up_pct = round(100.0 * (c.be_up / S0 - 1.0), 2) if c.be_up else None

    # grid wide enough to show both breakevens (or ±25%)
    lo_b = c.be_down if c.be_down else S0 * 0.85
    hi_b = c.be_up if c.be_up else S0 * 1.15
    span = max(S0 - lo_b, hi_b - S0, 0.10 * S0) * 1.8
    S_grid = [S0 - span + 2 * span * i / (n_grid - 1) for i in range(n_grid)]
    # the kink K (and the breakevens) must be ON the grid — the true max loss sits exactly
    # at the strike, which a uniform grid centred on S0 ≠ K would otherwise straddle
    S_grid += [K] + [b for b in (c.be_down, c.be_up) if b]
    S_grid = sorted({round(s, 6) for s in S_grid if s > 0})
    expiry = [pnl(s) for s in S_grid]
    c.max_loss = min(expiry)
    c.max_loss_at = round(S_grid[expiry.index(c.max_loss)], 4)

    out = {"S": [round(s, 4) for s in S_grid], "S0": S0, "K": K,
           "expiry": [round(v, 2) for v in expiry]}
    if c.n_futs > c.n_calls:
        c.notes.append("фьючей больше, чем коллов — непокрытый шорт, убыток вверх НЕ ограничен "
                       "(это уже не ПИ-конструкция)")

    # --- greeks + today curve (need a usable sigma) ------------------------------------
    if c.iv is not None:
        dc = float(opt.call_delta(S0, K, T, r, c.iv))
        c.delta0 = round(c.n_calls * dc + c.n_puts * (dc - 1.0) - c.n_futs, 4)

        def _opt_value(s: float, t: float) -> float:
            v = 0.0
            if c.n_calls:
                v += c.n_calls * float(opt.call_price(s, K, t, r, c.iv))
            if c.n_puts:
                v += c.n_puts * float(opt.put_price(s, K, t, r, c.iv))
            return v
        T1 = max(T - 1.0 / 365.0, 1e-6)
        c.theta_day = round((_opt_value(S0, T1) - _opt_value(S0, T)) * scale, 2)
        c.theta_period = round(c.theta_day * dte_days, 2)
        c.scalp_per_day_needed = round(-c.theta_day, 2) if c.theta_day < 0 else 0.0
        today = [scale * (_opt_value(s, T) - c.n_futs * (s - S0) - prem_pts) for s in S_grid]
        out["today"] = [round(v, 2) for v in today]

    c.payoff = out
    if c.n_puts == 0 and abs(c.n_calls - 2 * c.n_futs) < 1e-9 and abs(K - S0) / S0 < 0.02:
        c.notes.append("классический синтетический стреддл 2С−1Ф около денег: "
                       "макс. убыток = премия, дельта ≈ 0")
    if c.n_futs == 0 and c.n_puts > 0 and abs(c.n_calls - c.n_puts) < 1e-9:
        c.notes.append("классический длинный стреддл (коллы + путы на одном страйке): "
                       "макс. убыток = вся премия на страйке, прибыль в обе стороны")
    return c
