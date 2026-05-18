#!/bin/bash
# replica-init.sh — entrypoint for postgres-replica container.
#
# First run: waits for primary, runs pg_basebackup -R to initialize standby.
# Subsequent runs: data already exists, starts postgres directly in standby mode.
# Uses gosu to drop from root to the postgres OS user before exec'ing postgres.
set -e

PGDATA="${PGDATA:-/var/lib/postgresql/data}"

if [ -s "$PGDATA/PG_VERSION" ]; then
  echo "[replica] data directory exists, starting standby..."
  exec gosu postgres postgres
fi

echo "[replica] empty data dir, initializing from primary $PRIMARY_HOST:${PRIMARY_PORT:-5432} ..."

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

chown -R postgres:postgres "$PGDATA"
chmod 700 "$PGDATA"
echo "[replica] base backup complete, starting standby..."
exec gosu postgres postgres
