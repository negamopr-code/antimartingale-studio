#!/usr/bin/env bash
# Build + (re)run the antimg-web container — Antimartingale studio on http://localhost:8090.
#
# Restart policy `unless-stopped` ⇒ the site is ALWAYS UP while Docker is up: it survives a
# container crash AND a Docker-daemon / host restart (only a manual `docker stop` keeps it down).
# Idempotent — safe to re-run; rebuilds the baked image from the current source and recreates the
# container (the image has NO /workspace bind, so a code change needs this rebuild to go live).
#
# Run from the repo root, the host, or inside the claude container (it uses the Docker socket;
# the build context is streamed to the daemon either way).
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

IMAGE="antimg-web:latest"
NAME="antimg-web"
PORT="${PORT:-8090}"

echo "[serve] building $IMAGE …"
docker build -f deploy/Dockerfile -t "$IMAGE" .

echo "[serve] (re)creating $NAME on :$PORT (--restart unless-stopped) …"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped \
  -p "${PORT}:8000" -v antimg-data:/data "$IMAGE" >/dev/null

sleep 5
docker ps --filter "name=$NAME" --format '[serve] {{.Names}}  {{.Status}}  {{.Ports}}'
echo "[serve] up → http://localhost:${PORT}/"
