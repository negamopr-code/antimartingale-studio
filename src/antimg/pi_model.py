"""Closed-form P&L attribution for the Прикрытый Интрадей (ПИ) method.

This is the *mathematical model* behind the backtest: a small set of equations that approximately
reproduces the engine's three P&L streams and CONCLUDES which part builds which part of the profit.

THE MODEL (all quantities annual, in account currency; sized to a fixed risk budget P = ρ·B):

    let  a   = ρ·B / (2·T)            # theta-rate: annual cost of carrying the long straddle
         vr  = σ_R / σ_I              # realized-to-implied vol ratio (the single state variable)
         C_s = K · f · √252 / (0.4·√T)# scalp constant (K = intraday edge, f = three-thirds frac)

    Θ (theta cost) = − a                                  # ALWAYS negative; the rent on long vol
    Γ (gamma/trend)= + a · vr² · g                        # straddle monetises directional variance
    Σ (scalp/flat) = + C_s · ρB · vr                      # counter-trend grid harvests range variance
    ────────────────────────────────────────────
    Total          = Γ + Σ + Θ  =  a·(vr²·g − 1) + C_s·ρB·vr

Where the constants come from:
  • a = ρB/(2T): an ATM call ≈ 0.4·σ_I·S·√T, so a straddle's premium ≈ 0.8·σ_I·S·√T and the number
    of straddles n = ρB/(0.8·σ_I·S·√T). The dollar-gamma of n ATM straddles is Γ$ ≈ ρB/(σ_I²·T), and
    the per-period theta = −½·Γ$·σ_I²·T = −½ρB, i.e. an annual rate a = ρB/(2T). Note a is
    **vol-independent in $** — premium ∝ σ_I but contract count ∝ 1/σ_I cancel (this is why coverage
    is vol-invariant, the earlier result).
  • Γ = ½·Γ$·σ_R²·(per year) = a·(σ_R/σ_I)² — but a STATICALLY base-hedged straddle only monetises the
    DIRECTIONAL (trend / overnight) slice of realized variance; the intraday slice is harvested by the
    scalp. `g` ∈ [0,1] is that gamma-capture fraction (trend-heavy names → g→1; choppy names → g small).
  • Σ = K·L_total·σ$·(252 days), L_total = 2·n·f, σ$ = (σ_R/√252)·S → collapses to C_s·ρB·vr (the S and
    σ_I cancel — same vol-invariance). Linear in vr; K is the calibrated intraday mean-reversion edge.

THE CONCLUSION (what part builds which part of the profit):
  • Γ ∝ vr²  → CONVEX: the straddle gamma builds profit in TRENDS / big moves (realized ≫ implied).
  • Σ ∝ vr   → LINEAR: the scalp builds the steady profit in the FLAT (it "pays the theta").
  • Θ = −a   → the constant cost both engines fight.
  ⇒ Profitable ⟺  vr²·g + 2T·C_s·vr > 1   (gamma's variance-ratio capture + the scalp's theta coverage).
  In calm markets (vr≈1) the linear scalp tends to dominate; in turbulent markets the squared gamma term
  takes over. Attribution = each positive term's share of the gross profit.

Constants validated against the BS engine (GLD/SLV/SPY 2019–22): theta & scalp within ~10–15%; the
gamma-capture g is genuinely per-instrument (SPY 0.68 trends hard, SLV 0.28 chops) and is calibrated
from the backtest. Not financial advice — an educational reproduction of a third-party method.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

TRADING_DAYS = 252


@dataclass
class PiAttribution:
    # the three streams (annualised to the run's length via `years`)
    theta: float = 0.0            # Θ ≤ 0 — the cost
    gamma_trend: float = 0.0      # Γ ≥ 0 — built by trends (straddle gamma)
    scalp_flat: float = 0.0       # Σ — built in the flat (counter-trend scalp); <0 if edge K<0
    total: float = 0.0            # Γ + Σ + Θ
    # attribution of the GROSS positive profit
    gross_profit: float = 0.0     # Γ + max(Σ,0)
    pct_from_trend: float = 0.0   # gamma share of gross (%)
    pct_from_flat: float = 0.0    # scalp share of gross (%)
    profitable: bool = False
    regime: str = ""              # "trend-built (gamma)" | "flat-built (scalp)" | "bleeding (theta wins)"
    conclusion: str = ""
    # the model state / params (for transparency)
    a: float = 0.0                # theta-rate ρB/2T
    c_s: float = 0.0              # scalp constant
    vr: float = 0.0               # σ_R/σ_I
    gamma_capture: float = 0.0    # g
    profitable_condition: float = 0.0   # vr²g + 2T·C_s·vr  (>1 ⇒ profitable)


def scalp_constant(scalp_k: float, dte_years: float, intraday_frac: float = 1.0 / 3.0) -> float:
    """C_s = K · f · √252 / (0.4·√T) — the linear scalp coefficient (see module docstring)."""
    return scalp_k * intraday_frac * math.sqrt(TRADING_DAYS) / (0.4 * math.sqrt(max(dte_years, 1e-9)))


def closed_form(bank: float, risk_pct: float, dte_years: float, sigma_implied: float,
                sigma_realized: float, *, scalp_k: float, intraday_frac: float = 1.0 / 3.0,
                gamma_capture: float = 1.0, years: float = 1.0) -> PiAttribution:
    """Compute the closed-form ПИ P&L decomposition + attribution from vol stats alone.

    `gamma_capture` (g) is the fraction of realized variance the base-hedged straddle monetises as
    directional gamma (the rest is the scalp's range variance); calibrate it from a backtest with
    `calibrate_gamma_capture` or pass a default (1.0 = attribute all realized variance to gamma).
    """
    res = PiAttribution()
    T = max(dte_years, 1e-9)
    sI = max(sigma_implied, 1e-9)
    a = risk_pct * bank / (2.0 * T)
    c_s = scalp_constant(scalp_k, T, intraday_frac)
    vr = sigma_realized / sI
    res.a, res.c_s, res.vr, res.gamma_capture = a, c_s, vr, gamma_capture
    res.theta = -a * years
    res.gamma_trend = a * vr * vr * gamma_capture * years
    res.scalp_flat = c_s * risk_pct * bank * vr * years
    res.total = res.gamma_trend + res.scalp_flat + res.theta
    return _attribute(res, T)


def attribute_measured(theta: float, gamma_trend: float, scalp_flat: float,
                       dte_years: float = 0.5) -> PiAttribution:
    """Same attribution + conclusion, but from MEASURED engine streams (theta≤0, gamma=gamma_dir_pnl,
    scalp=scalp_pnl) — so the conclusion rests on the real backtest numbers, explained by the model."""
    res = PiAttribution(theta=theta, gamma_trend=gamma_trend, scalp_flat=scalp_flat,
                        total=theta + gamma_trend + scalp_flat)
    return _attribute(res, max(dte_years, 1e-9))


def _attribute(res: PiAttribution, T: float) -> PiAttribution:
    gross = res.gamma_trend + max(res.scalp_flat, 0.0)
    res.gross_profit = gross
    if gross > 1e-9:
        res.pct_from_trend = 100.0 * res.gamma_trend / gross
        res.pct_from_flat = 100.0 * max(res.scalp_flat, 0.0) / gross
    res.profitable = res.total > 0
    # the dimensionless profitability gauge: vr²g + 2T·C_s·vr (>1 ⇒ net positive), when params present
    if res.a > 0:
        res.profitable_condition = (res.vr ** 2) * res.gamma_capture + 2.0 * T * res.c_s * res.vr
    cost = -res.theta
    if not res.profitable:
        res.regime = "bleeding (theta wins)"
    elif res.gamma_trend >= max(res.scalp_flat, 0.0):
        res.regime = "trend-built (gamma)"
    else:
        res.regime = "flat-built (scalp)"
    # plain-language conclusion of "what part builds which part of profit"
    if res.profitable:
        lead = "тренд/гамма" if res.gamma_trend >= max(res.scalp_flat, 0.0) else "флет/скальп"
        res.conclusion = (
            f"ПРИБЫЛЬНО (+{res.total:,.0f}). Профит строит ГЛАВНЫМ ОБРАЗОМ {lead}: "
            f"гамма (тренд) даёт {res.pct_from_trend:.0f}% валовой прибыли, "
            f"скальп (флет) {res.pct_from_flat:.0f}%; оба бьют тету −{cost:,.0f}.")
    else:
        res.conclusion = (
            f"УБЫТОК ({res.total:,.0f}). Тета −{cost:,.0f} не покрыта: "
            f"гамма (тренд) +{res.gamma_trend:,.0f} и скальп (флет) +{max(res.scalp_flat,0):,.0f} "
            f"вместе меньше стоимости стреддла.")
    return res


def calibrate_gamma_capture(gamma_dir_pnl: float, bank: float, risk_pct: float, dte_years: float,
                            sigma_implied: float, sigma_realized: float, years: float) -> float:
    """Back out g so the closed-form gamma term matches the engine's measured gamma_dir_pnl:
        g = gamma_dir_pnl / (a · vr² · years).
    Lets the closed-form reproduce the backtest exactly on the gamma leg (theta & scalp already match
    from first principles). Clamped to [0, 3]."""
    T = max(dte_years, 1e-9)
    a = risk_pct * bank / (2.0 * T)
    vr = sigma_realized / max(sigma_implied, 1e-9)
    denom = a * vr * vr * max(years, 1e-9)
    if abs(denom) < 1e-9:
        return 1.0
    return max(0.0, min(gamma_dir_pnl / denom, 3.0))
