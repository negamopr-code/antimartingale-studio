# Lean, portable runtime image for the FastAPI web service.
# Build from the repo root:  docker build -f deploy/Dockerfile -t antimg-web .
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    MPLCONFIGDIR=/tmp/mpl \
    ANTIMG_CACHE=/data/cache \
    ANTIMG_SIGNAL_DB=/data/signals.db \
    PORT=8000 \
    WEB_CONCURRENCY=4

WORKDIR /app

COPY requirements-web.txt .
RUN pip install -r requirements-web.txt

# NotebookLM CLI for the Practice tab — isolated venv so its deps never touch the app's.
# Auth comes from the shared profile bind-mounted at ~app/.notebooklm-mcp-cli (scripts/serve.sh);
# without the mount the tab degrades gracefully (the construction calculator still works).
RUN python -m venv /opt/nlmvenv && /opt/nlmvenv/bin/pip install notebooklm-mcp-cli
ENV NLM_BIN=/opt/nlmvenv/bin/nlm

# Claude Code CLI for the Practice tab's direct Claude chat (headless `claude -p`, no tools).
# OAuth credentials are SEEDED at start from the read-only /seed mount (deploy/entrypoint.sh) —
# never baked into the image. Without them the chat endpoint degrades gracefully.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/* /root/.npm
ENV CLAUDE_CONFIG_DIR=/home/app/.claude \
    DISABLE_AUTOUPDATER=1

COPY src/ ./src/
COPY deploy/entrypoint.sh /entrypoint.sh

# non-root + writable data volume
RUN chmod +x /entrypoint.sh && useradd -m app && mkdir -p /data && chown -R app:app /data /app
USER app
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/api/health').status==200 else 1)"

# entrypoint seeds claude credentials from /seed (if mounted) then execs gunicorn
# (process manager + uvicorn workers; scale via WEB_CONCURRENCY)
CMD ["/entrypoint.sh"]
