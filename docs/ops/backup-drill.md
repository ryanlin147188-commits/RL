# Backup / Restore Drill SOP

Quarterly exercise to confirm the backup pipeline still produces a snapshot
that actually restores. Skipping drills is how teams discover their backups
were broken six months ago — only when they need them.

## Cadence

* **Quarterly** in staging — full down + restore + smoke.
* **After every major change** to: postgres version, SeaweedFS layout,
  Alembic migrations that touch data shape, `.env` schema.
* **Before any production restore** — never restore a snapshot you have
  not test-restored elsewhere first.

## Scope

The drill exercises `scripts/backup.sh` and `scripts/restore.sh` end-to-end
against a staging stack provisioned identically to production. The drill
**does not** test:

* DR across regions (run separately if S3 mirroring is enabled).
* Per-table partial restores (use `pg_restore -t` ad-hoc).
* Disaster recovery RTO targets (track via dedicated capacity tests).

## Steps

1. **Snapshot production-like data.** On staging, run the standard backup:

    ```sh
    BACKUP_KEY_FILE=~/.config/autotest/backup.key ./scripts/backup.sh
    ```

   Confirm the snapshot directory contains `postgres.dump.gz`,
   `seaweedfs.tar.gz`, `env.enc`, `manifest.json`, `SHA256SUMS`.

2. **Tear the stack down completely.**

    ```sh
    docker compose down -v
    ```

   `-v` is intentional — drop the volumes so the restore actually has to
   recreate state. A drill that skips this proves nothing.

3. **Bring the stack back up clean.**

    ```sh
    docker compose up -d postgres valkey seaweedfs
    ```

   Wait for healthchecks to go green.

4. **Run the restore against the snapshot.**

    ```sh
    ./scripts/restore.sh ./backups/<timestamp>/
    ```

   Note the `SHA256SUMS` verification line — if it fails the snapshot is
   the problem, not the restore.

5. **Smoke the application.** Open a browser, log in with a known account,
   open one project, run one testcase, view one report. If any of those
   fail, the drill failed; record what broke before recovering.

6. **Record the result** in `docs/ops/backup-drill-history.md` (append-only
   log). Include: date, who ran it, snapshot tag, smoke results, time-to-restore.

## Failure modes worth documenting

* **`SHA256SUMS` mismatch** → the snapshot is corrupted at rest. Common
  culprit: backup destination is a flaky network share. Move backups
  to a local-then-rsync flow.
* **`pg_restore` complains about ownership** → re-run with `--no-owner
  --no-privileges` (the script already passes these); if it still fails,
  the dump was taken from a postgres of a different major version.
* **SeaweedFS volume comes back empty** → the tarball captured the wrong
  directory. Check `SEAWEED_DATA_DIR` env var matches the container layout.
* **`alembic upgrade head` fails** → schema in dump is from a newer code
  revision than the deployed image. Pin the deploy to match before retrying.

## Restore time budget

On the reference staging stack (4 vCPU / 16 GB RAM / 50 GB postgres /
20 GB SeaweedFS), a full drill takes ~12 minutes:

| Step | Time |
|---|---|
| Backup | ~3 min |
| `docker compose down -v` + `up -d` | ~1 min |
| `pg_restore` | ~5 min |
| SeaweedFS untar | ~2 min |
| Smoke | ~1 min |

Production data orders of magnitude larger should plan for proportionally
longer restores; revisit this table during quarterly drills.
