"""Headless `claude -p` chat for the Practice tab — talk to a Claude model directly.

Pattern proven by yt2nlm-web / chat-27074 in this sandbox: the image bakes the
Claude Code CLI; deploy/entrypoint.sh seeds OAuth credentials from a READ-ONLY
/seed mount of the operator's ~/.claude into the container's own CLAUDE_CONFIG_DIR
(copy, not mount — the container never writes back into the live host config).

Pure text-in/text-out, no tools, stateless: the web client resends a compact
chat history each turn and we rebuild one prompt (no --resume bookkeeping; the
app stays horizontally scalable). Degrades gracefully when the CLI or the
credentials are missing — the rest of the tab keeps working.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CHAT_MODEL = os.environ.get("CLAUDE_CHAT_MODEL", "claude-sonnet-4-6")
CHAT_TIMEOUT = float(os.environ.get("CLAUDE_CHAT_TIMEOUT", "240"))
MAX_HISTORY = 24           # turns kept in the prompt
MAX_TURN_CHARS = 4000      # each turn clipped (notebook answers can be long)

_PREAMBLE = (
    "Ты — ассистент вкладки «Практика» студии Antimartingale. Домен: стратегия "
    "«Прикрытый Интрадей» (ПИ) Ильи Коровина — длинный синтетический стреддл "
    "(2 Колла − 1 Фьючерс), тету отбивает контр-трендовый интрадей-скальпинг по "
    "экспоненциальной сетке; правило трёх третей; максимальный убыток = уплаченная "
    "премия. Пользователь разбирает КОНКРЕТНЫЕ примеры конструкций (числа берёт из "
    "корпуса NotebookLM) и просит дальнейшие расчёты. Отвечай по-русски, кратко и "
    "по делу, считай в числах и показывай формулы, честно помечай допущения. "
    "Это образовательный разбор, НЕ инвестиционный совет."
)

_ROLE = {"q": "Пользователь", "a": "Ноутбук", "c": "Claude"}


def available() -> tuple[bool, str]:
    if shutil.which(CLAUDE_BIN) is None and not os.path.exists(CLAUDE_BIN):
        return False, (f"claude CLI not found ({CLAUDE_BIN}) — rebuild the container "
                       "with scripts/serve.sh")
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    if not os.path.exists(os.path.join(cfg, ".credentials.json")):
        return False, ("claude credentials not seeded — the container needs the "
                       "read-only /seed mount of the host ~/.claude (scripts/serve.sh)")
    return True, ""


def build_prompt(question: str, history: list[dict] | None = None,
                 construction: dict | None = None) -> str:
    parts = [_PREAMBLE]
    if construction:
        parts.append("ТЕКУЩАЯ КОНСТРУКЦИЯ (из калькулятора вкладки):\n"
                     + json.dumps(construction, ensure_ascii=False, indent=1))
    if history:
        lines = []
        for h in history[-MAX_HISTORY:]:
            role = _ROLE.get(h.get("role", ""), "Пользователь")
            who = f"{role} «{h['title']}»" if h.get("title") and role == "Ноутбук" else role
            lines.append(f"{who}: {(h.get('text') or '')[:MAX_TURN_CHARS]}")
        parts.append("ИСТОРИЯ ДИАЛОГА (вопросы пользователя, ответы ноутбуков NotebookLM "
                     "и твои прошлые ответы):\n" + "\n\n".join(lines))
    parts.append("ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n" + question)
    return "\n\n---\n\n".join(parts)


def chat(question: str, history: list[dict] | None = None,
         construction: dict | None = None) -> dict:
    """One stateless turn. Returns {answer, model} or {error}."""
    ok, why = available()
    if not ok:
        return {"error": why}
    prompt = build_prompt(question, history, construction)
    try:
        proc = subprocess.run([CLAUDE_BIN, "-p", "--model", CHAT_MODEL],
                              input=prompt, capture_output=True, text=True,
                              timeout=CHAT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "claude chat timed out"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:400] or "claude failed"}
    answer = proc.stdout.strip()
    if not answer:
        return {"error": "empty answer from claude"}
    return {"answer": answer, "model": CHAT_MODEL}
