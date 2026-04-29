#!/usr/bin/env bash
# AutoTest v1.0 -- restore script (RFC-11).
#
# Inverse of ./scripts/backup.sh. Restores a snapshot directory back into the
# running stack. Verifies SHA256SUMS first; aborts on any mismatch.
#
# Usage:
#   ./scripts/restore.sh ./backups/20260429-143000
#
# Behaviour:
#   1. Verifies SHA256SUMS in the snapshot directory.
#   2. Stops only the data services (backend / celery) so writes pause.
#   3. Drops + recreates the postgres database, then restores from dump.
#   4. Replaces SeaweedFS volume directory contents from the tarball.
#   5. Runs alembic upgrade head to land any new migrations on top.
#   6. Restarts services.
#
# Designed to be idempotent against a clean stack and safe to abort:
# any failure leaves the snapshot intact for retry.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ $# -lt 1 ]; then
    echo "usage: $0 <snapshot-dir>" >&2
    exit 64
fi
SRC="$1"
if [ ! -d "$SRC" ]; then
    echo "ERROR: snapshot dir does not exist: $SRC" >&2
    exit 66
fi

# shellcheck source=/dev/null
set -a; . ./.env; set +a

DB_USER="${DB_USER:-admin}"
DB_NAME="${DB_NAME:-autotest_db}"
PG_CONTAINER="${PG_CONTAINER:-autotest-postgres}"
SEAWEED_CONTAINER="${SEAWEED_CONTAINER:-autotest-seaweedfs}"
SEAWEED_DATA_DIR="${SEAWEED_DATA_DIR:-/data}"

# ── 1) Integrity ───────────────────────────────────────────────────────────
echo "[restore] verifying SHA256SUMS ..."
( cd "$SRC" && sha256sum -c SHA256SUMS )

# ── 2) Pause writers (keep DB + storage up) ────────────────────────────────
echo "[restore] stopping backend + celery ..."
docker compose stop backend celery >/dev/null

trap 'echo "[restore] FAILED -- restarting services"; docker compose start backend celery >/dev/null' EXIT

# ── 3) Postgres ────────────────────────────────────────────────────────────
echo "[restore] re-creating database $DB_NAME ..."
docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" -d postgres <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS "$DB_NAME";
CREATE DATABASE "$DB_NAME" OWNER "$DB_USER";
SQL

echo "[restore] loading postgres dump ..."
gunzip -c "$SRC/postgres.dump.gz" \
    | docker exec -i "$PG_CONTAINER" pg_restore -U "$DB_USER" -d "$DB_NAME" \
        --no-owner --no-privileges

# ── 4) SeaweedFS ───────────────────────────────────────────────────────────
echo "[restore] wiping + restoring SeaweedFS data ($SEAWEED_DATA_DIR) ..."
docker exec -i "$SEAWEED_CONTAINER" sh -c \
    "rm -rf $SEAWEED_DATA_DIR/* && mkdir -p $SEAWEED_DATA_DIR"
docker exec -i "$SEAWEED_CONTAINER" tar -C "$SEAWEED_DATA_DIR" -xzf - \
    < "$SRC/seaweedfs.tar.gz"

# ── 5) Schema migrations on top ────────────────────────────────────────────
echo "[restore] applying migrations (alembic upgrade head) ..."
docker compose start backend >/dev/null
sleep 5
docker compose exec -T backend alembic upgrade head || \
    echo "[restore] WARN: alembic upgrade reported issues -- inspect manually"

# ── 6) Resume ──────────────────────────────────────────────────────────────
echo "[restore] starting celery ..."
docker compose start celery >/dev/null
trap - EXIT

echo "[restore] done."
echo "[restore] post-checks:"
echo "    docker compose logs --tail=50 backend"
echo "    curl -fsS http://localhost:8000/healthz"
