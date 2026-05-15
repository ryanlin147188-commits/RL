#!/usr/bin/env bash
# AutoTest — backup script.
#
# Snapshots the live docker compose stack into a single timestamped directory
# containing:
#
#   postgres.dump.gz   logical pg_dump (custom format, restorable via pg_restore)
#   seaweedfs.tar.gz   tarball of the SeaweedFS volume directory inside the
#                      container (object storage data)
#   env.enc            .env encrypted with AES-256-CBC + PBKDF2 (key file
#                      sourced from $BACKUP_KEY_FILE)
#   manifest.json      version + timestamp + git revision
#   SHA256SUMS         integrity manifest, verified by restore.sh
#
# Usage:
#   ./scripts/backup.sh                         # write to ./backups/<timestamp>/
#   BACKUP_DEST=/srv/snaps ./scripts/backup.sh  # override destination
#   BACKUP_KEEP_DAYS=14 ./scripts/backup.sh     # keep 14 days (default: 7)
#   S3_BUCKET=mybucket ./scripts/backup.sh      # also sync to s3://...
#
# Designed to be re-runnable from cron / systemd timer.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# ── 0) Sanity ──────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo "ERROR: .env not found at $ROOT_DIR/.env" >&2
    exit 1
fi

# Source DB creds from .env without exporting noise.
# shellcheck source=/dev/null
set -a; . ./.env; set +a

DB_USER="${DB_USER:-admin}"
DB_NAME="${DB_NAME:-autotest_db}"
PG_CONTAINER="${PG_CONTAINER:-autotest-postgres}"
SEAWEED_CONTAINER="${SEAWEED_CONTAINER:-autotest-seaweedfs}"
SEAWEED_DATA_DIR="${SEAWEED_DATA_DIR:-/data}"

TS="$(date +%Y%m%d-%H%M%S)"
DEST_BASE="${BACKUP_DEST:-./backups}"
DEST="$DEST_BASE/$TS"
mkdir -p "$DEST"

echo "[backup] target dir: $DEST"

# ── 1) PostgreSQL ──────────────────────────────────────────────────────────
echo "[backup] dumping postgres ($PG_CONTAINER) ..."
docker exec -t "$PG_CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB_NAME" \
    | gzip > "$DEST/postgres.dump.gz"

# ── 2) SeaweedFS ───────────────────────────────────────────────────────────
echo "[backup] tarballing SeaweedFS data ($SEAWEED_CONTAINER:$SEAWEED_DATA_DIR) ..."
docker exec -t "$SEAWEED_CONTAINER" \
    tar -C "$SEAWEED_DATA_DIR" -czf - . > "$DEST/seaweedfs.tar.gz"

# ── 3) .env (encrypted) ────────────────────────────────────────────────────
if [ -n "${BACKUP_KEY_FILE:-}" ] && [ -f "$BACKUP_KEY_FILE" ]; then
    echo "[backup] encrypting .env with key file $BACKUP_KEY_FILE ..."
    openssl enc -aes-256-cbc -salt -pbkdf2 \
        -in .env -out "$DEST/env.enc" \
        -pass "file:$BACKUP_KEY_FILE"
else
    echo "[backup] WARNING: BACKUP_KEY_FILE not set -- skipping .env (set this in production)" >&2
fi

# ── 4) Manifest + checksums ───────────────────────────────────────────────
GIT_REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat > "$DEST/manifest.json" <<EOF
{
  "schema": "autotest-backup/v1",
  "version": "$(git describe --tags --always 2>/dev/null || echo unknown)",
  "git_rev": "$GIT_REV",
  "timestamp": "$TS",
  "host": "$(hostname)"
}
EOF

(
    cd "$DEST"
    sha256sum -- *.gz *.json $( [ -f env.enc ] && echo env.enc || true ) \
        > SHA256SUMS
)

echo "[backup] integrity:"
cat "$DEST/SHA256SUMS"

# ── 5) Optional S3 mirror ─────────────────────────────────────────────────
if [ -n "${S3_BUCKET:-}" ]; then
    if command -v aws >/dev/null 2>&1; then
        echo "[backup] mirroring to s3://$S3_BUCKET/autotest-backup/$TS/ ..."
        aws s3 sync "$DEST" "s3://$S3_BUCKET/autotest-backup/$TS/"
    else
        echo "[backup] aws CLI not found -- skipping S3 mirror" >&2
    fi
fi

echo "[backup] done -> $DEST"

# ── 6) Retention cleanup ──────────────────────────────────────────────────
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
if [ -d "$DEST_BASE" ]; then
    echo "[backup] pruning snapshots older than ${BACKUP_KEEP_DAYS} days in $DEST_BASE ..."
    find "$DEST_BASE" -maxdepth 1 -mindepth 1 -type d \
        -name '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]' \
        -mtime +"$BACKUP_KEEP_DAYS" \
        -exec rm -rf {} +
fi
