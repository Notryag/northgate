# Backup and restore

Status: implemented for the Compose deployment  
Last reviewed: 2026-07-17

## Scope

PostgreSQL is Northgate's durable source of truth. Redis contains reconstructible
rate counters, leases, route health, and cache entries; it is flushed after a
database restore instead of being restored from backup.

Database backups do not contain the plaintext credential encryption key, raw
application/operator keys, or deployment environment. Retain the secret-manager
versions and deployment configuration needed by the matching Northgate release.

## Create a backup

The Compose backup script uses the PostgreSQL container's matching `pg_dump`
version, creates a compressed custom-format archive from a consistent snapshot,
verifies that `pg_restore` can list it, and writes a SHA-256 checksum:

```sh
./scripts/compose-backup.sh
./scripts/compose-backup.sh /secure/backups/northgate-before-upgrade.dump
```

The default destination is `backups/northgate-<UTC timestamp>.dump`. Archive
and checksum permissions are set to `0600`. Copy both files to storage outside
the Northgate host and apply the retention policy appropriate to the request
ledger.

Backups are online and do not require stopping Northgate. Schedule restore
drills; a backup that has never been restored is not a verified recovery plan.

## Restore a backup

Restore is destructive. It stops the Compose application, verifies the checksum,
drops and recreates the target database, restores in one transaction, and
flushes Redis. The database-name confirmation prevents accidental execution:

```sh
export NORTHGATE_RESTORE_CONFIRM=northgate
./scripts/compose-restore.sh /secure/backups/northgate-before-upgrade.dump
```

Northgate remains stopped after restoration. Before restoring traffic:

```sh
docker compose run --rm --no-deps northgate alembic current
docker compose run --rm --no-deps northgate alembic heads
docker compose up -d --wait northgate
```

Use the application version matching the backup schema. If intentionally
restoring into a newer release, run its documented upgrade after the restore.
Confirm that the original `NORTHGATE_CREDENTIAL_ENCRYPTION_KEY` is present, then
exercise readiness and one non-production provider request.

## Restore drill database

The script supports a different Compose database name for drills:

```sh
export NORTHGATE_COMPOSE_DATABASE=northgate_restore_drill
export NORTHGATE_RESTORE_CONFIRM=northgate_restore_drill
export NORTHGATE_RESTORE_FLUSH_REDIS=false
./scripts/compose-restore.sh backups/northgate-<timestamp>.dump
```

Drop the drill database after verification. Never point a drill at a shared or
production PostgreSQL service.
