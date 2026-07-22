# Upgrade and rollback

Status: implemented for the Compose deployment  
Last reviewed: 2026-07-22

## Upgrade policy

Compose upgrades currently use a maintenance window. Northgate does not yet
claim rolling schema compatibility across versions. The upgrade script builds
the checked-out target version, creates a verified backup, stops the application,
applies the single Alembic head, and waits for readiness. Before any of those
changes, it validates that the merged Compose configuration attaches Northgate to
both its private network and `PLATFORM_INFRA_NETWORK`, and that the external
platform network exists:

```sh
git checkout <target release>
NORTHGATE_APPLICATION_PROBE_CONTAINER=dayboard-api ./scripts/compose-upgrade.sh
```

If the default host port is already occupied, override the Compose bind without
changing Northgate's container port:

```sh
NORTHGATE_APPLICATION_PROBE_CONTAINER=dayboard-api \
NORTHGATE_HTTP_BIND=127.0.0.1:18080 \
./scripts/compose-upgrade.sh
```

To select the backup destination:

```sh
NORTHGATE_APPLICATION_PROBE_CONTAINER=dayboard-api \
./scripts/compose-upgrade.sh /secure/backups/northgate-pre-release.dump
```

Before starting, review release notes and update deployment settings without
removing the prior secret versions. Confirm sufficient disk space for the dump
and retain the current application version or image for rollback.

If migration or readiness fails, the script leaves Northgate stopped and prints
the pre-upgrade backup path. Do not repeatedly rerun a failed migration without
understanding its state.

The application-container probe is mandatory. Set the container that exercises
Northgate from the real application network before every upgrade:

```sh
NORTHGATE_APPLICATION_PROBE_CONTAINER=dayboard-api ./scripts/compose-upgrade.sh
```

The script rejects a missing container setting before build, backup, migration,
or replacement. After Northgate readiness succeeds, it runs
`scripts/probe-application-connectivity.sh` inside that application container and
requires `http://northgate:8080/health/ready` to return the expected JSON. Override
the URL with `NORTHGATE_APPLICATION_PROBE_URL` when the Compose service name or
port differs. The application image must provide Python and `urllib.request`.

Import `deploy/prometheus/northgate-alerts.yml` into the deployment's Prometheus
rule configuration. Its ten-minute stale thresholds are conservative defaults;
set them above the maximum accepted provider request plus settlement duration.

When enabling durable settlement, deploy the `settlement-worker` profile in the
same release as the application. Northgate readiness remains `503` until a worker
heartbeat is visible, so a data-plane-only rollout cannot pass health checks.
Set `NORTHGATE_SETTLEMENT_OUTBOX_ENABLED=true` before running the upgrade script;
its topology preflight then requires the worker service and the upgrade starts,
stops, and builds both services as one unit.

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
