#!/usr/bin/env bash
# setup-replica.sh — one-time setup for EXISTING deployments.
#
# Run this once BEFORE starting postgres-replica and backup-cron services
# when postgres_data already exists (init scripts don't re-run on existing data).
#
# Usage:
#   ./scripts/setup-replica.sh
#
# Prerequisites:
#   - .env exists with REPLICA_USER and REPLICA_PASSWORD set
#   - autotest-postgres container is running

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env not found at $ROOT_DIR/.env" >&2
  exit 1
fi

set -a; . ./.env; set +a

REPLICA_USER="${REPLICA_USER:-replicator}"
PG_CONTAINER="${PG_CONTAINER:-autotest-postgres}"
DB_USER="${DB_USER:-admin}"

if [ -z "${REPLICA_PASSWORD:-}" ]; then
  echo "ERROR: REPLICA_PASSWORD is not set in .env" >&2
  echo "  Add it: echo \"REPLICA_PASSWORD=\$(openssl rand -hex 24)\" >> .env" >&2
  exit 1
fi

echo "[setup-replica] Creating replication user '$REPLICA_USER' on $PG_CONTAINER ..."

docker exec -i "$PG_CONTAINER" psql -U "$DB_USER" <<-EOSQL
  DO \$\$
  BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$REPLICA_USER') THEN
      CREATE USER $REPLICA_USER
        WITH REPLICATION LOGIN ENCRYPTED PASSWORD '$REPLICA_PASSWORD';
      RAISE NOTICE 'User % created.', '$REPLICA_USER';
    ELSE
      RAISE NOTICE 'User % already exists, skipping.', '$REPLICA_USER';
    END IF;
  END \$\$;
EOSQL

echo "[setup-replica] Adding pg_hba.conf entry and reloading..."

docker exec "$PG_CONTAINER" sh -c "
  grep -q 'replication.*$REPLICA_USER' /var/lib/postgresql/data/pg_hba.conf \
    && echo '[setup-replica] pg_hba.conf entry already exists, skipping.' \
    || (echo 'host replication $REPLICA_USER 0.0.0.0/0 scram-sha-256' \
        >> /var/lib/postgresql/data/pg_hba.conf \
        && echo '[setup-replica] pg_hba.conf entry added.')
  pg_ctl reload -D /var/lib/postgresql/data -s
"

echo "[setup-replica] Done. You can now start postgres-replica and backup-cron:"
echo "  docker compose up -d postgres-replica backup-cron"
