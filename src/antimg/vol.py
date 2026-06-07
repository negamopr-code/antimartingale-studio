"""Volatility surface: real ATM term-structure (CBOE vol indices) + a fixed-β skew.

The options tab prices a modeled call with Black-Scholes, so the only thing that makes the
premium realistic is the IV input. Two refinements over a single flat VIX number:

1. **Term structure.** VIX is a 30-day number; a 365-day LEAPS trades on a different IV.
   When the real CBOE points exist (S&P: ^VIX9D 9d, ^VIX 30d, ^VIX3M 90d, ^VIX6M 180d) we
   interpolate the ATM IV to the option's actual tenor in **variance-time** (linear in total
   variance σ²·T vs T), and extrapolate flat in σ beyond the longest/shortest point. This is
   real market data, not an assumption.

2. **Skew.** Equity-index smiles slope down (lower strikes carry higher IV). With a delta
   target the chosen strike is off-ATM (deep-ITM at Δ0.9), so the smile shifts the premium.
   Modeled as an additive vol skew σ(m) = σ_atm + β·m, m = ln(K/S); β is a fixed per-asset-
   class constant (equity index β<0; commodities/FX flatter or a smile). β is a TUNABLE knob
   (UI slider) — it is a calibration, not free market data, so we keep it simple & explicit.

Everything degrades gracefully: with one tenor only (a non-S&P vol index, or realized vol)
the term structure is flat; with β=0 there is no skew (pure ATM, the old behaviour).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data as datamod

# ---- CBOE constant-maturity vol indices, by underlying asset class -----------------------
# Each maps to {tenor_in_days: yahoo_ticker}. The S&P curve has the full term structure;
# the others are single 30-day points (still REAL implied vol, just no term structure).
SP_TERM = {9: "^VIX9D", 30: "^VIX", 93: "^VIX3M", 184: "^VIX6M"}   # ^VIX1Y exists but is spotty
VOL_INDEX_BY_CLASS = {
    "sp500":   SP_TERM,
    "nasdaq":  {30: "^VXN"},
    "russell": {30: "^RVX"},
    "dow":     {30: "^VXD"},
    "gold":    {30: "^GVZ"},
    "oil":     {30: "^OVX"},
    "eurusd":  {30: "^EVZ"},
}

# Fixed skew β (additive annualized-vol per unit ln-moneyness). Negative = downside puts /
# low strikes bid (classic equity smirk). Commodities/FX run flatter / smile-ish.
SKEW_BY_CLASS = {
    "sp500": -0.18, "nasdaq": -0.16, "russell": -0.15, "dow": -0.17,
    "gold": -0.05, "oil": -0.02, "eurusd": -0.03, "crypto": -0.04, "other": -0.10,
}

# Ticker → asset class. Used to pick the vol index + the default β.
_CLASS_BY_TICKER = {
    "sp500":   {"SPY", "^GSPC", "^SPX", "SPX", "ES=F", "MES=F", "VOO", "IVV", "SPXL", "UPRO", "XSP"},
    "nasdaq":  {"QQQ", "^NDX", "^IXIC", "NQ=F", "MNQ=F", "TQQQ"},
    "russell": {"IWM", "^RUT", "RTY=F", "M2K=F"},
    "dow":     {"DIA", "^DJI", "YM=F", "MYM=F"},
    "gold":    {"GLD", "GC=F", "MGC=F", "IAU"},
    "oil":     {"USO", "CL=F", "MCL=F", "BZ=F"},
    "eurusd":  {"EURUSD=X", "6E=F", "M6E=F"},
    "crypto":  {"BTC-USD", "ETH-USD", "SOL-USD", "BITO"},
}


def classify(ticker: str) -> str:
    t = ticker.upper()
    for cls, members in _CLASS_BY_TICKER.items():
        if t in members:
            return cls
    return "other"


def _dvol_currency(ticker: str) -> str | None:
    """Map a crypto ticker to a Deribit DVOL currency (BTC/ETH) — real implied vol exists only for these.
    Matches BTC*/ETH* (BTC-USD, ETH-USDT, …) but NOT lookalikes (ETC-, BCH-)."""
    t = ticker.upper().replace("X:", "")
    if t.startswith("BTC") or t.startswith("XBT"):
        return "BTC"
    if t.startswith("ETH"):
        return "ETH"
    return None


def default_skew_beta(ticker: str) -> float:
    return SKEW_BY_CLASS.get(classify(ticker), SKEW_BY_CLASS["other"])


class VolModel:
    """Queryable IV surface: σ(date, T_years, K, S) = atm_term(date,T) + β·ln(K/S)."""

    def __init__(self, tenors: dict[float, pd.Series], skew_beta: float = 0.0,
                 floor: float = 1e-3, label: str = ""):
        # tenors: {tenor_in_years: annualized-vol Series indexed by date}
        self._T = sorted(tenors)                       # ascending tenors (years)
        self._series = {T: tenors[T].dropna() for T in self._T}
        self.skew_beta = float(skew_beta)
        self.floor = floor
        self.label = label

    # -- ATM term structure (variance-time interpolation) --
    def atm(self, date, T_years: float) -> float | None:
        pts = []
        for T in self._T:
            v = self._series[T].asof(pd.Timestamp(date))
            if v is not None and np.isfinite(v) and v > 0:
                pts.append((T, float(v)))
        if not pts:
            return None
        if len(pts) == 1:
            return pts[0][1]                           # single tenor → flat in T
        Ts = np.array([p[0] for p in pts])
        sig = np.array([p[1] for p in pts])
        var = sig ** 2 * Ts                            # total variance at each tenor
        Tq = max(float(T_years), 1e-6)
        if Tq <= Ts[0]:
            return float(sig[0])                       # flat-σ extrapolation (short end)
        if Tq >= Ts[-1]:
            return float(sig[-1])                      # flat-σ extrapolation (long end)
        vq = float(np.interp(Tq, Ts, var))            # linear in total variance
        return float(np.sqrt(max(vq, 0.0) / Tq))

    # -- full smile (ATM + additive skew in ln-moneyness) --
    def sigma(self, date, T_years: float, K: float, S: float,
              default: float = 0.20) -> float:
        a = self.atm(date, T_years)
        if a is None:
            a = default
        m = np.log(max(K, 1e-9) / max(S, 1e-9))        # ln-moneyness (K<S ⇒ m<0 ⇒ +skew if β<0)
        return max(a + self.skew_beta * m, self.floor)


def build(ticker: str, start: str, *, iv_source: str = "auto",
          skew_beta: float | None = None, realized: pd.Series | None = None,
          iv_const: float = 0.20) -> VolModel:
    """Construct a VolModel for a ticker.

    iv_source: 'auto' (vol index by class, else realized), 'vix'/'index' (force the class's
    vol index), 'realized' (rolling realized vol, flat in T), 'constant' (flat IV).
    skew_beta: None → asset-class default; a number → override (UI slider).
    """
    cls = classify(ticker)
    beta = default_skew_beta(ticker) if skew_beta is None else float(skew_beta)

    if iv_source == "constant" and realized is not None:
        return VolModel({1.0: realized}, beta, label="constant")  # realized used as the flat const series
    if iv_source == "constant":
        idx = pd.date_range(start, periods=2, freq="D")
        return VolModel({1.0: pd.Series(iv_const, index=idx)}, beta, label="constant")

    if iv_source in ("auto", "vix", "index"):
        ccy = _dvol_currency(ticker)                          # BTC/ETH → real Deribit DVOL (the crypto VIX)
        if ccy:
            try:
                dv = datamod.fetch_dvol(ccy, start=start)
                if dv is not None and not dv.empty:
                    return VolModel({30.0 / 365.0: dv}, beta, label=f"index:dvol-{ccy}")
            except Exception:
                pass                                          # Deribit unreachable → fall through to realized
        tenors_days = VOL_INDEX_BY_CLASS.get(cls)
        if tenors_days:
            loaded: dict[float, pd.Series] = {}
            for days, sym in tenors_days.items():
                try:
                    s = datamod.fetch(sym, start=start)["Close"] / 100.0
                    if not s.empty:
                        loaded[days / 365.0] = s
                except Exception:
                    continue
            if loaded:
                return VolModel(loaded, beta, label=f"index:{cls}")
        # 'vix'/'index' explicitly asked but no index for this class, or fetch failed → realized

    if realized is not None:
        return VolModel({1.0: realized}, beta, label="realized")
    # last resort: a flat constant so pricing never crashes
    idx = pd.date_range(start, periods=2, freq="D")
    return VolModel({1.0: pd.Series(iv_const, index=idx)}, beta, label="fallback-const")
