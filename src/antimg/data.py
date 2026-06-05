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
    if (df is None or df.empty) and _to_binance_symbol(ticker):
        # crypto: free Binance daily klines — keeps the crypto path independent of Yahoo (429-prone).
        # Pull FULL daily history (start=None) regardless of the requested window so a short first
        # request can't poison the cache; fetch() caches the full df and _slice serves the window.
        try:
            df = fetch_intraday_crypto(ticker, "1d", start=None, end=end, use_cache=False)
        except Exception:
            df = None
    if df is None or df.empty:
        raise RuntimeError(f"No data for {ticker!r} from yfinance, stooq, or Binance "
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


def fetch_intraday(ticker: str, interval: str = "60m", start: str | None = None,
                   end: str | None = None, use_cache: bool = True) -> pd.DataFrame:
    """Intraday OHLCV bars (DatetimeIndex) for the scalping overlay.

    yfinance limits: 1m≈7d, ≤90m≈60d, 60m≈730d. So for a multi-month/2-year backtest use
    `interval="60m"` (hourly) — the finest with ~2y of history. Cached per (ticker, interval)
    like the daily cache; raises on total failure so the caller can fall back to daily bars.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = ticker.replace("/", "_").replace("=", "-").replace("^", "idx_")
    cp = CACHE_DIR / f"{safe}__{interval}.pkl"
    if use_cache and cp.exists():
        df = pd.read_pickle(cp)
        if not df.empty:
            return _slice(df, start or "1990-01-01", end)
    try:
        import yfinance as yf
        df = yf.download(ticker, start=start, end=end, interval=interval,
                         auto_adjust=False, progress=False, threads=False)
    except Exception as ex:
        raise RuntimeError(f"intraday fetch failed for {ticker!r} @ {interval}: {ex}")
    if df is None or df.empty:
        raise RuntimeError(f"no intraday data for {ticker!r} @ {interval} "
                           f"(yfinance limits history: 60m≈730d, 1m≈7d)")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)             # tz-naive so _slice/groupby-by-date work
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
    if use_cache:
        df.to_pickle(cp)
    return _slice(df, start or "1990-01-01", end)


# --------------------------------------------------------------------------- #
# Free deep INTRADAY crypto bars — Binance public REST (keyless, no extra deps).
# Best free 1-min/tick source for the ПИ scalp; crypto = the doctrine's *ideal*
# instrument (high vol + divisible lots). See the /tradinglivedata skill.
# --------------------------------------------------------------------------- #
_BINANCE_HOSTS = (
    "https://data-api.binance.vision",   # public market-data mirror, no auth, least geo-blocked
    "https://api.binance.com",
)
_BINANCE_SYMBOL_OVERRIDES = {
    "BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT", "SOL-USD": "SOLUSDT",
}
_BINANCE_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000,
}


def _to_binance_symbol(ticker: str) -> str | None:
    """Map an antimg/yfinance crypto ticker to a Binance USDT spot symbol.

    BTC-USD/ETH-USD/SOL-USD → BTCUSDT/ETHUSDT/SOLUSDT. Returns None for anything that
    isn't a crypto pair we can serve from Binance (futures `=F`, FX `=X`, indices `^`, equities).
    """
    t = ticker.upper().strip()
    if t in _BINANCE_SYMBOL_OVERRIDES:
        return _BINANCE_SYMBOL_OVERRIDES[t]
    if any(c in t for c in ("=", "^", ".")):
        return None                                       # non-crypto yfinance tickers
    base = t.replace("/", "-")
    for sep in ("-USDT", "-USD", "-USDC"):
        if base.endswith(sep):
            return base[: -len(sep)].replace("-", "") + "USDT"
    if base.endswith("USDT"):
        return base
    return None


def _parse_binance_klines(rows: list) -> pd.DataFrame:
    """Binance /klines array → OHLCV DataFrame (tz-naive UTC index on each bar's OPEN time)."""
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if not rows:
        return pd.DataFrame(columns=cols)
    idx = pd.to_datetime([r[0] for r in rows], unit="ms")          # open time, UTC, tz-naive
    df = pd.DataFrame({c: [float(r[i]) for r in rows]
                       for i, c in enumerate(cols, start=1)}, index=idx)
    return df[~df.index.duplicated(keep="last")].sort_index()


def fetch_intraday_crypto(ticker: str, interval: str = "1m", start: str | None = None,
                          end: str | None = None, use_cache: bool = True,
                          max_pages: int = 4000) -> pd.DataFrame:
    """FREE deep intraday crypto bars from Binance public REST (keyless, stdlib only).

    Maps a crypto ticker (BTC-USD/ETH-USD/SOL-USD) to a Binance USDT pair and paginates
    `/api/v3/klines` (1000 bars/request) over [start, end]. tz-naive UTC index so the engine's
    groupby-by-date path works. Cached per (symbol, interval) — delete the cache file to extend
    history. Raises on an unmapped ticker or total network failure so the caller can fall back
    to the daily bar. NOTE: 1-min over a multi-year window is ~1 request/1000 bars (slow first
    pull, then cached); pick a coarser `interval` for long windows.
    """
    import http.client
    import json as _json
    sym = _to_binance_symbol(ticker)
    if sym is None:
        raise RuntimeError(f"{ticker!r} is not a Binance-servable crypto pair "
                           f"(the free 1m feed is crypto-only; use scalp_data='hourly' otherwise)")
    step = _BINANCE_INTERVAL_MS.get(interval)
    if step is None:
        raise RuntimeError(f"unsupported crypto interval {interval!r}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = CACHE_DIR / f"binance_{sym}__{interval}.pkl"
    if use_cache and cp.exists():
        df = pd.read_pickle(cp)
        if not df.empty:
            return _slice(df, start or "1990-01-01", end)

    start_ms = int(pd.Timestamp(start or "2017-08-01").timestamp() * 1000)
    end_ms = (int(pd.Timestamp(end).timestamp() * 1000) if end
              else int(pd.Timestamp.now("UTC").timestamp() * 1000))

    # Reuse ONE keep-alive HTTPS connection across all pages — a fresh urlopen() per page pays a
    # DNS lookup + TLS handshake every time (~4s/req from a container → minutes for a 60d 1m pull);
    # keep-alive drops that to ~0.3s/req. Rotate to the next host (reconnecting) on any error.
    hosts = [h.split("://", 1)[-1] for h in _BINANCE_HOSTS]
    state = {"conn": None, "host": None, "err": None}

    def _get(path):
        order = ([state["host"]] if state["host"] else []) + [h for h in hosts if h != state["host"]]
        for host in order:
            try:
                if state["conn"] is None or state["host"] != host:
                    if state["conn"]:
                        state["conn"].close()
                    state["conn"] = http.client.HTTPSConnection(host, timeout=30)
                    state["host"] = host
                state["conn"].request("GET", path, headers={"User-Agent": "antimg/1.0"})
                resp = state["conn"].getresponse()
                body = resp.read()                        # must fully read to reuse the connection
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return _json.loads(body.decode("utf-8"))
            except Exception as ex:                       # drop the conn, try the next host
                state["err"] = ex
                if state["conn"]:
                    state["conn"].close()
                state["conn"] = None
                state["host"] = None
        return None

    rows: list = []
    cur, pages = start_ms, 0
    while cur < end_ms and pages < max_pages:
        path = (f"/api/v3/klines?symbol={sym}&interval={interval}"
                f"&startTime={cur}&endTime={end_ms}&limit=1000")
        batch = _get(path)
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + step                         # next page starts one step past last open
        pages += 1
        if len(batch) < 1000:                             # reached the live tip
            break
    if state["conn"]:
        state["conn"].close()

    if not rows:
        raise RuntimeError(f"no Binance data for {sym} @ {interval} "
                           f"(geo-blocked? network? last error: {state['err']})")
    df = _parse_binance_klines(rows)
    if use_cache:
        df.to_pickle(cp)
    return _slice(df, start or "1990-01-01", end)


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLC to weekly (week ending Friday)."""
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    wk = df.resample("W-FRI").agg(agg).dropna(how="any")
    return wk


def monthly(df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLC to monthly (calendar month-end)."""
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    return df.resample("ME").agg(agg).dropna(how="any")


def atr_on_timeframe(daily: pd.DataFrame, timeframe: str, period: int = 14) -> pd.Series:
    """ATR computed on a coarser bar (daily/weekly/monthly) and aligned back to the DAILY index.

    Used to size a scalping GRID STEP off a longer-timeframe range so that, on a daily-bar
    backtest, each daily bar is sub-step ("intraday-like") information within the larger
    weekly/monthly oscillation. The coarse ATR is SHIFTED one bar before reindex (use the last
    COMPLETED week/month) so there is no look-ahead, then forward-filled onto daily dates.
    """
    if timeframe == "weekly":
        coarse = weekly(daily)
    elif timeframe == "monthly":
        coarse = monthly(daily)
    else:
        return atr(daily, period)                       # daily: native, no realignment
    a = atr(coarse, period).shift(1)                    # last completed coarse bar → no look-ahead
    return a.reindex(daily.index, method="ffill")


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
