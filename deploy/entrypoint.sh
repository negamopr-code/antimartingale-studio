#!/bin/sh
# Seed Claude CLI credentials from a READ-ONLY /seed mount of the operator's ~/.claude,
# then start the web server. Copying (not mounting) keeps the container's `claude` from
# ever writing back into the host's live config (same pattern as yt2nlm-web).
# Without /seed the Claude chat reports "credentials not seeded" and the rest of the
# studio (NotebookLM Q&A, calculators, backtests) works as before.
set -eu

mkdir -p "${CLAUDE_CONFIG_DIR:?}"
if [ -f /seed/.credentials.json ]; then
  cp /seed/.credentials.json "$CLAUDE_CONFIG_DIR/.credentials.json" \
    && echo "entrypoint: seeded claude credentials" \
    || echo "entrypoint: WARN could not seed credentials (Claude chat unavailable)"
else
  echo "entrypoint: no /seed credentials — Claude chat unavailable (mount /root/.claude:ro at /seed)"
fi

exec gunicorn antimg.web.api:app -k uvicorn.workers.UvicornWorker \
  -b "0.0.0.0:${PORT}" -w "${WEB_CONCURRENCY}" --timeout 900 \
  --access-logfile - --error-logfile -
