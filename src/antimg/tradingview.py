"""TradingView alert → Signal adapter.

TradingView sends a webhook (JSON body) when a Pine Script alert fires. Pine cannot call
out directly, but `alert()` / strategy alerts POST a user-defined JSON message. Recommended
alert-message template (paste into the TradingView alert dialog):

    {
      "passphrase": "YOUR_SECRET",
      "ticker": "{{ticker}}",
      "action": "{{strategy.order.action}}",
      "price": {{close}},
      "time": "{{timenow}}",
      "strategy": "my-antimg-strat",
      "comment": "{{strategy.order.comment}}",
      "pnl": {{strategy.order.profit}}
    }

`pnl` (or an explicit "outcome":"win"/"loss") lets us classify the trade; our calculator
then overlays antimartingale sizing on the resulting win/loss stream.
"""
from __future__ import annotations

import json

from .signals import Signal

_ALIASES = {
    "ticker": ("ticker", "symbol", "sym"),
    "action": ("action", "side", "order_action", "strategy.order.action"),
    "price": ("price", "close", "fill_price"),
    "signal_time": ("time", "signal_time", "timenow", "timestamp"),
    "strategy_id": ("strategy", "strategy_id", "strategy_name", "id"),
    "outcome": ("outcome", "result"),
    "pnl": ("pnl", "profit", "realized_pnl", "strategy.order.profit"),
    "comment": ("comment", "message", "msg", "text"),
}


def _pick(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] not in ("", None):
            return d[k]
    return None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_alert(payload: dict | str) -> Signal:
    """Build a Signal from a TradingView alert payload (dict or raw JSON/text string)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {"comment": payload}

    action = (_pick(payload, _ALIASES["action"]) or "").lower() or "signal"
    outcome = _pick(payload, _ALIASES["outcome"])
    outcome = outcome.lower() if isinstance(outcome, str) else outcome
    return Signal(
        source="tradingview",
        ticker=str(_pick(payload, _ALIASES["ticker"]) or "UNKNOWN").upper(),
        action=action,
        price=_to_float(_pick(payload, _ALIASES["price"])),
        signal_time=_pick(payload, _ALIASES["signal_time"]),
        strategy_id=str(_pick(payload, _ALIASES["strategy_id"]) or "default"),
        outcome=outcome if outcome in ("win", "loss") else None,
        pnl=_to_float(_pick(payload, _ALIASES["pnl"])),
        comment=str(_pick(payload, _ALIASES["comment"]) or ""),
        raw=raw,
    )


def extract_passphrase(payload: dict | str) -> str | None:
    if isinstance(payload, dict):
        return payload.get("passphrase") or payload.get("secret")
    try:
        d = json.loads(payload)
        return d.get("passphrase") or d.get("secret")
    except Exception:
        return None
