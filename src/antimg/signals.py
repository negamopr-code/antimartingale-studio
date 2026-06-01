"""Signal ingestion + storage — the seam that lets external strategies (TradingView)
feed the antimartingale money-management engine.

Architecture: a *signal source* produces a sequence of `Trial`s (win/loss units); the
sizing engine (atr_strategy.run_linear / run_options) is agnostic to where they came from.
  - HistoricalAtrSource  -> atr_strategy.resolve_trials (price data)        [implemented]
  - WebhookSignalSource  -> TradingView alerts persisted here, replayed     [this module]

Storage is behind the `SignalStore` interface so SQLite (default, single-node) can be
swapped for Postgres/Redis when scaling out — no engine changes required.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import pandas as pd

from .atr_strategy import Trial


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Signal:
    source: str                  # 'tradingview' | 'manual' | ...
    ticker: str
    action: str                  # 'buy' | 'sell' | 'close' | 'win' | 'loss'
    price: float | None = None
    signal_time: str | None = None       # ISO time from the alert, if any
    strategy_id: str = "default"
    outcome: str | None = None            # 'win' | 'loss' (if the alert encodes a result)
    pnl: float | None = None              # realized P&L if provided
    comment: str = ""
    received_at: str | None = None
    raw: str = ""                         # original payload JSON
    id: int | None = None

    def resolved_outcome(self) -> str | None:
        if self.outcome in ("win", "loss"):
            return self.outcome
        if self.action in ("win", "loss"):
            return self.action
        if self.pnl is not None:
            return "win" if self.pnl > 0 else "loss"
        return None


class SignalStore(Protocol):
    def add(self, sig: Signal) -> int: ...
    def list(self, strategy_id: str | None = None, limit: int = 1000) -> list[Signal]: ...
    def clear(self, strategy_id: str | None = None) -> int: ...


class InMemorySignalStore:
    def __init__(self) -> None:
        self._rows: list[Signal] = []
        self._lock = threading.Lock()

    def add(self, sig: Signal) -> int:
        with self._lock:
            sig.id = len(self._rows) + 1
            sig.received_at = sig.received_at or _utcnow()
            self._rows.append(sig)
            return sig.id

    def list(self, strategy_id=None, limit=1000):
        rows = [s for s in self._rows if strategy_id is None or s.strategy_id == strategy_id]
        return rows[-limit:]

    def clear(self, strategy_id=None):
        with self._lock:
            before = len(self._rows)
            self._rows = [s for s in self._rows if strategy_id is not None and s.strategy_id != strategy_id]
            return before - len(self._rows)


class SQLiteSignalStore:
    """Single-node default. Thread-safe via a lock + per-call connection."""

    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT, source TEXT, ticker TEXT, action TEXT,
                    price REAL, signal_time TEXT, strategy_id TEXT,
                    outcome TEXT, pnl REAL, comment TEXT, raw TEXT
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_strategy ON signals(strategy_id)")

    def add(self, sig: Signal) -> int:
        sig.received_at = sig.received_at or _utcnow()
        with self._lock, self._conn() as c:
            cur = c.execute(
                """INSERT INTO signals(received_at,source,ticker,action,price,signal_time,
                   strategy_id,outcome,pnl,comment,raw)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (sig.received_at, sig.source, sig.ticker, sig.action, sig.price,
                 sig.signal_time, sig.strategy_id, sig.outcome, sig.pnl, sig.comment, sig.raw))
            sig.id = cur.lastrowid
            return sig.id

    def list(self, strategy_id=None, limit=1000):
        q = "SELECT * FROM signals"
        args: tuple = ()
        if strategy_id is not None:
            q += " WHERE strategy_id=?"; args = (strategy_id,)
        q += " ORDER BY id DESC LIMIT ?"; args = args + (limit,)
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        out = [Signal(**{k: r[k] for k in r.keys()}) for r in rows]
        out.reverse()  # chronological
        return out

    def clear(self, strategy_id=None):
        with self._lock, self._conn() as c:
            if strategy_id is None:
                n = c.execute("DELETE FROM signals").rowcount
            else:
                n = c.execute("DELETE FROM signals WHERE strategy_id=?", (strategy_id,)).rowcount
            return n


def _ts(s: Signal) -> "pd.Timestamp":
    return pd.Timestamp(s.signal_time or s.received_at or _utcnow())


def signals_to_trials(signals: list[Signal]) -> list[Trial]:
    """Turn the signal stream into Trials, handling BOTH alert styles:

    1. **Closed-trade alert** (carries `pnl` or `outcome`) → one Trial directly.
    2. **Separate open + close alerts**: an OPEN (`action` buy/sell, a `price`, no outcome) is
       held; the next CLOSE (`action` close/exit/flat, a `price`, no outcome) on the same
       (strategy, ticker) is PAIRED with it — outcome inferred from the price move and side
       (long: close≥open ⇒ win; short: close≤open ⇒ loss-inverted), entry/exit = the two prices.

    Signals that resolve to neither are skipped. `atr_entry` falls back to |pnl|, then the
    open→close move, then a 1% notional proxy, so `units = bet/atr_entry` stays sane.
    """
    trials: list[Trial] = []
    open_pos: dict[tuple[str, str], Signal] = {}          # (strategy_id, ticker) -> open alert
    for s in signals:
        key = (s.strategy_id, s.ticker)
        outcome = s.resolved_outcome()
        if outcome is not None:                            # style 1: self-contained closed trade
            t = _ts(s)
            price = float(s.price) if s.price is not None else 0.0
            atr = abs(s.pnl) if s.pnl else (price * 0.01 if price else 1.0)
            trials.append(Trial(entry_date=t, exit_date=t, entry_price=price, exit_price=price,
                                atr_entry=float(atr) or 1.0, outcome=outcome, days_held=0))
            open_pos.pop(key, None)                         # any dangling open is now resolved
            continue
        act = (s.action or "").lower()
        if act in ("buy", "sell", "long", "short") and s.price is not None:
            open_pos[key] = s                              # style 2: remember the OPEN
            continue
        if act in ("close", "exit", "flat") and s.price is not None and key in open_pos:
            o = open_pos.pop(key)                          # style 2: PAIR open→close
            short = (o.action or "").lower() in ("sell", "short")
            entry, exit_ = float(o.price), float(s.price)
            move = (entry - exit_) if short else (exit_ - entry)
            trials.append(Trial(entry_date=_ts(o), exit_date=_ts(s), entry_price=entry,
                                exit_price=exit_, atr_entry=abs(move) or 1.0,
                                outcome="win" if move > 0 else "loss",
                                days_held=max((_ts(s) - _ts(o)).days, 0)))
    return trials


def to_dict(sig: Signal) -> dict:
    d = asdict(sig)
    d.pop("raw", None)  # don't echo full payload by default
    return d


def parse_raw(raw: str) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        return {}
