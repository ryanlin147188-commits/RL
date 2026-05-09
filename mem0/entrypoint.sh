#!/bin/sh
# mem0 sidecar entrypoint。
# Runs as uid 10001 (mem0)。Tini 是 PID 1 — 我們只 exec proxy。
set -eu

: "${MEM0_SIDECAR_AUTH_TOKEN:?MEM0_SIDECAR_AUTH_TOKEN env required (run docker compose --profile init run --rm bootstrap)}"
: "${MEM0_PG_PASSWORD:?MEM0_PG_PASSWORD env required (run docker compose --profile init run --rm bootstrap)}"
: "${MEM0_PG_HOST:=mem0-postgres}"
: "${MEM0_PG_USER:=mem0}"
: "${MEM0_PG_DB:=mem0}"

echo "[mem0] starting proxy(uid=$(id -u) pg=${MEM0_PG_HOST}/${MEM0_PG_DB})"
exec python -u /opt/mem0/mem0_proxy.py
