# Upgrade and rollback

Status: implemented for the Compose deployment  
Last reviewed: 2026-07-17

## Upgrade policy

Compose upgrades currently use a maintenance window. Northgate does not yet
claim rolling schema compatibility across versions. The upgrade script builds
the checked-out target version, creates a verified backup, stops the application,
applies the single Alembic head, and waits for readiness:

```sh
git checkout <target release>
./scripts/compose-upgrade.sh
```

If the default host port is already occupied, override the Compose bind without
changing Northgate's container port:

```sh
NORTHGATE_HTTP_BIND=127.0.0.1:18080 ./scripts/compose-upgrade.sh
```

To select the backup destination:

```sh
./scripts/compose-upgrade.sh /secure/backups/northgate-pre-release.dump
```

Before starting, review release notes and update deployment settings without
removing the prior secret versions. Confirm sufficient disk space for the dump
and retain the current application version or image for rollback.

If migration or readiness fails, the script leaves Northgate stopped and prints
the pre-upgrade backup path. Do not repeatedly rerun a failed migration without
understanding its state.

## Rollback

Production rollback is restore-based:

1. Keep Northgate stopped.
2. Restore the pre-upgrade archive using
   [the restore procedure](backup-and-restore.md).
3. Check out or select the prior Northgate version.
4. Restore the matching configuration and secret-manager versions.
5. Confirm `alembic current` matches that release, then start and wait for
   readiness.
6. Verify authentication, credential decryption, proxy traffic, usage writes,
   metrics, and traces before closing the incident.

Alembic downgrade is not a production rollback mechanism because reversing a
schema does not reconstruct deleted or transformed data.
