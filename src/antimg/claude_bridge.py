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
# Latest first — Fable 5 is the default; the UI offers the rest as a dropdown.
MODELS = ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
CHAT_MODEL = os.environ.get("CLAUDE_CHAT_MODEL", "claude-fable-5")
EXTRACT_MODEL = os.environ.get("CLAUDE_EXTRACT_MODEL", "claude-haiku-4-5")
CHAT_TIMEOUT = float(os.environ.get("CLAUDE_CHAT_TIMEOUT", "240"))
# Global Claude skills (doctrine files) — combinable context for the chat. In the container
# this is the read-only /seed mount of the operator's ~/.claude (see scripts/serve.sh).
SKILLS_DIR = os.environ.get("CLAUDE_SKILLS_DIR", os.path.expanduser("~/.claude/skills"))
MAX_HISTORY = 24           # turns kept in the prompt
MAX_TURN_CHARS = 4000      # each turn clipped (notebook answers can be long)
MAX_SKILL_CHARS = 16_000   # each skill doctrine clipped in the prompt

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


def list_skills() -> list[dict]:
    """Available skill doctrines: [{name, description}] — directories with a SKILL.md."""
    out = []
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return out
    for name in names:
        if name.startswith((".", "_")):
            continue
        path = os.path.join(SKILLS_DIR, name, "SKILL.md")
        if not os.path.isfile(path):
            continue
        desc = ""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line:                            # first non-empty line = the title
                        desc = line.lstrip("#").strip()[:160]
                        break
        except OSError:
            pass
        out.append({"name": name, "description": desc})
    return out


def load_skill(name: str) -> str | None:
    """SKILL.md content for one skill; None if unknown. Name validated against the
    real directory listing — no path components accepted."""
    if name not in {s["name"] for s in list_skills()}:
        return None
    try:
        with open(os.path.join(SKILLS_DIR, name, "SKILL.md"),
                  encoding="utf-8", errors="replace") as fh:
            return fh.read()[:MAX_SKILL_CHARS]
    except OSError:
        return None


def build_prompt(question: str, history: list[dict] | None = None,
                 construction: dict | None = None,
                 sources: list[dict] | None = None,
                 skills: list[dict] | None = None,
                 images: list[dict] | None = None) -> str:
    parts = [_PREAMBLE]
    if images:
        lst = "\n".join(f"- {i['path']}  (файл пользователя: {i.get('name', '?')})"
                        for i in images)
        parts.append(
            "ПРИЛОЖЕННЫЕ ИЗОБРАЖЕНИЯ — пользователь загрузил картинки (скриншоты брокера, "
            "доски опционов, слайды). ОБЯЗАТЕЛЬНО прочитай КАЖДЫЙ файл инструментом Read "
            "ПЕРЕД ответом и опирайся на их фактическое содержимое:\n" + lst)
    if skills:
        blocks = "\n\n".join(f"[Скилл /{s['name']}]\n{s['content']}" for s in skills)
        parts.append(
            "ВЫБРАННЫЕ ПОЛЬЗОВАТЕЛЕМ СКИЛЛЫ-ДОКТРИНЫ — применяй их правила, математику и "
            "анти-паттерны при ответе; при конфликте между скиллами явно это отметь:\n" + blocks)
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
    if sources:
        blocks = "\n\n".join(
            f"[Ноутбук «{s.get('title', '?')}»]\n{(s.get('answer') or '')[:MAX_TURN_CHARS]}"
            for s in sources)
        parts.append(
            "ПЕРВОИСТОЧНИКИ — ответы РАЗНЫХ ноутбуков NotebookLM на ТЕКУЩИЙ вопрос (каждый "
            "ноутбук видит только свой корпус материалов):\n" + blocks
            + "\n\nТвоя задача — КОМПИЛЯЦИЯ: сведи первоисточники в одну полную картину; "
            "объедини общее, явно отметь расхождения между ноутбуками и пробелы; при ключевых "
            "утверждениях указывай, из какого ноутбука они пришли; добавляй собственные расчёты "
            "где это помогает. Не выдумывай ничего сверх приведённого и истории диалога.")
    parts.append("ВОПРОС ПОЛЬЗОВАТЕЛЯ:\n" + question)
    return "\n\n---\n\n".join(parts)


def _run_claude(prompt: str, model: str, extra_args: list[str] | None = None) -> dict:
    ok, why = available()
    if not ok:
        return {"error": why}
    try:
        proc = subprocess.run([CLAUDE_BIN, "-p", "--model", model, *(extra_args or [])],
                              input=prompt, capture_output=True, text=True,
                              timeout=CHAT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "claude chat timed out"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout).strip()[:400] or "claude failed"}
    answer = proc.stdout.strip()
    if not answer:
        return {"error": "empty answer from claude"}
    return {"answer": answer, "model": model}


def chat(question: str, history: list[dict] | None = None,
         construction: dict | None = None, sources: list[dict] | None = None,
         skills: list[dict] | None = None, model: str | None = None,
         images: list[dict] | None = None) -> dict:
    """One stateless turn (optionally compiling notebook sources, with skill doctrines
    and attached pictures in context). {answer, model} | {error}. With images the Read
    tool is enabled (the ONLY tool) so the model actually sees the pixels."""
    prompt = build_prompt(question, history, construction, sources, skills, images)
    extra = ["--allowedTools", "Read"] if images else None
    return _run_claude(prompt, model or CHAT_MODEL, extra_args=extra)


_EXTRACT_PROMPT = (
    "Извлеки из текста ниже параметры опционной конструкции (варианты: синтетический стреддл "
    "«длинные коллы + короткие фьючерсы» ИЛИ классический стреддл «длинные коллы + длинные путы "
    "на одном страйке»). Верни ТОЛЬКО JSON-объект без какого-либо текста вокруг, с ключами:\n"
    '{"instrument": str|null, "s0": number|null, "strike": number|null, '
    '"n_calls": number|null, "n_puts": number|null, "n_futs": number|null, '
    '"premium": number|null, "put_premium": number|null, '
    '"dte_days": number|null, "multiplier": number|null, "iv": number|null, '
    '"comment": str}\n'
    "Где: s0 = цена фьючерса/базового актива при входе (ТЕКУЩАЯ цена, не страйк); strike = "
    "страйк опционов; premium = премия ОДНОГО колла в пунктах цены, put_premium = премия "
    "ОДНОГО пута в пунктах (не в деньгах — если даны $, пересчитай через мультипликатор и "
    "поясни в comment); n_calls/n_puts = число длинных коллов/путов; n_futs = число КОРОТКИХ "
    "фьючерсов (0 для классического стреддла); dte_days = дней до экспирации; multiplier = $ "
    "за пункт; iv = подразумеваемая волатильность долей (0.60 = 60%). null для всего, чего в "
    "тексте НЕТ — НЕ выдумывай. В comment одной-двумя фразами: какой это пример и что "
    "пришлось додумать/пересчитать.\n\nТЕКСТ:\n"
)


def _parse_extraction(res: dict) -> dict:
    if "error" in res:
        return res
    raw = res["answer"]
    i = raw.find("{")
    if i < 0:
        return {"error": "extraction returned no JSON"}
    try:
        data = json.loads(raw[i:raw.rfind("}") + 1])
    except Exception as exc:
        return {"error": f"extraction JSON parse: {exc}"}
    comment = str(data.pop("comment", "") or "")
    params = {k: v for k, v in data.items() if v is not None}
    return {"params": params, "comment": comment, "model": EXTRACT_MODEL}


def extract_construction(text: str) -> dict:
    """Pull construction parameters out of a notebook's textual example.
    Returns {params: {...}, comment} or {error}."""
    return _parse_extraction(_run_claude(_EXTRACT_PROMPT + text[:16_000], EXTRACT_MODEL))


def extract_construction_from_image(path: str) -> dict:
    """Same extraction, but from a user-uploaded PICTURE of the example (broker screenshot,
    option board photo, webinar slide). The headless CLI reads the image with its Read tool
    — the ONLY tool allowed — so the model actually sees the pixels."""
    prompt = (f"Сначала прочитай изображение по пути {path} инструментом Read. "
              "Затем выполни задание по его содержимому.\n\n" + _EXTRACT_PROMPT
              + "(текст = содержимое изображения выше)")
    return _parse_extraction(
        _run_claude(prompt, EXTRACT_MODEL, extra_args=["--allowedTools", "Read"]))
