#!/usr/bin/env bash
# setup-cron.sh — install a daily backup cron job for AutoTest.
#
# Adds one crontab entry: daily at 03:00 (server local time).
# Running this script again is idempotent — it will not add a duplicate.
#
# Usage (run on the server, inside the project root):
#   ./scripts/setup-cron.sh
#   BACKUP_DEST=/srv/backups BACKUP_KEEP_DAYS=14 ./scripts/setup-cron.sh
#
# To remove the cron job:
#   crontab -l | grep -v 'autotest-backup' | crontab -

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BACKUP_DEST="${BACKUP_DEST:-$ROOT_DIR/backups}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
CRON_HOUR="${CRON_HOUR:-3}"
CRON_MINUTE="${CRON_MINUTE:-0}"
LOG_FILE="${LOG_FILE:-$ROOT_DIR/backups/cron.log}"

CRON_LINE="$CRON_MINUTE $CRON_HOUR * * *  cd $ROOT_DIR && BACKUP_DEST=$BACKUP_DEST BACKUP_KEEP_DAYS=$BACKUP_KEEP_DAYS bash $ROOT_DIR/scripts/backup.sh >> $LOG_FILE 2>&1  # autotest-backup"

echo "[setup-cron] project root : $ROOT_DIR"
echo "[setup-cron] backup dest  : $BACKUP_DEST"
echo "[setup-cron] retention    : ${BACKUP_KEEP_DAYS} days"
echo "[setup-cron] schedule     : ${CRON_HOUR}:$(printf '%02d' "$CRON_MINUTE") daily"
echo "[setup-cron] log file     : $LOG_FILE"

mkdir -p "$BACKUP_DEST"

# Idempotent: remove any previous autotest-backup line, then re-add
(crontab -l 2>/dev/null | grep -v '# autotest-backup'; echo "$CRON_LINE") | crontab -

echo "[setup-cron] cron job installed:"
crontab -l | grep 'autotest-backup'
echo "[setup-cron] done."
echo ""
echo "To run a backup immediately:"
echo "  cd $ROOT_DIR && BACKUP_DEST=$BACKUP_DEST bash scripts/backup.sh"
echo ""
echo "To remove the cron job:"
echo "  crontab -l | grep -v 'autotest-backup' | crontab -"
