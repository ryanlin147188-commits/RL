#!/bin/bash
# replica-init.sh — entrypoint for postgres-replica container.
#
# First run: waits for primary, runs pg_basebackup -R to initialize standby.
# Subsequent runs: data already exists, starts postgres directly in standby mode.
set -e

PGDATA="${PGDATA:-/var/lib/postgresql/data}"

if [ -s "$PGDATA/PG_VERSION" ]; then
  echo "[replica] data directory exists, starting standby..."
  exec postgres
fi

echo "[replica] empty data dir, initializing from primary $PRIMARY_HOST:$PRIMARY_PORT ..."

until pg_isready -h "$PRIMARY_HOST" -p "${PRIMARY_PORT:-5432}" -U "$REPLICA_USER" -q 2>/dev/null; do
  echo "[replica] waiting for primary to be ready..."; sleep 3
done

export PGPASSWORD="$REPLICA_PASSWORD"
pg_basebackup \
  -h "$PRIMARY_HOST" \
  -p "${PRIMARY_PORT:-5432}" \
  -U "$REPLICA_USER" \
  -D "$PGDATA" \
  -P \
  --wal-method=stream \
  -R
unset PGPASSWORD

chmod 700 "$PGDATA"
echo "[replica] base backup complete, starting standby..."
exec postgres
