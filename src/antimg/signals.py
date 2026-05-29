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


def signals_to_trials(signals: list[Signal]) -> list[Trial]:
    """Turn outcome-bearing signals (e.g. TradingView closed-trade alerts) into Trials.

    Signals without a resolvable outcome are skipped. atr_entry falls back to |pnl| (or a
    1% notional proxy) so the options sizing engine can still size `units = bet/atr_entry`.
    Entry/exit *pairing* of separate open/close alerts is a future extension.
    """
    trials: list[Trial] = []
    for s in signals:
        outcome = s.resolved_outcome()
        if outcome is None:
            continue
        t = pd.Timestamp(s.signal_time or s.received_at or _utcnow())
        price = float(s.price) if s.price is not None else 0.0
        atr = abs(s.pnl) if s.pnl else (price * 0.01 if price else 1.0)
        trials.append(Trial(entry_date=t, exit_date=t, entry_price=price,
                            exit_price=price, atr_entry=float(atr) or 1.0,
                            outcome=outcome, days_held=0))
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
