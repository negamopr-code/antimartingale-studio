"""Market data: fetch (yfinance + stooq fallback), pickle cache, ATR, weekly resample,
realized volatility.

Daily is the finest interval with full free history (yfinance: 1m~7-30d, intraday<=730d,
1d=full). Weekly bars are a resample of daily — one pull serves both the weekly entry
grid (Tab 2/3) and the daily intra-week resolution.
"""
from __future__ import annotations

import io
import os
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

CACHE_DIR = Path(os.environ.get("ANTIMG_CACHE", "/workspace/.cache"))
TRADING_DAYS = 252


def _cache_path(ticker: str) -> Path:
    safe = ticker.replace("/", "_").replace("=", "-").replace("^", "idx_")
    return CACHE_DIR / f"{safe}.pkl"   # pickle: no parquet engine dependency


def fetch(ticker: str, start: str = "1990-01-01", end: str | None = None,
          use_cache: bool = True, refresh: bool = False) -> pd.DataFrame:
    """Daily OHLCV with a DatetimeIndex and columns Open/High/Low/Close/Volume.

    Tries the parquet cache, then yfinance, then stooq (daily CSV). Raises on total failure.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = _cache_path(ticker)
    if use_cache and not refresh and cp.exists():
        df = pd.read_pickle(cp)
        if not df.empty:
            return _slice(df, start, end)

    df = _fetch_yfinance(ticker, start, end)
    if df is None or df.empty:
        df = _fetch_stooq(ticker)
    if df is None or df.empty:
        raise RuntimeError(f"No data for {ticker!r} from yfinance or stooq "
                           f"(network blocked? Yahoo 429? unknown ticker?)")

    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
    if use_cache:
        df.to_pickle(cp)
    return _slice(df, start, end)


def _slice(df: pd.DataFrame, start: str, end: str | None) -> pd.DataFrame:
    out = df.loc[df.index >= pd.Timestamp(start)]
    if end:
        out = out.loc[out.index <= pd.Timestamp(end)]
    return out


def _fetch_yfinance(ticker: str, start: str, end: str | None) -> pd.DataFrame | None:
    try:
        import yfinance as yf
        df = yf.download(ticker, start=start, end=end, interval="1d",
                         auto_adjust=False, progress=False, threads=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):       # yfinance>=0.2 returns MultiIndex
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index)
        return df
    except Exception:
        return None


def _fetch_stooq(ticker: str) -> pd.DataFrame | None:
    """Stooq daily CSV fallback. Maps a few common US tickers to stooq symbols."""
    sym = ticker.lower()
    if sym.startswith("^"):
        return None
    if "=" in sym or "-" in sym:
        return None
    if "." not in sym:
        sym = f"{sym}.us"
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            raw = r.read().decode("utf-8")
        df = pd.read_csv(io.StringIO(raw))
        if "Date" not in df.columns or df.empty:
            return None
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        df["Volume"] = df.get("Volume", pd.Series(index=df.index, dtype=float))
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception:
        return None


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLC to weekly (week ending Friday)."""
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    wk = df.resample("W-FRI").agg(agg).dropna(how="any")
    return wk


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR on whatever timeframe `df` is in."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    # Wilder smoothing == EMA with alpha = 1/period
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def realized_vol(close: pd.Series, window: int = 20, annualize: int = TRADING_DAYS) -> pd.Series:
    """Annualized realized volatility from rolling std of daily log returns."""
    logret = np.log(close / close.shift(1))
    return logret.rolling(window).std() * np.sqrt(annualize)
