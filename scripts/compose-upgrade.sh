#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
application_probe_container="${NORTHGATE_APPLICATION_PROBE_CONTAINER:?Set NORTHGATE_APPLICATION_PROBE_CONTAINER before upgrading}"

cd -- "${repo_root}"
compose=(docker compose -f docker-compose.yml -f docker-compose.platform.yml)
upgrade_services=(northgate)
case "${NORTHGATE_SETTLEMENT_OUTBOX_ENABLED:-false}" in
    1 | [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Oo][Nn]) outbox_enabled=true ;;
    *) outbox_enabled=false ;;
esac
if [[ "${outbox_enabled}" == "true" ]]; then
    compose+=(--profile settlement-worker)
    upgrade_services+=(settlement-worker)
fi

"${script_dir}/validate-compose-topology.sh"
"${compose[@]}" build "${upgrade_services[@]}"
backup_path="$("${script_dir}/compose-backup.sh" "${1:-}")"

"${compose[@]}" stop "${upgrade_services[@]}" >/dev/null
if ! "${compose[@]}" run --rm --no-deps northgate alembic upgrade head; then
    echo "Migration failed. Northgate remains stopped. Backup: ${backup_path}" >&2
    exit 1
fi
if ! "${compose[@]}" up -d --wait "${upgrade_services[@]}"; then
    echo "Upgrade applied, but readiness failed. Northgate requires investigation." >&2
    echo "Pre-upgrade backup: ${backup_path}" >&2
    exit 1
fi
NORTHGATE_APPLICATION_PROBE_CONTAINER="${application_probe_container}" \
    "${script_dir}/probe-application-connectivity.sh"

echo "Upgrade complete. Pre-upgrade backup: ${backup_path}"
