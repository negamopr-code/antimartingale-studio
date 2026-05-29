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
    commission_pct: float = Field(0.0, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    slippage_pct: float = Field(0.0, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    starting_bank: float = Field(10_000.0)
    cap_mult: float | None = Field(None, gt=0)


class OptionsReq(BacktestReq):
    r: float = Field(0.045, ge=-0.05, le=0.5)
    dte_days: int = Field(365, ge=1, le=3650)
    target_delta: float = Field(0.95, gt=0, lt=1)
    iv_window: int = Field(20, ge=2, le=500)


class FromSignalsReq(BaseModel):
    strategy_id: str | None = None
    base_bet: float = Field(100.0, gt=0)
    target_streak: int = Field(10, ge=1, le=settings.max_target_streak)
    commission_pct: float = Field(0.0, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    slippage_pct: float = Field(0.0, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    starting_bank: float = Field(10_000.0)
    cap_mult: float | None = Field(None, gt=0)
