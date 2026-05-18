#!/bin/bash
# primary-init.sh — runs via /docker-entrypoint-initdb.d/ on FIRST init only.
# Creates the streaming replication user and adds pg_hba.conf entry.
# For EXISTING deployments (postgres_data already populated), use setup-replica.sh instead.
set -e

echo "[primary-init] Creating replication user '${REPLICA_USER:-replicator}'..."

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  DO \$\$
  BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${REPLICA_USER:-replicator}') THEN
      CREATE USER ${REPLICA_USER:-replicator}
        WITH REPLICATION LOGIN ENCRYPTED PASSWORD '${REPLICA_PASSWORD}';
    END IF;
  END \$\$;
EOSQL

echo "host replication ${REPLICA_USER:-replicator} 0.0.0.0/0 scram-sha-256" \
  >> "$PGDATA/pg_hba.conf"

echo "[primary-init] Done: replication user and pg_hba.conf entry created."
