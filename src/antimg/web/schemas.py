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
    # cap on lots added per scale-in step (add = min(2^step, cap_mult)); default 8 bounds the
    # otherwise-exponential ladder (1,2,4,8,8,8…) so notional stays realistic. Risk per stop is
    # still exactly b regardless (stop = avg − h/Q). Set 0/None to uncap (raw 2^N pyramid).
    cap_mult: float | None = Field(8.0, gt=0)
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
    cap_mult: float | None = Field(8.0, gt=0)   # shares: cap lots per scale-in (see BacktestReq)
    mode: str = Field("pyramid", pattern="^(pyramid|scalp)$")
    # which strategy to sweep: 'shares' = linear ATR pyramid (default); 'coinflip' = long-call coin-flip
    model: str = Field("shares", pattern="^(shares|coinflip)$")
    # coin-flip params (used when model='coinflip')
    target_delta: float = Field(0.5, gt=0, lt=1)
    dte_days: int = Field(45, ge=7, le=3650)
    iv_window: int = Field(20, ge=2, le=500)
    iv_markup: float = Field(1.25, ge=1, le=3)
    double_target: float = Field(2.0, gt=1, le=10)
    r: float = Field(0.045, ge=-0.05, le=0.5)
    # stress test: decompose each instrument's net into drift / trend / noise-floor via IID
    # shuffle surrogates (destroy time-order, keep the bar distribution), plus the naive detrend
    # (reference) and, for coinflip, the breakeven IV markup. Exposes how much of the "edge" is
    # directional drift, how much is trend/momentum (serial structure), and how much is just a
    # fill/artifact floor that survives even on shuffled data. ~10-30x slower → opt-in.
    stress: bool = Field(False)
    shuffle_n: int = Field(8, ge=2, le=40)   # IID shuffle seeds per instrument (noisy → more = steadier)


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


class InspectReq(BaseModel):
    """Drill into a REAL instrument over a chosen window: run the engine with full tracing
    so every entry / scale-in / exit can be inspected campaign-by-campaign (Inspect tab)."""
    ticker: str = Field("SPY", min_length=1, max_length=20)
    start: str = "2020-01-01"
    end: str | None = None
    atr_period: int = Field(14, ge=2, le=200)
    mult: float = Field(1.0, gt=0, le=20)
    base_bet: float = Field(100.0, gt=0)
    target_streak: int = Field(10, ge=1, le=settings.max_target_streak)
    mode: str = Field("pyramid", pattern="^(pyramid|scalp)$")
    commission_pct: float = Field(0.0, ge=0, le=50)
    slippage_pct: float = Field(0.0, ge=0, le=50)
    starting_bank: float = Field(10_000.0)
    cap_mult: float | None = Field(8.0, gt=0)
    # shares = linear ATR pyramid; coinflip = long-call premium=bet; calls = pyramid of
    # delta-normalised long calls WITH auto-roll near expiry (the only model that rolls).
    model: str = Field("shares", pattern="^(shares|coinflip|calls)$")
    # coin-flip / calls params
    target_delta: float = Field(0.5, gt=0, lt=1)
    dte_days: int = Field(45, ge=7, le=3650)
    iv_window: int = Field(20, ge=2, le=500)
    iv_markup: float = Field(1.25, ge=1, le=3)
    double_target: float = Field(2.0, gt=1, le=10)
    roll_buffer_days: int = Field(5, ge=1, le=120)   # calls: re-strike within this many days of expiry
    r: float = Field(0.045, ge=-0.05, le=0.5)


class HedgedIntradayReq(BaseModel):
    """Прикрытый Интрадей (ПИ) backtest: synthetic straddle (2 ATM calls − 1 future) whose
    theta is paid by a counter-trend intraday scalping grid. Daily-bar approximation — the
    scalp efficiency knobs are explicit because we have no tick data (see engine docstring)."""
    ticker: str = Field("GLD", min_length=1, max_length=20)   # gold = corpus beginner pick
    start: str = "2018-01-01"
    end: str | None = None
    atr_period: int = Field(14, ge=2, le=200)                 # DAILY ATR (grid step scale)
    starting_bank: float = Field(10_000.0, gt=0)
    risk_pct: float = Field(0.20, gt=0, le=1.0)               # premium budget = risk_pct·bank
    dte_days: int = Field(365, ge=7, le=730)                  # long-dated straddle = slow theta (user's regime)
    roll_buffer_days: int = Field(10, ge=1, le=90)            # re-strike ATM this many days before expiry
    # doctrine roll (modules 26/27): roll IN PROFIT when the period's gain hits this % of the deposit
    # (≈ 5–7%/mo ref) — close the whole construction, re-open fresh ATM, compound, scrap stuck parts.
    # 0 = OFF (schedule-only roll at expiry).
    roll_profit_pct: float = Field(0.0, ge=0, le=100)
    r: float = Field(0.045, ge=-0.05, le=0.5)
    # 'grid' = event-driven daily/intraday counter-trend grid (measures the scalp on a real feed);
    # 'analytic' = VOL-DRIVEN approximation (scalp income = scalp_k·lots·daily-realized-$vol) so ANY
    # instrument gets an estimated ПИ behaviour from its own volatility with NO intraday feed — K is
    # calibrated to the free 1m-crypto ground truth (but embeds the edge, so it is an assumption — see
    # scalp_k); 'range' = legacy heuristic lower bound.
    # 'capture' = the SIMPLE direct estimate: scalp = scalp_capture × actual daily range × part_lots,
    # summed over real history, POSITIVE-ONLY (we close just wins; losers carried, capped by the
    # premium). The recommended honest model.
    scalp_model: str = Field("grid", pattern="^(grid|capture|analytic|range)$")
    # capture model: fraction of each day's (High−Low) the scalp books (doctrine ideal ~0.5, "catch
    # >50% of the move", achieved with ~200–250 trades/mo). The result is linear in it. Default 0.20 =
    # the grid-calibrated REALISTIC level (after costs/regime); 0.5 is the doctrine optimum, not typical.
    scalp_capture: float = Field(0.20, ge=0, le=3.0)
    # analytic model only: the scalp-efficiency / intraday-edge constant K (scalp ∝ K·lots·σ$).
    # ⚠ NOT universal — 1m crypto calibration gave ETH +0.06 / SOL ~0 / BTC −0.006; the result scales
    # linearly in K. Default = a modest positive edge; raise for a ranging name, drop/negative for a
    # trending one. Magnitude (∝ lots·σ$) is vol-invariant; K is the edge assumption.
    scalp_k: float = Field(0.02, ge=-1.0, le=5.0)
    # scalp data feed: 'daily' = one OHLC bar/day (intraday chop invisible); 'hourly' = real 60m bars
    # (yfinance ~730d history); '1m' = FREE deep 1-minute crypto bars (Binance public REST, keyless,
    # crypto tickers only — the doctrine's ideal instrument) so the grid walks the real intraday path
    # and finally MEASURES the scalp. Non-crypto + '1m' falls back to the daily bar.
    scalp_data: str = Field("daily", pattern="^(daily|hourly|1m)$")
    # grid-step timeframe: 'weekly'/'monthly' ATR makes the step WIDER than a daily bar, so each
    # daily bar is sub-step "intraday-like" info within a larger oscillation the grid scalps over
    # several days (the doctrine's "flatten the grid, bigger targets, once-a-day" mode). 'daily' =
    # tightest grid (needs the most intraday resolution we don't have).
    grid_timeframe: str = Field("daily", pattern="^(daily|weekly|monthly)$")
    # re-center the scalp grid to the CURRENT price every N calendar days (realizing stuck legs):
    # the grid follows price and scalps the current range instead of sitting frozen at the year-old
    # strike. 0 = never re-center (legacy frozen grid). ~21 ≈ monthly.
    scalp_recenter_days: int = Field(0, ge=0, le=365)
    # FLAT detector: scalp counter-trend only INSIDE the Bollinger band; suspend new entries on a
    # breakout (trend) and let the straddle run (doctrine: don't fade a galloping market).
    use_bbands: bool = True
    bb_window: int = Field(20, ge=2, le=200)
    bb_k: float = Field(2.0, gt=0, le=5)
    # scalping grid (three-thirds + exponential spacing)
    n_parts: int = Field(5, ge=1, le=50)                         # working parts (modern universal = 5)
    grid_atr_frac: float = Field(0.5, gt=0, le=10)            # first grid step = this × the chosen-timeframe ATR (≈2× daily)
    grid_mult: float = Field(2.0, ge=1.0, le=5)               # exponential spacing between parts
    intraday_frac: float = Field(0.333, gt=0, le=1.0)         # ⅓ rule: scalp limit as a frac of futures
    scalp_efficiency: float = Field(0.5, ge=0, le=1.0)        # range model only: frac of reversed range booked
    max_rt_per_day: float = Field(10.0, ge=0, le=100)         # range model only: cap on round-trips/day
    stuck_penalty: float = Field(0.5, ge=0, le=5)             # range model only: drag from stuck parts
    # "эквивалент монетки" projection: the capture fraction (доля пойманного движения) to ASSUME when
    # projecting profitability onto an asset where we can't run 1m bars. Doctrine ideal >0.5; 0.33 =
    # the user's conservative "catch ~⅓ of the move". The measured capture (from the 1m feed) is the
    # honest anchor; this knob asks "what coverage would I get at this skill level?". Vol cancels, so
    # it transfers across instruments. Display-only — does not change the backtest itself.
    assumed_capture: float = Field(0.333, ge=0, le=2.0)
    # IV surface (same engine as the options tab)
    iv_window: int = Field(20, ge=2, le=500)
    iv_source: str = Field("auto", pattern="^(auto|vix|index|realized|constant)$")
    iv_const: float = Field(0.20, gt=0, le=3)
    skew_beta: float | None = Field(None, ge=-2, le=2)
    use_term_structure: bool = True
    commission_pct: float = Field(0.0, ge=0, le=50)
    slippage_pct: float = Field(0.0, ge=0, le=50)


class HedgedIntradayScanReq(BaseModel):
    """Run the Прикрытый Интрадей backtest across the WHOLE instrument catalog with identical
    params (same knobs as HedgedIntradayReq minus `ticker`/`end` — the scan iterates every
    catalog ticker itself). Sequential by design (Yahoo 429); per-ticker failures are captured."""
    start: str = "2018-01-01"
    atr_period: int = Field(14, ge=2, le=200)
    starting_bank: float = Field(10_000.0, gt=0)
    risk_pct: float = Field(0.20, gt=0, le=1.0)
    dte_days: int = Field(365, ge=7, le=730)
    roll_buffer_days: int = Field(10, ge=1, le=90)
    roll_profit_pct: float = Field(0.0, ge=0, le=100)        # doctrine profit-target roll (0 = off)
    r: float = Field(0.045, ge=-0.05, le=0.5)
    scalp_model: str = Field("grid", pattern="^(grid|capture|analytic|range)$")
    scalp_capture: float = Field(0.20, ge=0, le=3.0)        # capture model: frac of daily range booked
                                                            # (0.20 = grid-calibrated realistic anchor)
    capture_mode: str = Field("preset", pattern="^(flat|preset)$")  # extrapolate: per-CLASS capture
                                                            # preset (rangy↑, trend-prone↓) vs one flat number
    scalp_k: float = Field(0.02, ge=-1.0, le=5.0)           # analytic edge constant (see HedgedIntradayReq)
    grid_timeframe: str = Field("daily", pattern="^(daily|weekly|monthly)$")
    scalp_recenter_days: int = Field(0, ge=0, le=365)
    use_bbands: bool = True
    bb_window: int = Field(20, ge=2, le=200)
    bb_k: float = Field(2.0, gt=0, le=5)
    n_parts: int = Field(5, ge=1, le=50)
    grid_atr_frac: float = Field(0.5, gt=0, le=10)
    grid_mult: float = Field(2.0, ge=1.0, le=5)
    intraday_frac: float = Field(0.333, gt=0, le=1.0)
    scalp_efficiency: float = Field(0.5, ge=0, le=1.0)
    max_rt_per_day: float = Field(10.0, ge=0, le=100)
    assumed_capture: float = Field(0.333, ge=0, le=2.0)      # see HedgedIntradayReq.assumed_capture
    stuck_penalty: float = Field(0.5, ge=0, le=5)
    iv_window: int = Field(20, ge=2, le=500)
    iv_source: str = Field("auto", pattern="^(auto|vix|index|realized|constant)$")
    iv_const: float = Field(0.20, gt=0, le=3)
    skew_beta: float | None = Field(None, ge=-2, le=2)
    use_term_structure: bool = True
    commission_pct: float = Field(0.0, ge=0, le=50)
    slippage_pct: float = Field(0.0, ge=0, le=50)


class PiCoinReq(BaseModel):
    """Tab 13: estimate the net ПИ monthly win-rate `p_net` for an instrument from observable data
    (RV vs IV per period + scalp coverage c net of costs), and the p_net(c) curve / critical c* — to
    decide IN ADVANCE whether it's a >0.55 coin (antimartingale-worthy). `scan` ranks the whole catalog."""
    ticker: str = Field("ETH-USD", min_length=1, max_length=20)
    start: str = "2018-01-01"
    end: str | None = None
    dte_days: int = Field(30, ge=7, le=365)          # period length (monthly≈30, quarterly≈90)
    c: float = Field(0.35, ge=0, le=1.0)             # GROSS scalp coverage of theta (assumption)
    cost_drag: float = Field(0.05, ge=0, le=0.5)     # coverage eaten by commissions/slippage
    # VRP haircut for instruments WITHOUT a real vol index (IV proxied by realized → RV/IV≈1 by
    # construction). We inflate the proxied IV by (1+vrp_proxy) so they aren't falsely flattered vs
    # real-IV names (SPY). Set 0 to see the raw (misleading) RV/IV=1 version.
    vrp_proxy: float = Field(0.15, ge=0, le=1.0)
    iv_window: int = Field(20, ge=2, le=500)
    iv_source: str = Field("auto", pattern="^(auto|vix|index|realized|constant)$")
    iv_const: float = Field(0.20, gt=0, le=3)
    skew_beta: float | None = Field(None, ge=-2, le=2)
    use_term_structure: bool = True
    scan: bool = False                               # rank the whole catalog by p_net at this c


class AntimgOverlayReq(HedgedIntradayReq):
    """Tab 12: run the ПИ (Hedged Intraday) backtest, then lay the antimartingale pyramid-on-wins
    overlay on its per-period P&L — double the position after a winning period, reset after a loss,
    stop at the target streak. `am_period` sets the straddle tenor (monthly≈30d / quarterly≈90d).
    `n_shuffles` runs the shuffle test (does real time-ordering / clustering add alpha vs chance)."""
    target_streak: int = Field(4, ge=1, le=20)
    am_period: str = Field("monthly", pattern="^(monthly|quarterly|asis)$")
    n_shuffles: int = Field(50, ge=0, le=500)
    # source of the period win/loss sequence:
    #   'backtest' = the ПИ daily backtest (pessimistic — free daily data can't see the intraday scalp,
    #                so it under-measures ⇒ low win-rate);
    #   'doctrine' = synthetic 9/3 sequence at Korovin's planned win-rate (the scalp DOES cover theta).
    source: str = Field("doctrine", pattern="^(backtest|doctrine)$")
    d_win_rate: float = Field(0.75, ge=0, le=1.0)    # doctrine: fraction of profitable months (9/12)
    d_win_pct: float = Field(6.0, ge=0, le=100)      # doctrine: a winning month = +this % of deposit (5–7%)
    d_loss_pct: float = Field(5.0, ge=0, le=100)     # doctrine: a losing ("tухлый") month = −this % of deposit
    d_n_periods: int = Field(120, ge=4, le=2000)     # doctrine: how many months to simulate (120 = 10y)
    d_seed: int = Field(7, ge=0, le=10_000_000)


class PureStraddleReq(BaseModel):
    """Pure long-straddle backtest (Tab 10): each period spend risk_pct of the deposit on an ATM
    straddle (call+put), HOLD TO EXPIRATION, settle at intrinsic |S_T−K|, roll. No scalp, no early
    roll. Premium = BS model price (vol surface); expiry payoff uses the real price path."""
    ticker: str = Field("GLD", min_length=1, max_length=20)
    start: str = "2010-01-01"
    end: str | None = None
    risk_pct: float = Field(0.01, gt=0, le=1.0)              # % of deposit spent on the straddle per period
    dte_days: int = Field(30, ge=1, le=730)                  # straddle tenor = holding period to expiry
    starting_bank: float = Field(10_000.0, gt=0)
    compounding: bool = True                                 # size bet to current bank (vs starting bank)
    # 'expiry'   = each expiration is its own win/loss (original).
    # 'coinflip' = a TRIAL rolls to expiry until cumulative P&L hits +R (win) or −R (loss), R=risk_pct×bank
    #              — a fixed risk/reward coin flip; loss capped at −R, win books actual (can overshoot).
    resolution: str = Field("expiry", pattern="^(expiry|coinflip)$")
    # coinflip only: HORIZON — close a trial at its actual cum if it hasn't hit ±R within this many
    # rolls (a straddle rarely doubles/zeroes in one expiry, so without this a losing trial drags on).
    max_rolls: int = Field(12, ge=1, le=120)
    # coinflip only: take profit at +R (clean ±R coin flip, every full win = +R) vs let winners run
    # (a win books the actual cum, ≥ +R, overshoots = long-option convexity).
    take_profit: bool = True
    r: float = Field(0.045, ge=-0.05, le=0.5)
    # IV surface (same engine as the options/hedged tabs)
    iv_window: int = Field(20, ge=2, le=500)
    iv_source: str = Field("auto", pattern="^(auto|vix|index|realized|constant)$")
    iv_const: float = Field(0.20, gt=0, le=3)
    skew_beta: float | None = Field(None, ge=-2, le=2)
    use_term_structure: bool = True
    commission_pct: float = Field(0.0, ge=0, le=50)          # % of premium/payoff per leg
    slippage_pct: float = Field(0.0, ge=0, le=50)


class PiSimReq(BaseModel):
    """Tab 14 — «Симуляция в деньгах»: ONE ПИ construction held for ONE period on a real instrument
    over a real past window, every number exposed (what to buy, straddle cost, grid prices, scalp $).
    Works for ANY catalog instrument; for crypto the scalp is MEASURED on the free 1m feed, otherwise
    it's the labelled daily-range capture SCENARIO. Premium = BS price off the vol surface."""
    ticker: str = Field("ETH-USD", min_length=1, max_length=20)
    start: str = "2025-08-01"                                # any past date with data
    deposit: float = Field(10_000.0, gt=0)
    dte_days: int = Field(30, ge=7, le=365)                  # period = straddle tenor (held to expiry)
    risk_pct: float = Field(0.10, gt=0, le=1.0)              # % of deposit spent on premium (= max loss)
    n_parts: int = Field(5, ge=1, le=20)                     # working scalp parts (modern universal = 5)
    # first grid step = this × daily ATR. INTRADAY-scaled (≈0.03–0.08) so the 1m measurement gets the
    # doctrine's ~200–250 round-trips/mo (INVARIANT #7); a wide ×0.5 daily-ATR grid barely trades on 1m.
    grid_atr_frac: float = Field(0.05, gt=0, le=10)
    grid_mult: float = Field(1.8, ge=1.0, le=5)              # exponential spacing between parts
    intraday_frac: float = Field(0.333, gt=0, le=1.0)        # ⅓ rule: scalp limit as a frac of calls
    capture: float = Field(0.20, ge=0, le=2.0)               # OPTIMISTIC ceiling: frac of daily range booked
    # REALISTIC anchor — coverage (scalp ÷ theta) is the vol-invariant profitability primitive (INV #7).
    # Default 0.15 = the conservative calibration (ETH-1m booked ~0.16 + guru's 10–15%-of-premium/mo);
    # уверенный флэт can reach ~1.0. The non-crypto headline uses coverage_anchor × theta.
    coverage_anchor: float = Field(0.15, ge=0, le=2.0)
    # ADAPTIVE CHOP-SCALP model (the trader's manual range-adjustment know-how) — the realistic headline.
    # scalp ≈ n_days·f_chop·trades_per_day·(scalp_eff·flat_frac·daily_range)·part_lots ; vol-invariant.
    f_chop: float = Field(0.667, ge=0, le=1.0)               # fraction of time in chop (stat. ~⅔)
    trades_per_day: float = Field(10.0, ge=0, le=200)        # target round-trips/day in chop
    scalp_eff: float = Field(0.5, ge=0, le=1.0)              # fraction of each swing booked (trader skill)
    flat_frac: float = Field(0.25, ge=0, le=2.0)             # local flat = this × daily range (adapts)
    use_1m: bool = True                                      # crypto: measure scalp on the real 1m feed
    # scan mode: roll NON-overlapping windows over [scan_start, scan_end] for the WHOLE catalog and rank
    # by edge (straddle core is real; scalp = anchor). 1m is NOT used in scan (too slow) — anchor only.
    scan: bool = False
    scan_start: str = "2019-01-01"
    scan_end: str | None = None
    atr_period: int = Field(14, ge=2, le=200)
    r: float = Field(0.045, ge=-0.05, le=0.5)
    iv_window: int = Field(20, ge=2, le=500)
    iv_source: str = Field("auto", pattern="^(auto|vix|index|realized|constant)$")
    iv_const: float = Field(0.20, gt=0, le=3)
    skew_beta: float | None = Field(None, ge=-2, le=2)
    use_term_structure: bool = True


class FromSignalsReq(BaseModel):
    strategy_id: str | None = None
    base_bet: float = Field(100.0, gt=0)
    target_streak: int = Field(10, ge=1, le=settings.max_target_streak)
    commission_pct: float = Field(0.035, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    slippage_pct: float = Field(0.01, ge=0, le=50)  # % of notional per fill (×2 round-trip)
    starting_bank: float = Field(10_000.0)
    cap_mult: float | None = Field(8.0, gt=0)   # cap antimartingale bet doubling (see BacktestReq)
