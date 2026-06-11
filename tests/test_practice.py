"""Tab 15 «Практика»: construction calculator math + NotebookLM bridge endpoints."""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from antimg import claude_bridge, nlm_bridge, options as opt, practice, practice_log
from antimg.web.api import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_practice_log(tmp_path, monkeypatch):
    """Every test gets its own persisted-state file (endpoints append to it)."""
    monkeypatch.setattr(practice_log, "LOG_PATH", str(tmp_path / "practice_log.json"))


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


def test_classic_long_straddle_real_mes_ticket():
    """The user's REAL ticket: 30 Put + 30 Call, K=7375, MES $5/pt, ask 244 (C) / 227.5 (P),
    DTE 81. Platform showed Max Loss ≈ −70 800 $, BE 6899.61/7850.39 (incl. fees) and
    +261 137 $ at S=9603.69. Clean-premium math must reproduce those within fees."""
    c = practice.build(7384.0, 7375.0, n_calls=30, n_puts=30, n_futs=0,
                       premium=244.0, put_premium=227.5, dte_days=81, multiplier=5)
    assert c.premium_total == pytest.approx(70_725.0)            # 30·(244+227.5)·5
    assert c.max_loss == pytest.approx(-70_725.0, rel=1e-6)      # all premium at the strike
    assert c.max_loss_at == pytest.approx(7375.0, abs=20)        # grid resolution
    assert c.be_down == pytest.approx(6_903.5)                   # K − (C+P) = 7375 − 471.5
    assert c.be_up == pytest.approx(7_846.5)                     # K + (C+P)
    # expiry P&L at the platform's Max-Move point: 150·(9603.69−7375) − 70725 ≈ +263.6k
    pnl = 5 * (30 * (9603.69 - 7375.0) - (30 * 244.0 + 30 * 227.5))
    assert pnl == pytest.approx(263_578.5, rel=1e-6)
    assert any("длинный стреддл" in n for n in c.notes)
    # near-ATM long straddle: roughly delta-neutral, bleeding theta both legs
    assert c.delta0 is not None and abs(c.delta0) < 6            # ~0.2Δ per pair × 30
    assert c.theta_day < 0


def test_put_premium_defaults_to_bs_when_missing():
    c = practice.build(100.0, 100.0, n_calls=1, n_puts=1, n_futs=0, iv=0.4, dte_days=30)
    assert c.put_premium > 0
    assert any("оценена по BS" in n for n in c.notes)


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

    def fake_run(cmd, capture_output, text, timeout, **kw):
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


def test_bridge_query_old_wrapped_shape(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(
        '{"value": {"answer": "Пример: RI 100000, 2 колла", "sources_used": [1, 2]}}'))
    res = nlm_bridge.query("abcd1234efgh", "дай пример")
    assert "Пример" in res["answer"] and res["sources_used"] == [1, 2]


def test_bridge_query_new_cli_top_level_shape(monkeypatch):
    """notebooklm-mcp-cli >0.6.x returns {"answer": ...} top-level (no "value" wrapper) —
    bit us live 2026-06-11: the wrapped-only parser reported 'empty answer'."""
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(
        '{"status": "success", "answer": "Правило трёх третей…", "sources": ["a", "b"]}'))
    res = nlm_bridge.query("abcd1234efgh", "что такое три трети")
    assert "трёх третей" in res["answer"] and res["sources_used"] == ["a", "b"]


def test_bridge_query_cli_status_error_surfaced(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(
        '{"status": "error", "error": "Query failed: API error (code 5): NOT_FOUND"}'))
    assert "NOT_FOUND" in nlm_bridge.query("abcd1234efgh", "дай пример")["error"]


def test_practice_ask_fans_out_to_selected_notebooks(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {
        "notebooks": [{"id": "nb-one-12345", "title": "ПИ Коровин", "sources": 50},
                      {"id": "nb-two-12345", "title": "MES Straddle", "sources": 5}]})
    monkeypatch.setattr(nlm_bridge, "query", lambda nb, q, source_ids=None: {
        "answer": f"ответ из {nb}", "sources_used": [1]})
    r = client.post("/api/practice/ask", json={
        "notebook_ids": ["nb-one-12345", "nb-two-12345", "nb-one-12345"],   # dup dropped
        "question": "дай пример"})
    assert r.status_code == 200
    res = r.json()["results"]
    assert [x["title"] for x in res] == ["ПИ Коровин", "MES Straddle"]
    assert all("ответ из" in x["answer"] for x in res)


def test_practice_ask_partial_failure_is_200(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {"notebooks": []})
    monkeypatch.setattr(nlm_bridge, "query", lambda nb, q, source_ids=None:
                        {"error": "RESOURCE_EXHAUSTED"} if nb.startswith("bad")
                        else {"answer": "ок", "sources_used": []})
    r = client.post("/api/practice/ask", json={
        "notebook_ids": ["good-1234567", "bad-12345678"], "question": "дай пример"})
    assert r.status_code == 200                      # one answer survived
    res = r.json()["results"]
    assert res[0]["answer"] == "ок" and "RESOURCE_EXHAUSTED" in res[1]["error"]


def test_practice_ask_all_failed_becomes_502(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {"notebooks": []})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"error": "RESOURCE_EXHAUSTED"})
    r = client.post("/api/practice/ask",
                    json={"notebook_ids": ["abcd1234efgh"], "question": "дай пример"})
    assert r.status_code == 502
    assert "RESOURCE_EXHAUSTED" in r.json()["error"]


# ------------------------------------------------------------------ claude chat
def test_claude_prompt_includes_history_and_construction():
    p = claude_bridge.build_prompt(
        "посчитай тету", history=[
            {"role": "q", "text": "дай пример"},
            {"role": "a", "text": "RI 100000, премия 2000", "title": "ПИ Коровин"},
            {"role": "c", "text": "вот расчёт"}],
        construction={"premium_total_usd": 4000})
    assert "Прикрытый Интрадей" in p                  # domain preamble
    assert "Ноутбук «ПИ Коровин»: RI 100000" in p
    assert '"premium_total_usd": 4000' in p
    assert p.rstrip().endswith("посчитай тету")


def test_practice_claude_endpoint(monkeypatch):
    monkeypatch.setattr(claude_bridge, "available", lambda: (True, ""))

    def fake_run(cmd, input, capture_output, text, timeout):
        assert cmd[:2] == [claude_bridge.CLAUDE_BIN, "-p"] and "ВОПРОС" in input
        return _FakeProc("Тета ≈ −241 $/день")
    monkeypatch.setattr(subprocess, "run", fake_run)
    r = client.post("/api/practice/claude", json={
        "question": "сколько тета?",
        "history": [{"role": "a", "text": "пример", "title": "ПИ"}],
        "construction": {"theta_usd_per_day": -241}})
    assert r.status_code == 200
    d = r.json()
    assert "Тета" in d["answer"] and d["model"] == claude_bridge.CHAT_MODEL


def test_practice_claude_unavailable_becomes_502(monkeypatch):
    monkeypatch.setattr(claude_bridge, "available",
                        lambda: (False, "claude credentials not seeded"))
    r = client.post("/api/practice/claude", json={"question": "привет"})
    assert r.status_code == 502
    assert "not seeded" in r.json()["error"]


def test_notebooks_endpoint_reports_claude_availability(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "available", lambda: (False, "no nlm"))
    monkeypatch.setattr(claude_bridge, "available", lambda: (True, ""))
    d = client.get("/api/practice/notebooks").json()
    assert d["claude_available"] is True and d["claude_model"] == claude_bridge.CHAT_MODEL


# ------------------------------------------------------------------ Claude as compiler
def test_claude_compiles_notebook_sources_with_participants(monkeypatch):
    """notebook_ids on /api/practice/claude → fan-out first, then Claude compiles;
    the response names every participant (the 'skills' list shown in the UI)."""
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {
        "notebooks": [{"id": "nb-one-12345", "title": "ПИ Коровин", "sources": 50},
                      {"id": "nb-two-12345", "title": "MES Straddle", "sources": 5}]})
    monkeypatch.setattr(nlm_bridge, "query", lambda nb, q, source_ids=None:
                        {"error": "quota"} if nb == "nb-two-12345"
                        else {"answer": "пример RI", "sources_used": [1]})
    seen = {}

    def fake_chat(question, history=None, construction=None, sources=None,
                  skills=None, model=None, images=None):
        seen["sources"] = sources
        return {"answer": "компиляция", "model": model or claude_bridge.CHAT_MODEL}
    monkeypatch.setattr(claude_bridge, "chat", fake_chat)
    r = client.post("/api/practice/claude", json={
        "question": "сведи примеры", "notebook_ids": ["nb-one-12345", "nb-two-12345"],
        "construction": {"premium_total_usd": 4000}})
    assert r.status_code == 200
    d = r.json()
    assert d["answer"] == "компиляция"
    # only the successful notebook reaches Claude as a source
    assert [s["title"] for s in seen["sources"]] == ["ПИ Коровин"]
    kinds = {p["kind"] for p in d["participants"]}
    assert {"doctrine", "construction", "notebook", "claude"} <= kinds
    failed = [p for p in d["participants"] if p["kind"] == "notebook" and p.get("error")]
    assert failed and failed[0]["name"] == "MES Straddle"
    assert len(d["notebook_results"]) == 2


def test_build_prompt_with_sources_has_compile_instruction():
    p = claude_bridge.build_prompt("вопрос", sources=[
        {"title": "ПИ Коровин", "answer": "пример RI 100000"}])
    assert "КОМПИЛЯЦИЯ" in p and "Ноутбук «ПИ Коровин»" in p


# ------------------------------------------------------------------ skills + model choice
@pytest.fixture
def _skills_dir(tmp_path, monkeypatch):
    for name, body in [("hedgedintraday", "# /hedgedintraday — ПИ Коровина\nдоктрина ПИ"),
                       ("antimartingal-strategy", "# /antimartingal-strategy\nEV identity")]:
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(body, encoding="utf-8")
    (tmp_path / "_consult-lib").mkdir()               # underscore dirs are ignored
    monkeypatch.setattr(claude_bridge, "SKILLS_DIR", str(tmp_path))
    return tmp_path


def test_skills_endpoint_lists_doctrines(_skills_dir):
    d = client.get("/api/practice/skills").json()
    assert [s["name"] for s in d["skills"]] == ["antimartingal-strategy", "hedgedintraday"]
    assert "ПИ Коровина" in d["skills"][1]["description"]
    assert d["models"][0] == "claude-fable-5" and d["default_model"] in d["models"]


def test_claude_combines_selected_skills(_skills_dir, monkeypatch):
    seen = {}

    def fake_chat(question, history=None, construction=None, sources=None,
                  skills=None, model=None, images=None):
        seen.update(skills=skills, model=model)
        return {"answer": "ок", "model": model or claude_bridge.CHAT_MODEL}
    monkeypatch.setattr(claude_bridge, "chat", fake_chat)
    r = client.post("/api/practice/claude", json={
        "question": "сведи", "skills": ["hedgedintraday", "antimartingal-strategy"],
        "model": "claude-opus-4-8"})
    assert r.status_code == 200
    assert seen["model"] == "claude-opus-4-8"
    assert [s["name"] for s in seen["skills"]] == ["hedgedintraday", "antimartingal-strategy"]
    assert "доктрина ПИ" in seen["skills"][0]["content"]
    skill_parts = [p["name"] for p in r.json()["participants"] if p["kind"] == "skill"]
    assert skill_parts == ["/hedgedintraday", "/antimartingal-strategy"]


def test_claude_rejects_unknown_skill_and_model(_skills_dir):
    r = client.post("/api/practice/claude",
                    json={"question": "x", "skills": ["../etc/passwd"]})
    assert r.status_code == 422
    r = client.post("/api/practice/claude",
                    json={"question": "x", "model": "gpt-5"})
    assert r.status_code == 422


def test_build_prompt_includes_skill_doctrines():
    p = claude_bridge.build_prompt("вопрос", skills=[
        {"name": "hedgedintraday", "content": "правило трёх третей"}])
    assert "СКИЛЛЫ-ДОКТРИНЫ" in p and "[Скилл /hedgedintraday]" in p


# ------------------------------------------------------------------ extraction → graph
def test_extract_construction_parses_haiku_json(monkeypatch):
    monkeypatch.setattr(claude_bridge, "_run_claude", lambda prompt, model: {
        "answer": 'вот:\n{"instrument": "RTS", "s0": 100000, "strike": 100000, '
                  '"n_calls": 2, "n_futs": 1, "premium": 2000, "dte_days": 30, '
                  '"multiplier": null, "iv": null, "comment": "пример из вебинара"}',
        "model": "claude-haiku-4-5"})
    res = claude_bridge.extract_construction("текст примера…")
    assert res["params"]["s0"] == 100000 and res["params"]["premium"] == 2000
    assert "multiplier" not in res["params"]          # nulls dropped
    assert res["comment"] == "пример из вебинара"


def test_extract_endpoint(monkeypatch):
    monkeypatch.setattr(claude_bridge, "extract_construction", lambda text: {
        "params": {"s0": 50_000, "strike": 50_000, "premium": 1500},
        "comment": "ок", "model": "claude-haiku-4-5"})
    r = client.post("/api/practice/extract", json={"text": "x" * 40})
    assert r.status_code == 200
    assert r.json()["params"]["s0"] == 50_000


# ------------------------------------------------------------------ exact-file selection
def test_bridge_query_passes_source_ids(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, **kw):
        seen["cmd"] = cmd
        return _FakeProc('{"answer": "ок", "sources": []}')
    monkeypatch.setattr(subprocess, "run", fake_run)
    nlm_bridge.query("nb-12345678", "вопрос", source_ids=["s1", "s2"])
    i = seen["cmd"].index("--source-ids")
    assert seen["cmd"][i + 1] == "s1,s2"


def test_sources_endpoint_lists_files(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "available", lambda: (True, ""))
    monkeypatch.setattr(nlm_bridge, "MIN_GAP", 0.0)
    monkeypatch.setattr(nlm_bridge, "_sources_cache", {})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(
        '[{"id": "src-1", "title": "Вебинар 3"}, {"id": "src-2", "name": "Инструкция"}]'))
    d = client.get("/api/practice/sources?notebook_id=nb-12345678").json()
    assert d["sources"] == [{"id": "src-1", "title": "Вебинар 3"},
                            {"id": "src-2", "title": "Инструкция"}]


def test_ask_fans_out_with_per_notebook_source_filter(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {
        "notebooks": [{"id": "nb-one-12345", "title": "ПИ", "sources": 50}]})
    seen = {}

    def fake_query(nb, q, source_ids=None):
        seen[nb] = source_ids
        return {"answer": "ок", "sources_used": []}
    monkeypatch.setattr(nlm_bridge, "query", fake_query)
    r = client.post("/api/practice/ask", json={
        "notebook_ids": ["nb-one-12345"], "question": "дай пример",
        "sources": {"nb-one-12345": ["src-1", "src-2"]}})
    assert r.status_code == 200
    assert seen["nb-one-12345"] == ["src-1", "src-2"]
    assert r.json()["results"][0]["source_filter"] == 2


# ------------------------------------------------------------------ real price + image
def _fake_daily(end_offset_days: int):
    import pandas as pd
    end = pd.Timestamp.now().normalize() - pd.Timedelta(days=end_offset_days)
    idx = pd.date_range(end=end, periods=30, freq="D")
    return pd.DataFrame({"Open": 100.0, "High": 102.0, "Low": 99.0, "Volume": 1000,
                         "Close": [100.0 + i for i in range(30)]}, index=idx)


def test_price_endpoint_returns_fresh_close(monkeypatch):
    from antimg import data as datamod
    df = _fake_daily(1)                               # yesterday's close → fresh
    monkeypatch.setattr(datamod, "fetch", lambda ticker, start=None, refresh=False: df)
    d = client.get("/api/practice/price?ticker=SPY").json()
    assert d["price"] == 129.0 and d["stale"] is False and d["atr"] > 0


def test_price_endpoint_refreshes_stale_cache(monkeypatch):
    """The daily cache has no TTL — a weeks-old close must trigger refresh=True
    (the live 'price of the asset is simply wrong' bug, BTC served 6 weeks stale)."""
    from antimg import data as datamod
    calls = []

    def fake_fetch(ticker, start=None, refresh=False):
        calls.append(refresh)
        return _fake_daily(1) if refresh else _fake_daily(40)
    monkeypatch.setattr(datamod, "fetch", fake_fetch)
    d = client.get("/api/practice/price?ticker=SPY").json()
    assert True in calls                              # refresh was forced
    assert d["stale"] is False                        # and the fresh close is served


def test_price_endpoint_flags_stale_when_refresh_fails(monkeypatch):
    from antimg import data as datamod
    state = {"first": True}

    def fake_fetch(ticker, start=None, refresh=False):
        if refresh:
            raise RuntimeError("Yahoo 429")
        return _fake_daily(40)
    monkeypatch.setattr(datamod, "fetch", fake_fetch)
    d = client.get("/api/practice/price?ticker=SPY").json()
    assert d["stale"] is True                         # honest flag, no 500


def test_upload_and_extract_image_roundtrip(tmp_path, monkeypatch):
    import antimg.web.api as apimod
    monkeypatch.setattr(apimod, "_UPLOAD_DIR", str(tmp_path / "up"))
    monkeypatch.setattr(claude_bridge, "extract_construction_from_image", lambda path: {
        "params": {"s0": 5000, "strike": 5000, "premium": 120}, "comment": "со скрина",
        "model": "claude-haiku-4-5"})
    r = client.post("/api/practice/upload",
                    files={"file": ("board.png", b"\x89PNG fake", "image/png")})
    assert r.status_code == 200
    path = r.json()["path"]
    r2 = client.post("/api/practice/extract-image", json={"path": path})
    assert r2.status_code == 200 and r2.json()["params"]["s0"] == 5000


def test_multiple_images_persist_and_attach_to_claude(tmp_path, monkeypatch):
    """Several uploaded pictures live in the tab state and ride along with a Claude
    question (the model reads them via Read) — no NotebookLM involved."""
    import antimg.web.api as apimod
    monkeypatch.setattr(apimod, "_UPLOAD_DIR", str(tmp_path / "up"))
    paths = []
    for nm in ("board1.png", "board2.jpg"):
        r = client.post("/api/practice/upload",
                        files={"file": (nm, b"\x89PNG fake", "image/png")})
        paths.append(r.json()["path"])
    st = client.get("/api/practice/state").json()
    assert [i["name"] for i in st["images"]] == ["board1.png", "board2.jpg"]

    seen = {}

    def fake_chat(question, history=None, construction=None, sources=None,
                  skills=None, model=None, images=None):
        seen["images"] = images
        return {"answer": "вижу обе картинки", "model": model or claude_bridge.CHAT_MODEL}
    monkeypatch.setattr(claude_bridge, "chat", fake_chat)
    r = client.post("/api/practice/claude",
                    json={"question": "сравни две доски", "images": paths})
    assert r.status_code == 200
    assert [i["name"] for i in seen["images"]] == ["board1.png", "board2.jpg"]
    img_parts = [p["name"] for p in r.json()["participants"] if p["kind"] == "image"]
    assert img_parts == ["📷 board1.png", "📷 board2.jpg"]

    # remove one: file gone + state updated
    r2 = client.post("/api/practice/image/remove", json={"path": paths[0]})
    assert [i["name"] for i in r2.json()["images"]] == ["board2.jpg"]
    import os as _os
    assert not _os.path.exists(paths[0])


def test_claude_rejects_foreign_image_path(tmp_path, monkeypatch):
    import antimg.web.api as apimod
    monkeypatch.setattr(apimod, "_UPLOAD_DIR", str(tmp_path / "up"))
    r = client.post("/api/practice/claude",
                    json={"question": "x", "images": ["/etc/passwd"]})
    assert r.status_code == 422


def test_build_prompt_with_images_demands_read():
    p = claude_bridge.build_prompt("что на скрине?", images=[
        {"path": "/data/uploads/a.png", "name": "доска.png"}])
    assert "инструментом Read" in p and "/data/uploads/a.png" in p


def test_upload_rejects_non_image_and_foreign_paths(tmp_path, monkeypatch):
    import antimg.web.api as apimod
    monkeypatch.setattr(apimod, "_UPLOAD_DIR", str(tmp_path / "up"))
    r = client.post("/api/practice/upload",
                    files={"file": ("evil.sh", b"#!/bin/sh", "text/x-sh")})
    assert r.status_code == 422
    r2 = client.post("/api/practice/extract-image", json={"path": "/etc/passwd"})
    assert r2.status_code == 422


# ------------------------------------------------------------------ persisted tab state
def test_state_accumulates_and_restores(monkeypatch):
    monkeypatch.setattr(nlm_bridge, "list_notebooks", lambda force=False: {"notebooks": []})
    monkeypatch.setattr(nlm_bridge, "query",
                        lambda nb, q, source_ids=None: {"answer": "ответ ноутбука", "sources_used": [1, 2]})
    client.post("/api/practice/ask",
                json={"notebook_ids": ["abcd1234efgh"], "question": "дай пример"})
    client.post("/api/practice/payoff", json={
        "s0": 100, "strike": 100, "n_calls": 2, "n_futs": 1, "iv": 0.4, "dte_days": 30})
    st = client.get("/api/practice/state").json()
    roles = [e["role"] for e in st["entries"]]
    assert roles == ["q", "a"]                        # question + notebook answer logged
    assert st["entries"][1]["sources"] == 2
    assert st["construction"]["request"]["s0"] == 100
    assert st["construction"]["stats"]["premium_total_usd"] > 0
    # clear wipes everything
    client.post("/api/practice/state/clear")
    st2 = client.get("/api/practice/state").json()
    assert st2["entries"] == [] and st2["construction"] is None


def test_state_log_is_capped(monkeypatch):
    monkeypatch.setattr(practice_log, "MAX_ENTRIES", 5)
    for i in range(8):
        practice_log.append("q", f"вопрос {i}")
    st = practice_log.load()
    assert len(st["entries"]) == 5
    assert st["entries"][-1]["text"] == "вопрос 7"


def test_state_survives_corrupted_file(monkeypatch, tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(practice_log, "LOG_PATH", str(bad))
    st = practice_log.load()                          # no 500 — resets instead
    assert st["entries"] == []
