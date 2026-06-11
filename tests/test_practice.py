"""Tab 15 «Практика»: construction calculator math + NotebookLM bridge endpoints."""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from antimg import nlm_bridge, options as opt, practice
from antimg.web.api import app

client = TestClient(app)


# ------------------------------------------------------------------ practice.build math
def test_classic_straddle_loss_cap_is_premium():
    """2C−1F at the money: max loss == total premium, at S_T == K (the doctrine's loss cap)."""
    c = practice.build(100.0, 100.0, n_calls=2, n_futs=1, iv=0.40, dte_days=30, multiplier=10)
    assert c.premium > 0
    assert c.max_loss == pytest.approx(-c.premium_total, rel=1e-6)
    assert c.max_loss_at == pytest.approx(100.0, rel=1e-3)
    # symmetric V: both breakevens exist, straddling S0
    assert c.be_down is not None and c.be_down < 100.0
    assert c.be_up is not None and c.be_up > 100.0
    # 2C−1F ATM ⇒ near-zero delta, negative theta
    assert abs(c.delta0) < 0.25
    assert c.theta_day < 0
    assert c.scalp_per_day_needed == pytest.approx(-c.theta_day)


def test_breakevens_solve_expiry_pnl_to_zero():
    c = practice.build(50_000.0, 50_000.0, n_calls=2, n_futs=1, premium=2_000.0, dte_days=45)
    for be in (c.be_down, c.be_up):
        pnl = 2 * max(be - c.K, 0.0) - 1 * (be - c.S0) - 2 * c.premium
        assert pnl == pytest.approx(0.0, abs=1e-6)
    # down BE = S0 − n_c·prem/n_f = 50000 − 4000; up BE = (n_c·prem + n_c·K − n_f·S0)/(n_c−n_f)
    assert c.be_down == pytest.approx(46_000.0)
    assert c.be_up == pytest.approx(54_000.0)


def test_premium_implies_iv_roundtrip():
    iv0 = 0.55
    T = 60 / 365.0
    prem = float(opt.call_price(200.0, 200.0, T, 0.045, iv0))
    c = practice.build(200.0, 200.0, premium=prem, dte_days=60)
    assert c.iv == pytest.approx(iv0, abs=1e-3)


def test_premium_below_intrinsic_no_greeks_but_payoff_works():
    c = practice.build(110.0, 100.0, n_calls=2, n_futs=1, premium=5.0, dte_days=30)  # intrinsic 10 > 5
    assert c.iv is None and c.theta_day is None
    assert c.payoff["expiry"]                       # payoff grid still computed
    assert any("IV не извлечь" in n for n in c.notes)


def test_uncovered_short_futs_flagged_and_unbounded_up():
    c = practice.build(100.0, 100.0, n_calls=1, n_futs=2, iv=0.3, dte_days=30)
    assert c.be_up is None                          # slope above K = −1 → never recovers
    assert any("НЕ ограничен" in n for n in c.notes)


def test_build_validates_inputs():
    with pytest.raises(ValueError):
        practice.build(0.0, 100.0, iv=0.3)
    with pytest.raises(ValueError):
        practice.build(100.0, 100.0)                # neither premium nor iv


# ------------------------------------------------------------------ /api/practice/payoff
def test_api_payoff_endpoint():
    r = client.post("/api/practice/payoff", json={
        "s0": 100_000, "strike": 100_000, "n_calls": 2, "n_futs": 1,
        "iv": 0.6, "dte_days": 30, "multiplier": 1, "lots": 1})
    assert r.status_code == 200
    d = r.json()
    s = d["stats"]
    assert s["max_loss_usd"] == pytest.approx(-s["premium_total_usd"], rel=1e-6)
    assert s["breakeven_down"] < 100_000 < s["breakeven_up"]
    assert len(d["payoff"]["S"]) == len(d["payoff"]["expiry"])
    assert "today" in d["payoff"]                   # iv known → today curve present


def test_api_payoff_validation_maps_to_422():
    r = client.post("/api/practice/payoff", json={"s0": 100, "strike": 100})  # no premium/iv
    assert r.status_code == 422


# ------------------------------------------------------------------ nlm bridge + endpoints
class _FakeProc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_practice_notebooks_endpoint_graceful_without_cli(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "available", lambda: (False, "nlm CLI not found (nlm)."))
    r = client.get("/api/practice/notebooks")
    assert r.status_code == 200                     # never 500s — tab degrades gracefully
    d = r.json()
    assert d["available"] is False and d["notebooks"] == [] and "nlm" in d["error"]


def test_practice_notebooks_lists_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "_list_cache", None)
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return _FakeProc('chatter\n[{"id": "abc12345", "title": "ПИ Коровин", "source_count": 15}]')
    monkeypatch.setattr(subprocess, "run", fake_run)
    r = client.get("/api/practice/notebooks")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["notebooks"] == [{"id": "abc12345", "title": "ПИ Коровин", "sources": 15}]
    client.get("/api/practice/notebooks")           # second hit → cache, no new subprocess
    assert len(calls) == 1


def test_practice_ask_endpoint(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)

    def fake_run(cmd, capture_output, text, timeout):
        assert cmd[1:3] == ["notebook", "query"]
        return _FakeProc('{"value": {"answer": "Пример: RI 100000, 2 колла", "sources_used": [1, 2]}}')
    monkeypatch.setattr(subprocess, "run", fake_run)
    r = client.post("/api/practice/ask",
                    json={"notebook_id": "abcd1234efgh", "question": "дай пример"})
    assert r.status_code == 200
    assert "Пример" in r.json()["answer"]


def test_practice_ask_new_cli_top_level_shape(monkeypatch):
    """notebooklm-mcp-cli >0.6.x returns {"answer": ...} top-level (no "value" wrapper) —
    bit us live 2026-06-11: the wrapped-only parser reported 'empty answer'."""
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(
        '{"status": "success", "answer": "Правило трёх третей…", "sources": ["a", "b"]}'))
    r = client.post("/api/practice/ask",
                    json={"notebook_id": "abcd1234efgh", "question": "что такое три трети"})
    assert r.status_code == 200
    d = r.json()
    assert "трёх третей" in d["answer"] and d["sources_used"] == ["a", "b"]


def test_practice_ask_cli_status_error_surfaced(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(
        '{"status": "error", "error": "Query failed: API error (code 5): NOT_FOUND"}'))
    r = client.post("/api/practice/ask",
                    json={"notebook_id": "abcd1234efgh", "question": "дай пример"})
    assert r.status_code == 502
    assert "NOT_FOUND" in r.json()["error"]


def test_practice_ask_error_becomes_502(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _FakeProc("", returncode=1, stderr="RESOURCE_EXHAUSTED"))
    r = client.post("/api/practice/ask",
                    json={"notebook_id": "abcd1234efgh", "question": "дай пример"})
    assert r.status_code == 502
    assert "RESOURCE_EXHAUSTED" in r.json()["error"]
