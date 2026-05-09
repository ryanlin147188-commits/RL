#!/bin/sh
# Hermes sidecar entrypoint.
# Runs as uid 10000 (hermes). Tini is PID 1 — we just exec the supervisor.
set -eu

: "${HERMES_DATA_ROOT:=/opt/data}"

# Sanity check — fail fast if the data volume isn't mounted writable.
# (Volume 在 first-run 會繼承 image 內 /opt/data 的擁有權,但 host bind-mount 就不一定。)
if [ ! -d "$HERMES_DATA_ROOT" ]; then
    echo "[hermes] FATAL: HERMES_DATA_ROOT=$HERMES_DATA_ROOT does not exist" >&2
    exit 1
fi
if [ ! -w "$HERMES_DATA_ROOT" ]; then
    echo "[hermes] FATAL: HERMES_DATA_ROOT=$HERMES_DATA_ROOT is not writable by uid $(id -u)" >&2
    exit 1
fi

echo "[hermes] starting supervisor (uid=$(id -u) data_root=$HERMES_DATA_ROOT)"
exec python -u /opt/hermes/supervisor.py
