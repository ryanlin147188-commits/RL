#!/usr/bin/env bash
# safe-rebuild.sh — rebuild Docker images without touching named volumes.
#
# Named volumes (postgres_data / seaweedfs_data) survive docker compose up --build.
# They are ONLY deleted by `docker compose down -v` — never call that accidentally.
#
# Workflow:
#   1. Pre-build backup  (abort if backup fails)
#   2. docker compose up -d --build  (NO -v; volumes preserved)
#   3. Wait for healthchecks to go green
#   4. Smoke-check /api/healthz
#
# Usage:
#   ./scripts/safe-rebuild.sh              # rebuild from local Dockerfiles
#   ./scripts/safe-rebuild.sh --no-backup  # skip pre-build backup (CI/dev only)
#
# Environment:
#   COMPOSE_FILE   path to docker-compose.yml (default: ./docker-compose.yml)
#   HEALTH_TIMEOUT seconds to wait for healthy (default: 120)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SKIP_BACKUP=0
for arg in "$@"; do
    [ "$arg" = "--no-backup" ] && SKIP_BACKUP=1
done

HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
COMPOSE_FILE="${COMPOSE_FILE:-./docker-compose.yml}"

log() { echo "[safe-rebuild] $*"; }
err() { echo "[safe-rebuild] ERROR: $*" >&2; }

# ── Guard ─────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    err ".env not found at $ROOT_DIR — run: docker compose --profile init run --rm bootstrap"
    exit 1
fi

# ── 1) Pre-build backup ───────────────────────────────────────────────────
if [ "$SKIP_BACKUP" -eq 0 ]; then
    log "taking pre-build snapshot ..."
    if ! bash "$ROOT_DIR/scripts/backup.sh"; then
        err "backup failed — aborting rebuild. Fix the backup issue first."
        exit 1
    fi
    log "snapshot complete."
else
    log "WARNING: --no-backup specified — skipping pre-build snapshot."
fi

# ── 2) Rebuild images (volumes are NOT touched) ───────────────────────────
log "rebuilding images (docker compose up -d --build) ..."
log "Named volumes postgres_data + seaweedfs_data are preserved."
docker compose -f "$COMPOSE_FILE" up -d --build

# ── 3) Wait for healthchecks ──────────────────────────────────────────────
log "waiting for services to become healthy (timeout: ${HEALTH_TIMEOUT}s) ..."
DEADLINE=$((SECONDS + HEALTH_TIMEOUT))
UNHEALTHY_SERVICES="postgres valkey seaweedfs backend"

while [ "$SECONDS" -lt "$DEADLINE" ]; do
    ALL_OK=1
    for svc in $UNHEALTHY_SERVICES; do
        STATUS="$(docker inspect --format='{{.State.Health.Status}}' \
            "$(docker compose -f "$COMPOSE_FILE" ps -q "$svc" 2>/dev/null)" 2>/dev/null || echo "missing")"
        if [ "$STATUS" != "healthy" ]; then
            ALL_OK=0
            break
        fi
    done
    if [ "$ALL_OK" -eq 1 ]; then
        log "all services healthy."
        break
    fi
    sleep 5
done

if [ "$ALL_OK" -eq 0 ]; then
    err "services did not become healthy within ${HEALTH_TIMEOUT}s."
    docker compose -f "$COMPOSE_FILE" ps
    exit 1
fi

# ── 4) Smoke check ────────────────────────────────────────────────────────
log "smoke-checking /api/healthz ..."
if curl -fsS --max-time 10 http://localhost/api/healthz > /dev/null 2>&1; then
    log "OK — /api/healthz responded."
else
    err "/api/healthz did not respond. Check: docker compose logs backend"
    exit 1
fi

log "rebuild complete. Data is intact."
log "  Volumes preserved: rl_tmp_postgres_data  rl_tmp_seaweedfs_data"
log "  To verify: docker compose ps && curl http://localhost/api/healthz"
