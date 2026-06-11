"""Persistent state for the Practice tab — the answers log + the last construction.

The user iterates incrementally: ask notebooks → build the graph → ask Claude →
tweak → repeat, possibly across page reloads and days. So the chat log and the
last calculator construction are saved SERVER-side in the /data volume (survives
container restarts) and replayed by the UI on load.

Plain JSON file guarded by fcntl.lockf — gunicorn runs several workers, each
request does read-modify-write under an exclusive lock. Volumes are tiny (the
log is capped), so a file beats dragging in a DB table.
"""
from __future__ import annotations

import fcntl
import json
import os
import time

LOG_PATH = os.environ.get("ANTIMG_PRACTICE_LOG", "practice_log.json")
MAX_ENTRIES = int(os.environ.get("ANTIMG_PRACTICE_LOG_MAX", "200"))
MAX_TEXT = 20_000

def _empty() -> dict:
    return {"entries": [], "construction": None}


def _locked(fn):
    """Open-or-create the state file, lock it exclusively, run fn(state) -> state|None."""
    os.makedirs(os.path.dirname(os.path.abspath(LOG_PATH)), exist_ok=True)
    with open(LOG_PATH, "a+", encoding="utf-8") as fh:
        fcntl.lockf(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read()
        try:
            state = json.loads(raw) if raw.strip() else _empty()
        except Exception:           # corrupted file — start over rather than 500 forever
            state = _empty()
        state.setdefault("entries", [])
        state.setdefault("construction", None)
        new = fn(state)
        if new is not None:
            fh.seek(0)
            fh.truncate()
            json.dump(new, fh, ensure_ascii=False)
        return state if new is None else new


def load() -> dict:
    return _locked(lambda s: None)


def append(role: str, text: str, **extra) -> dict:
    entry = {"role": role, "text": (text or "")[:MAX_TEXT], "ts": int(time.time()),
             **{k: v for k, v in extra.items() if v is not None}}

    def fn(s):
        s["entries"].append(entry)
        if len(s["entries"]) > MAX_ENTRIES:
            s["entries"] = s["entries"][-MAX_ENTRIES:]
        return s
    return _locked(fn)


def set_construction(construction: dict | None) -> dict:
    def fn(s):
        s["construction"] = construction
        return s
    return _locked(fn)


def clear() -> dict:
    return _locked(lambda s: _empty())
