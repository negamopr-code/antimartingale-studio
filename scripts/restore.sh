#!/usr/bin/env bash
# Idempotent bootstrap for the antimartingal project (see global CLAUDE.md rule).
# Recreates the venv + deps. Safe to re-run. The .venv lives in /workspace (bind-mounted,
# survives container rebuilds), so this is normally only needed after a fresh checkout.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "[restore] creating venv"
  "$PY" -m venv .venv
fi
echo "[restore] installing deps"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "[restore] sanity import"
.venv/bin/python -c "import numpy,pandas,matplotlib,scipy,yfinance; print('deps ok')"

echo "[restore] running headless tests"
MPLCONFIGDIR=/tmp/mplconfig .venv/bin/python -m pytest -q || true

cat <<'EOF'

[restore] done.
  Tests                     : .venv/bin/python -m pytest -q
  Web app (FastAPI+Plotly)  : .venv/bin/python scripts/run_web.py   -> http://127.0.0.1:8000
  Docker (portable deploy)  : docker compose -f deploy/docker-compose.yml up --build
  Desktop GUI (python3-tk)  : .venv/bin/python scripts/run_gui.py   (run on a host; not in the dev container)
EOF
