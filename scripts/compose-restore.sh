#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: NORTHGATE_RESTORE_CONFIRM=<database> $0 <backup.dump>" >&2
    exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
database="${NORTHGATE_COMPOSE_DATABASE:-northgate}"
database_user="${NORTHGATE_COMPOSE_DATABASE_USER:-northgate}"
confirmation="${NORTHGATE_RESTORE_CONFIRM:-}"

if [[ "${confirmation}" != "${database}" ]]; then
    echo "Restore refused: NORTHGATE_RESTORE_CONFIRM must equal ${database}" >&2
    exit 1
fi

if [[ "${1}" = /* ]]; then
    archive="${1}"
else
    archive="${PWD}/${1}"
fi
if [[ ! -f "${archive}" ]]; then
    echo "Backup archive not found: ${archive}" >&2
    exit 1
fi

archive_dir="$(dirname -- "${archive}")"
archive_name="$(basename -- "${archive}")"
if [[ -f "${archive}.sha256" ]]; then
    (
        cd -- "${archive_dir}"
        sha256sum --check -- "${archive_name}.sha256"
    )
else
    echo "Restore refused: checksum file not found: ${archive}.sha256" >&2
    exit 1
fi

cd -- "${repo_root}"
docker compose stop northgate >/dev/null
docker compose exec -T postgres dropdb \
    --username="${database_user}" \
    --force \
    --if-exists \
    "${database}"
docker compose exec -T postgres createdb \
    --username="${database_user}" \
    --template=template0 \
    --encoding=UTF8 \
    "${database}"
docker compose exec -T postgres pg_restore \
    --username="${database_user}" \
    --dbname="${database}" \
    --single-transaction \
    --exit-on-error \
    --no-owner \
    --no-privileges <"${archive}"

if [[ "${NORTHGATE_RESTORE_FLUSH_REDIS:-true}" == "true" ]]; then
    docker compose exec -T redis redis-cli FLUSHDB >/dev/null
fi

echo "Restored ${archive} into ${database}. Northgate remains stopped."
