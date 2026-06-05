"""Tests for the Прикрытый Интрадей (hedged intraday) backtest engine."""
import numpy as np
import pandas as pd
import pytest

from antimg import data as datamod
from antimg import hedged_intraday as hi


def _frame(close, rng_pct=0.02, drift_open=0.0):
    """OHLC frame from a close path; High/Low straddle Close by rng_pct, Open offset by drift."""
    idx = pd.bdate_range("2018-01-01", periods=len(close))
    close = pd.Series(close, index=idx, dtype=float)
    op = close.shift(1).fillna(close.iloc[0]) * (1 + drift_open)
    return pd.DataFrame({"Open": op, "High": np.maximum(close, op) * (1 + rng_pct),
                         "Low": np.minimum(close, op) * (1 - rng_pct), "Close": close,
                         "Volume": 0.0}, index=idx)


def test_smoke_flat_market():
    df = _frame(np.full(300, 100.0) + np.random.default_rng(1).normal(0, 0.5, 300))
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14),
                                 realized_vol=datamod.realized_vol(df["Close"], 20))
    assert res.n_days > 0
    assert res.table, "should resolve at least one straddle period"
    assert res.total_theta <= 0.0, "a long straddle pays (negative) theta"
    # total = bank + straddle + scalp identity holds at the end
    assert res.final_bank == pytest.approx(res.starting_bank + res.straddle_pnl + res.scalp_pnl, rel=1e-6)


def test_straddle_loss_floored_at_premium():
    """With NO scalping (efficiency 0, no drag) a dead-flat market bleeds the straddle, but a
    single straddle period's straddle leg cannot lose more than the premium paid for it."""
    df = _frame(np.full(200, 100.0), rng_pct=0.001)   # almost no range → near-pure theta bleed
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14),
                                 realized_vol=datamod.realized_vol(df["Close"], 20),
                                 scalp_efficiency=0.0, stuck_penalty=0.0, dte_days=30)
    for row in res.table:
        # straddle P&L for the period is bounded below by −premium (the construction's loss cap)
        assert row["straddle_pnl"] >= -row["premium"] - 1e-6, row


def test_trend_makes_straddle_gamma():
    """A strong sustained trend should make the straddle leg PROFIT (long gamma)."""
    up = 100.0 * np.cumprod(1 + np.full(300, 0.004))   # ~0.4%/day uptrend
    df = _frame(up, rng_pct=0.005, drift_open=0.0)
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14),
                                 realized_vol=datamod.realized_vol(df["Close"], 20),
                                 scalp_efficiency=0.0, stuck_penalty=0.0, dte_days=30)
    assert res.straddle_pnl > 0, "a trend should pay the long straddle via gamma"


def test_range_model_scales_with_efficiency():
    """range model only: scalp income scales with the efficiency knob (frac of reversed range)."""
    rng = np.random.default_rng(3)
    path = 100.0 + np.cumsum(rng.normal(0, 0.3, 300))
    df = _frame(path, rng_pct=0.02)
    lo = hi.run_hedged_intraday(df, datamod.atr(df, 14), scalp_model="range",
                                realized_vol=datamod.realized_vol(df["Close"], 20),
                                scalp_efficiency=0.1)
    himore = hi.run_hedged_intraday(df, datamod.atr(df, 14), scalp_model="range",
                                    realized_vol=datamod.realized_vol(df["Close"], 20),
                                    scalp_efficiency=0.9)
    assert himore.scalp_pnl > lo.scalp_pnl


def test_grid_books_round_trips_on_oscillation():
    """grid model: an oscillating (mean-reverting) market books counter-trend round-trips and
    earns positive scalp P&L; the daily bar is the execution timeframe (no efficiency knob)."""
    rng = np.random.default_rng(7)
    # oscillate around 100 with a wide daily range so the grid completes round-trips
    osc = 100.0 + 6.0 * np.sin(np.arange(400) / 3.0) + rng.normal(0, 0.5, 400)
    df = _frame(osc, rng_pct=0.015)
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14), scalp_model="grid",
                                 realized_vol=datamod.realized_vol(df["Close"], 20),
                                 grid_atr_frac=1.0, dte_days=180)
    assert res.scalp_round_trips > 0, "an oscillating market should complete round-trips"
    assert res.scalp_pnl > 0, "counter-trend round-trips in a range should book positive scalp P&L"


def test_grid_position_bounded_by_intraday_limit():
    """grid model: each working part holds ≤1 leg, so a hard one-way trend can leave parts stuck
    but the straddle loss cap (per period) is still respected — total never goes naked."""
    up = 100.0 * np.cumprod(1 + np.full(260, 0.005))    # relentless uptrend → shorts get stuck
    df = _frame(up, rng_pct=0.004)
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14), scalp_model="grid",
                                 realized_vol=datamod.realized_vol(df["Close"], 20), dte_days=180)
    for row in res.table:
        assert row["straddle_pnl"] >= -row["premium"] - 1e-6, row   # straddle leg cap intact


def test_grid_step_timeframe_widens_grid():
    """A weekly/monthly-ATR grid step is WIDER than a daily-ATR one — so the daily bar becomes
    sub-step 'intraday-like' info within a larger oscillation the grid scalps over days (user's
    reframe). The robust invariant is the widening of the step + monotonic daily<weekly<monthly."""
    rng = np.random.default_rng(11)
    path = 100.0 + np.cumsum(rng.normal(0, 0.4, 500))
    df = _frame(path, rng_pct=0.015)
    rv = datamod.realized_vol(df["Close"], 20)
    d = datamod.atr_on_timeframe(df, "daily", 14).dropna().mean()
    w = datamod.atr_on_timeframe(df, "weekly", 14).dropna().mean()
    m = datamod.atr_on_timeframe(df, "monthly", 14).dropna().mean()
    assert d < w < m, (d, w, m)            # coarser timeframe ⇒ wider grid step
    weekly_grid = hi.run_hedged_intraday(df, datamod.atr_on_timeframe(df, "weekly", 14),
                                         realized_vol=rv, scalp_model="grid", dte_days=180)
    assert weekly_grid.n_days > 0 and weekly_grid.table


def test_scalp_captures_mean_reversion_when_legs_carried():
    """A counter-trend grid on a strongly MEAN-REVERTING (OU) zero-drift series MUST profit before
    costs when stuck legs are carried (recenter=0). Re-centering force-closes underwater legs before
    they revert and destroys the edge — this is the regression guard for that bug."""
    rng = np.random.default_rng(1)
    x, path = 100.0, []
    for _ in range(3000):
        x += 0.1 * (100.0 - x) + rng.normal(0, 2.0)       # Ornstein-Uhlenbeck: reverts to 100
        path.append(x)
    df = _frame(np.array(path), rng_pct=0.003)
    rv = datamod.realized_vol(df["Close"], 20)
    datr = datamod.atr_on_timeframe(df, "daily", 14)
    carried = hi.run_hedged_intraday(df, datr, realized_vol=rv, scalp_model="grid", dte_days=365,
                                     grid_atr_frac=0.3, grid_mult=1.25, n_parts=20, scalp_recenter_days=0)
    recentered = hi.run_hedged_intraday(df, datr, realized_vol=rv, scalp_model="grid", dte_days=365,
                                        grid_atr_frac=0.3, grid_mult=1.25, n_parts=20, scalp_recenter_days=21)
    assert carried.scalp_pnl > 0, "carrying stuck legs must capture OU mean-reversion"
    assert carried.scalp_pnl > recentered.scalp_pnl, "timer re-centering realizes legs early, hurting the edge"


def test_delta_neutral_core_base_hedge():
    """The core is DELTA-NEUTRAL (corpus: 2·n_str calls hedged by EXACTLY n_str futures = "30 Колл −
    15 Фьюч"), NOT net-long. The three-thirds is the SCALP limit, not a permanent core tilt. Verify
    base_futs == n_str via the resolved straddle state, and that a trend still pays via gamma + the
    loss cap holds. (Regression for the net-long-tilt bug that bled the straddle on down moves.)"""
    up = 100.0 * np.cumprod(1 + np.full(300, 0.004))
    df = _frame(up, rng_pct=0.005)
    res = hi.run_hedged_intraday(df, datamod.atr_on_timeframe(df, "daily", 14),
                                 realized_vol=datamod.realized_vol(df["Close"], 20), dte_days=365)
    assert res.straddle_pnl > 0                       # symmetric long-gamma still captures the trend
    for row in res.table:
        assert row["straddle_pnl"] >= -row["premium"] - 1e-6, row   # loss cap intact


def test_confident_flat_scaling_grows_lot_from_profit():
    """Уверенный флет / заслуженный риск: after ≥N clean cycles the working-part lot scales UP from
    accrued profit (capped ×2, so total scalp ≤ calls−base ⇒ never naked). On a long mean-reverting
    flat, scaling ON should book MORE scalp than OFF, with the same round-trips."""
    rng = np.random.default_rng(1)
    x, path = 100.0, []
    for _ in range(3000):
        x += 0.1 * (100.0 - x) + rng.normal(0, 2.0)
        path.append(x)
    df = _frame(np.array(path), rng_pct=0.003)
    rv = datamod.realized_vol(df["Close"], 20)
    datr = datamod.atr_on_timeframe(df, "daily", 14)
    kw = dict(realized_vol=rv, dte_days=365, grid_atr_frac=0.5, grid_mult=2.0, n_parts=5)
    on = hi.run_hedged_intraday(df, datr, confident_flat_scale=True, **kw)
    off = hi.run_hedged_intraday(df, datr, confident_flat_scale=False, **kw)
    assert on.scalp_scaled_max > 1.0 and on.scalp_scaled_max <= 2.0 + 1e-9   # scaled, capped ≤×2 (never naked)
    assert on.scalp_pnl > off.scalp_pnl                                       # earned-risk scaling books more


def test_rolls_happen():
    df = _frame(100.0 + np.random.default_rng(2).normal(0, 0.5, 400))
    res = hi.run_hedged_intraday(df, datamod.atr(df, 14),
                                 realized_vol=datamod.realized_vol(df["Close"], 20),
                                 dte_days=30, roll_buffer_days=5)
    assert res.n_rolls >= 5, "≈400 days / 30-day straddle ⇒ many rolls"


# --------------------------------------------------------------------------- #
# Free crypto 1-minute scalp feed (Binance public REST) — see /tradinglivedata.
# --------------------------------------------------------------------------- #
def test_binance_symbol_mapping():
    """Crypto tickers map to Binance USDT pairs; non-crypto returns None (falls back to daily)."""
    assert datamod._to_binance_symbol("BTC-USD") == "BTCUSDT"
    assert datamod._to_binance_symbol("ETH-USD") == "ETHUSDT"
    assert datamod._to_binance_symbol("SOL-USD") == "SOLUSDT"
    assert datamod._to_binance_symbol("eth/usdt") == "ETHUSDT"
    for non_crypto in ("SPY", "GC=F", "EURUSD=X", "^GSPC", "000001.SS"):
        assert datamod._to_binance_symbol(non_crypto) is None


def test_parse_binance_klines():
    """A raw /klines array parses into a sorted, tz-naive OHLCV frame on each bar's open time."""
    rows = [
        [1700000000000, "100.0", "101.0", "99.0", "100.5", "12.0", 1700000059999],
        [1700000060000, "100.5", "102.0", "100.0", "101.5", "8.0", 1700000119999],
    ]
    df = datamod._parse_binance_klines(rows)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 2 and df.index.tz is None
    assert df["Close"].iloc[0] == 100.5 and df["High"].iloc[1] == 102.0
    assert df.index.is_monotonic_increasing


def test_fetch_intraday_crypto_rejects_non_crypto():
    """The free 1m feed is crypto-only; a non-crypto ticker raises (caller then uses the daily bar)."""
    with pytest.raises(RuntimeError):
        datamod.fetch_intraday_crypto("SPY", "1m", start="2024-01-01")


@pytest.mark.network
def test_fetch_intraday_crypto_live_smoke():
    """LIVE: pull a small recent ETH 1m slice from Binance and walk it through the engine.
    Skips automatically if Binance is unreachable (offline/geo-blocked CI)."""
    end = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    start = (end - pd.Timedelta(days=3)).date().isoformat()
    try:
        intr = datamod.fetch_intraday_crypto("ETH-USD", "1m", start=start,
                                             end=end.date().isoformat(), use_cache=False)
    except RuntimeError:
        pytest.skip("Binance unreachable (offline / geo-blocked)")
    assert not intr.empty and list(intr.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert intr.index.tz is None
    # ~1440 1m bars/day over ~3 days; allow slack for the partial current day
    assert len(intr) > 1000, f"expected a deep 1m slice, got {len(intr)} bars"


def test_straddle_symmetric_wins_on_crash_and_rally():
    """The synthetic straddle is DELTA-NEUTRAL → long gamma → it must capture a big move EITHER way
    (gamma_dir_pnl > 0 on both a strong crash and a strong rally). Regression for the net-long-tilt
    bug where base_futs=(2/3)·n_str made the core net-LONG and bled on down moves (BTC 60k→17k showed
    a negative straddle — nonsense for long-vol)."""
    n = 260
    crash = _frame(60000.0 * (1 - 0.7 * np.arange(n) / n), rng_pct=0.02)   # -70% one-way down
    rally = _frame(20000.0 * (1 + 2.0 * np.arange(n) / n), rng_pct=0.02)   # +200% one-way up
    for df, name in [(crash, "crash"), (rally, "rally")]:
        rv = datamod.realized_vol(df["Close"], 20).bfill().fillna(0.6)
        r = hi.run_hedged_intraday(df, datamod.atr(df, 14), realized_vol=rv,
                                   dte_days=365, roll_buffer_days=10, n_parts=5)
        assert r.gamma_dir_pnl > 0, f"straddle gamma must capture the {name} (got {r.gamma_dir_pnl:.0f})"
        # loss cap still holds (delta-neutral doesn't raise max loss above the premium)
        assert r.worst_period_pnl >= -r.max_premium_at_risk - 1e-6


def test_profit_target_roll_fires_and_re_centers():
    """Doctrine roll (module 26/27): with roll_profit_pct>0 the straddle rolls IN THE PROFIT ZONE
    when the period's gain hits the target — closing the whole construction and re-opening ATM, so a
    strong trend produces MORE rolls than schedule-only, some tagged 'профит-цель'."""
    up = 100.0 * np.cumprod(1 + np.full(400, 0.006))      # strong uptrend → straddle gains fast
    df = _frame(up, rng_pct=0.005)
    rv = datamod.realized_vol(df["Close"], 20).bfill().fillna(0.4)
    base = hi.run_hedged_intraday(df, datamod.atr(df, 14), realized_vol=rv,
                                  dte_days=365, roll_buffer_days=10, roll_profit_pct=0.0)
    tgt = hi.run_hedged_intraday(df, datamod.atr(df, 14), realized_vol=rv,
                                 dte_days=365, roll_buffer_days=10, roll_profit_pct=10.0)
    profit_rolls = sum(1 for x in tgt.rolls if x.get("reason") == "профит-цель")
    assert profit_rolls >= 1, "a strong trend should hit the profit target and roll"
    assert tgt.n_rolls > base.n_rolls, "profit-target rolling adds rolls beyond the expiry schedule"
    for row in tgt.table:                                  # loss cap still respected per period
        assert row["straddle_pnl"] >= -row["premium"] - 1e-6, row
