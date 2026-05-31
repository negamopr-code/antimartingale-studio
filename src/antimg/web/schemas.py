"""Pydantic request models (validation + anti-DoS caps)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .config import settings


class CoinFlipReq(BaseModel):
    iterations: int = Field(100_000, ge=1, le=settings.max_iterations)
    target_streak: int = Field(10, ge=1, le=settings.max_target_streak)
    base_bet: float = Field(1.0, gt=0)
    win_prob: float = Field(0.5, ge=0.0, le=1.0)
    mode: str = Field("separate", pattern="^(separate|continuous)$")
    seed: int | None = None
    stop_at_first_target: bool = True   # original behaviour: stop at the first target streak


class BacktestReq(BaseModel):
    ticker: str = Field("SPY", min_length=1, max_length=20)
    start: str = settings.default_start
    atr_period: int = Field(14, ge=2, le=200)
    mult: float = Field(1.0, gt=0, le=20)
    base_bet: float = Field(100.0, gt=0)
    target_streak: int = Field(10, ge=1, le=settings.max_target_streak)
    commission_pct: float = Field(0.035, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    slippage_pct: float = Field(0.01, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    starting_bank: float = Field(10_000.0)
    cap_mult: float | None = Field(None, gt=0)
    mode: str = Field("pyramid", pattern="^(pyramid|scalp)$")  # pyramid = scale-in; scalp = book each step


class OptionsReq(BacktestReq):
    r: float = Field(0.045, ge=-0.05, le=0.5)
    dte_days: int = Field(365, ge=1, le=3650)
    target_delta: float = Field(0.95, gt=0, lt=1)
    iv_window: int = Field(20, ge=2, le=500)
    roll_buffer_days: int = Field(5, ge=0, le=60)   # roll the call this many days before expiry
    iv_source: str = Field("auto", pattern="^(auto|vix|index|realized|constant)$")  # auto=vol-index by class
    iv_const: float = Field(0.20, gt=0, le=3)       # used when iv_source=constant
    skew_beta: float | None = Field(None, ge=-2, le=2)  # additive IV per unit ln-moneyness; None=asset-class default
    use_term_structure: bool = True   # interpolate real CBOE vol-index term structure to the option tenor
    # option model: 'pyramid' = scale-into-one-position on the ATR grid (delta-normalised);
    # 'coinflip' = long-call coin-flip (premium IS the bet, risk ≤ b, double-or-roll)
    opt_model: str = Field("pyramid", pattern="^(pyramid|coinflip)$")
    double_target: float = Field(2.0, gt=1, le=10)   # coinflip: value multiple that counts as a win
    iv_markup: float = Field(1.25, ge=1, le=3)       # coinflip: pay IV = realized × this (variance risk premium)


class ScanReq(BaseModel):
    """Run the linear (shares) campaign across the whole instrument catalog. Same knobs as
    BacktestReq minus `ticker` (the scan iterates every catalog ticker itself)."""
    start: str = settings.default_start
    atr_period: int = Field(14, ge=2, le=200)
    mult: float = Field(1.0, gt=0, le=20)
    base_bet: float = Field(100.0, gt=0)
    target_streak: int = Field(10, ge=1, le=settings.max_target_streak)
    commission_pct: float = Field(0.035, ge=0, le=50)
    slippage_pct: float = Field(0.01, ge=0, le=50)
    starting_bank: float = Field(10_000.0)
    cap_mult: float | None = Field(None, gt=0)
    mode: str = Field("pyramid", pattern="^(pyramid|scalp)$")


class ExplainReq(BaseModel):
    """Step-by-step trace of one campaign on a synthetic flat/up/down path (Explain tab)."""
    scenario: str = Field("uptrend", pattern="^(flat|uptrend|downtrend)$")
    target_streak: int = Field(4, ge=1, le=8)   # small N so the pyramid is readable
    mult: float = Field(1.0, gt=0, le=20)
    base_bet: float = Field(100.0, gt=0)
    atr_period: int = Field(4, ge=2, le=20)
    instrument: str = Field("shares", pattern="^(shares|calls)$")
    target_delta: float = Field(0.5, gt=0, lt=1)   # calls only
    dte_days: int = Field(45, ge=7, le=3650)       # calls only
    iv: float = Field(0.20, gt=0, le=3)            # calls only: constant IV for a clean demo
    double_target: float = Field(2.0, gt=1, le=10)  # calls coin-flip: value multiple that counts as a "win"


class FromSignalsReq(BaseModel):
    strategy_id: str | None = None
    base_bet: float = Field(100.0, gt=0)
    target_streak: int = Field(10, ge=1, le=settings.max_target_streak)
    commission_pct: float = Field(0.035, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    slippage_pct: float = Field(0.01, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    starting_bank: float = Field(10_000.0)
    cap_mult: float | None = Field(None, gt=0)
