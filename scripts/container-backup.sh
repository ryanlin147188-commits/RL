#!/bin/sh
# container-backup.sh — runs inside backup-cron container.
# Backs up PostgreSQL (from replica) + SeaweedFS volume.
# Designed to be called by crond daily at 03:00.
set -e

TS=$(date +%Y%m%d-%H%M%S)
DEST="/backups/$TS"
mkdir -p "$DEST"

echo "[backup] === $TS ==="

# 1) PostgreSQL dump from replica (keeps load off primary)
echo "[backup] pg_dump from postgres-replica..."
pg_dump -h postgres-replica -U "$DB_USER" -Fc "$DB_NAME" \
  | gzip > "$DEST/postgres.dump.gz"

# 2) SeaweedFS volume (read-only mount)
echo "[backup] tarballing SeaweedFS data..."
tar -C /seaweedfs_data -czf "$DEST/seaweedfs.tar.gz" .

# 3) SHA256 integrity manifest
echo "[backup] generating checksums..."
cd "$DEST" && sha256sum postgres.dump.gz seaweedfs.tar.gz > SHA256SUMS

echo "[backup] done -> $DEST"
cat "$DEST/SHA256SUMS"

# 4) Retention: remove snapshots older than BACKUP_KEEP_DAYS
KEEP="${BACKUP_KEEP_DAYS:-7}"
echo "[backup] pruning snapshots older than $KEEP days..."
find /backups -maxdepth 1 -mindepth 1 -type d \
  -name '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]' \
  -mtime +"$KEEP" -exec rm -rf {} + || true
