"""Black-Scholes for the options tab (Tab 3).

We have no free historical option chains, so option prices are *modeled* via BS with the
asset's realized volatility as the IV input (user's choice 2026-05-29). This lets us
auto-compute and plot the call's delta over the holding period and see how far a
deep-ITM call really sits from the delta=1 linear assumption.
"""
from __future__ import annotations

import numpy as np
from scipy.special import ndtr  # standard-normal CDF; identical to scipy.stats.norm.cdf

# scipy.stats.norm.cdf carries frozen-distribution overhead (~38 us/scalar call); ndtr is the
# bare CDF (~0.1 us) and numerically identical. BS here runs inside 64-80-iteration bisection
# loops called per-bar, so this swap is the dominant speedup (see options speed benchmark).


def d1_d2(S, K, T, r, sigma, q=0.0):
    S = np.asarray(S, dtype=float)
    T = np.maximum(np.asarray(T, dtype=float), 1e-9)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-9)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return d1, d2


def call_delta(S, K, T, r, sigma, q=0.0):
    """Δ = e^{-qT} N(d1) for a European call. Deep ITM -> ~1."""
    d1, _ = d1_d2(S, K, T, r, sigma, q)
    return np.exp(-q * np.asarray(T, dtype=float)) * ndtr(d1)


def call_price(S, K, T, r, sigma, q=0.0):
    """European call price (per 1 unit underlying; multiply by contract multiplier)."""
    d1, d2 = d1_d2(S, K, T, r, sigma, q)
    T = np.maximum(np.asarray(T, dtype=float), 1e-9)
    return (np.asarray(S, dtype=float) * np.exp(-q * T) * ndtr(d1)
            - K * np.exp(-r * T) * ndtr(d2))


def put_price(S, K, T, r, sigma, q=0.0):
    """European put price (per 1 unit underlying) via put–call parity / direct BS."""
    d1, d2 = d1_d2(S, K, T, r, sigma, q)
    T = np.maximum(np.asarray(T, dtype=float), 1e-9)
    return (K * np.exp(-r * T) * ndtr(-d2)
            - np.asarray(S, dtype=float) * np.exp(-q * T) * ndtr(-d1))


def straddle_price(S, K, T, r, sigma, q=0.0):
    """Long straddle premium (per 1 unit underlying) = ATM call + put. The 'rent' you pay to be
    long volatility for T years. At expiry the position is worth |S_T − K| (intrinsic), so it
    profits only when the realized move exceeds this premium."""
    return call_price(S, K, T, r, sigma, q) + put_price(S, K, T, r, sigma, q)


def price_for_value(target_value, K, T, r, sigma, q=0.0, S_lo=None, S_hi=None):
    """Underlying price S at which a call is worth `target_value` (per 1 unit underlying).

    Call price is strictly increasing in S, so bisection is exact. Used by the long-call
    coin-flip to find the spot where the option DOUBLES (target_value = 2·premium) — i.e.
    the dynamic '+m·ATR' win level, which depends on IV/DTE/strike, not a fixed 2·ATR.
    """
    lo = S_lo if S_lo is not None else max(K * 1e-3, 1e-6)
    hi = S_hi if S_hi is not None else max(K * 10.0, target_value * 10.0 + K)
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        if float(call_price(mid, K, T, r, sigma, q)) < target_value:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def strike_for_delta(S, T, r, sigma, target_delta=0.95, q=0.0):
    """Solve for the call strike giving `target_delta` (bisection on K).

    Deep-ITM => low strike => high delta. Used when the user picks a delta target
    instead of a fixed moneyness.
    """
    target_delta = min(max(target_delta, 1e-4), 0.9999)
    lo, hi = S * 1e-3, S * 5.0           # very-low strike => delta~1, very-high => delta~0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        dlt = float(call_delta(S, mid, T, r, sigma, q))
        if dlt > target_delta:           # delta too high -> raise strike
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
