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

    # ALWAYS download deep history into the cache (ignore the requested `start` for the download) so a
    # caller passing a recent start can't poison the cache with a short window; `_slice` serves the window.
    dl_start = "1990-01-01"
    df = _fetch_yfinance(ticker, dl_start, end)
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


def _slice_series(s: pd.Series, start: str, end: str | None) -> pd.Series:
    out = s.loc[s.index >= pd.Timestamp(start)]
    if end:
        out = out.loc[out.index <= pd.Timestamp(end)]
    return out


def fetch_dvol(currency: str, start: str = "2021-01-01", end: str | None = None,
               refresh: bool = False) -> pd.Series:
    """Deribit **DVOL** — the real 30-day implied-vol index for BTC/ETH (the "crypto VIX"). Returns a
    daily Series as a FRACTION (e.g. 0.55 = 55% IV), so a long-vol model can use a TRUE option premium
    for crypto instead of proxying IV by realized. Public API, no key. Cached; raises on total failure.
    History starts ~2021-03 (BTC) / later (ETH) — use start≥2021 for full coverage."""
    import json
    currency = currency.upper()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = _cache_path(f"DVOL_{currency}")
    if not refresh and cp.exists():
        s = pd.read_pickle(cp)
        if isinstance(s, pd.Series) and not s.empty:
            return _slice_series(s, start, end)

    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "antimg/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    end_ts = pd.Timestamp(end) if end else pd.Timestamp.utcnow().tz_localize(None)
    cur = pd.Timestamp("2021-01-01")                      # DVOL inception-ish; page forward to today
    rows = []
    while cur < end_ts:
        chunk_end = min(cur + pd.Timedelta(days=300), end_ts)
        url = ("https://www.deribit.com/api/v2/public/get_volatility_index_data"
               f"?currency={currency}&start_timestamp={int(cur.timestamp() * 1000)}"
               f"&end_timestamp={int(chunk_end.timestamp() * 1000)}&resolution=1D")
        try:
            rows.extend(_get(url).get("result", {}).get("data", []) or [])
        except Exception:
            pass
        cur = chunk_end + pd.Timedelta(days=1)
    if not rows:
        raise RuntimeError(f"Deribit DVOL fetch failed for {currency} (network blocked?)")
    arr = {}
    for row in rows:                                      # [ts_ms, open, high, low, close]
        arr[pd.Timestamp(row[0], unit="ms").normalize()] = float(row[4]) / 100.0
    s = pd.Series(arr).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s.to_pickle(cp)
    return _slice_series(s, start, end)


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


def _binance_rest_rows(sym: str, interval: str, start_ms: int, end_ms: int, max_pages: int = 4000) -> list:
    """Paginate Binance /api/v3/klines over [start_ms, end_ms] with ONE keep-alive HTTPS connection
    (a fresh urlopen per page pays DNS+TLS handshake each time → minutes; keep-alive ≈0.3s/req)."""
    import http.client
    import json as _json
    step = _BINANCE_INTERVAL_MS.get(interval, 60_000)
    hosts = [h.split("://", 1)[-1] for h in _BINANCE_HOSTS]
    state = {"conn": None, "host": None}

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
                body = resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return _json.loads(body.decode("utf-8"))
            except Exception:
                if state["conn"]:
                    state["conn"].close()
                state["conn"] = None
                state["host"] = None
        return None

    rows, cur, pages = [], start_ms, 0
    while cur < end_ms and pages < max_pages:
        batch = _get(f"/api/v3/klines?symbol={sym}&interval={interval}"
                     f"&startTime={cur}&endTime={end_ms}&limit=1000")
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + step
        pages += 1
        if len(batch) < 1000:
            break
    if state["conn"]:
        state["conn"].close()
    return rows


_BINANCE_BULK_HOST = "data.binance.vision"


def _binance_monthly_rows(sym: str, interval: str, year: int, month: int) -> "list | None":
    """Download ONE monthly kline dump (data.binance.vision) → rows, or None if not published yet
    (404). One ~2-3 MB zip per month = ~30× fewer requests than REST for deep history. Handles the
    2025 timestamp-unit change (some dumps switched open-time from ms to µs)."""
    import http.client
    import zipfile, io, csv
    path = f"/data/spot/monthly/klines/{sym}/{interval}/{sym}-{interval}-{year:04d}-{month:02d}.zip"
    try:
        conn = http.client.HTTPSConnection(_BINANCE_BULK_HOST, timeout=60)
        conn.request("GET", path, headers={"User-Agent": "antimg/1.0"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status != 200:
            return None
        zf = zipfile.ZipFile(io.BytesIO(body))
        rows = []
        with zf.open(zf.namelist()[0]) as fh:
            for rec in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                if not rec or not rec[0].lstrip("-").isdigit():
                    continue                               # skip a header line if present
                t = int(rec[0])
                if t > 1e14:                               # µs (post-2025 dumps) → ms
                    t //= 1000
                rows.append([t, rec[1], rec[2], rec[3], rec[4], rec[5]])
        return rows
    except Exception:
        return None


def _binance_1m_rows(sym: str, start_ms: int, end_ms: int) -> list:
    """Deep 1-minute history: monthly DUMPS for complete past months (fast, ~1 zip/month) + REST for
    the current incomplete month or any month whose dump is missing. Each month is filled independently
    so a missing dump can't trigger a runaway multi-year REST pull."""
    rows = []
    now = pd.Timestamp.now("UTC").tz_localize(None)
    cur_month = pd.Timestamp(now.year, now.month, 1)
    m = pd.Timestamp(pd.Timestamp(start_ms, unit="ms").year, pd.Timestamp(start_ms, unit="ms").month, 1)
    end = pd.Timestamp(end_ms, unit="ms")
    while m <= end and m <= cur_month:
        m_next = m + pd.offsets.MonthBegin(1)
        seg_start = max(int(m.timestamp() * 1000), start_ms)
        seg_end = min(int(m_next.timestamp() * 1000), end_ms)
        if seg_start < seg_end:
            mr = _binance_monthly_rows(sym, "1m", m.year, m.month) if m < cur_month else None
            if mr:
                rows.extend(r for r in mr if seg_start <= r[0] < seg_end)
            else:                                          # current month or missing dump → REST (bounded to the month)
                rows.extend(_binance_rest_rows(sym, "1m", seg_start, seg_end))
        m = m_next
    return rows


def fetch_intraday_crypto(ticker: str, interval: str = "1m", start: str | None = None,
                          end: str | None = None, use_cache: bool = True,
                          max_pages: int = 4000) -> pd.DataFrame:
    """FREE deep intraday crypto bars from Binance (keyless, stdlib only).

    Maps a crypto ticker (BTC-USD/ETH-USD/SOL-USD) to a Binance USDT pair. For **1-minute** it pulls
    DEEP history from the **bulk monthly dumps** (data.binance.vision, ~1 zip/month) for complete
    months + REST for the recent tail — so multi-year 1m works (≈1 download/month vs ~43k REST
    calls/month). Other intervals use plain REST pagination. tz-naive UTC index so the engine's
    groupby-by-date path works. Cached per (symbol, interval) — delete the cache file to refresh.
    Raises on an unmapped ticker or total network failure so the caller can fall back to the daily bar.
    """
    sym = _to_binance_symbol(ticker)
    if sym is None:
        raise RuntimeError(f"{ticker!r} is not a Binance-servable crypto pair "
                           f"(the free 1m feed is crypto-only; use scalp_data='hourly' otherwise)")
    if interval not in _BINANCE_INTERVAL_MS:
        raise RuntimeError(f"unsupported crypto interval {interval!r}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cp = CACHE_DIR / f"binance_{sym}__{interval}.pkl"
    now = pd.Timestamp.now("UTC").tz_localize(None)
    req_start = pd.Timestamp(start or "2017-08-01")
    req_end = pd.Timestamp(end) if end else now
    # COVERAGE-aware cache (keyed by symbol+interval): only reuse it if it actually spans the
    # requested window — otherwise a recent-data cache would slice to EMPTY for a historical window.
    cached = None
    if use_cache and cp.exists():
        cached = pd.read_pickle(cp)
        if (not cached.empty and cached.index.min() <= req_start + pd.Timedelta(days=1)
                and cached.index.max() >= min(req_end, now - pd.Timedelta(days=1)) - pd.Timedelta(days=1)):
            return _slice(cached, start or "1990-01-01", end)

    start_ms = int(req_start.timestamp() * 1000)
    end_ms = int(req_end.timestamp() * 1000)

    # 1-minute over a long window: use the BULK monthly dumps (deep history, ~1 zip/month) + REST for
    # the recent tail. Other intervals (1d daily fallback, etc.) are small → plain REST pagination.
    rows = (_binance_1m_rows(sym, start_ms, end_ms) if interval == "1m"
            else _binance_rest_rows(sym, interval, start_ms, end_ms, max_pages))
    if not rows:
        raise RuntimeError(f"no Binance data for {sym} @ {interval} (geo-blocked? network?)")
    df = _parse_binance_klines(rows)
    if cached is not None and not cached.empty:           # MERGE so a new window never shrinks the cache
        df = pd.concat([cached, df])
        df = df[~df.index.duplicated(keep="last")].sort_index()
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
