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

COPY src/ ./src/

# non-root + writable data volume
RUN useradd -m app && mkdir -p /data && chown -R app:app /data /app
USER app
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/api/health').status==200 else 1)"

# gunicorn process manager + uvicorn workers (scale workers via WEB_CONCURRENCY)
CMD ["sh", "-c", "gunicorn antimg.web.api:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT} -w ${WEB_CONCURRENCY} --timeout 900 --access-logfile - --error-logfile -"]
