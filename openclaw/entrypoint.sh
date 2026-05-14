#!/usr/bin/env sh
# OpenClaw sidecar entrypoint (Phase 3 scaffold).
# 目前只跑 supervisor.py (健康檢查 + provision endpoint);實際 openclaw daemon
# 等 Phase 3.5 + OAuth wiring 才 spawn。
set -eu

mkdir -p "${OPENCLAW_DATA_ROOT:-/opt/openclaw-data}"

exec python3 /opt/openclaw/supervisor.py
