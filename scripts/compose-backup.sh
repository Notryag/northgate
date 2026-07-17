#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
caller_dir="${PWD}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"

if [[ -n "${1:-}" ]]; then
    if [[ "${1}" = /* ]]; then
        output="${1}"
    else
        output="${caller_dir}/${1}"
    fi
else
    output="${repo_root}/backups/northgate-${timestamp}.dump"
fi

database="${NORTHGATE_COMPOSE_DATABASE:-northgate}"
database_user="${NORTHGATE_COMPOSE_DATABASE_USER:-northgate}"
output_dir="$(dirname -- "${output}")"
output_name="$(basename -- "${output}")"

mkdir -p -- "${output_dir}"
if [[ -e "${output}" || -e "${output}.sha256" ]]; then
    echo "Refusing to overwrite existing backup: ${output}" >&2
    exit 1
fi

temporary="$(mktemp "${output}.tmp.XXXXXX")"
cleanup() {
    rm -f -- "${temporary}"
}
trap cleanup EXIT

cd -- "${repo_root}"
docker compose exec -T postgres pg_dump \
    --username="${database_user}" \
    --dbname="${database}" \
    --format=custom \
    --compress=9 \
    --no-owner \
    --no-privileges >"${temporary}"
docker compose exec -T postgres pg_restore --list <"${temporary}" >/dev/null

chmod 600 "${temporary}"
mv -- "${temporary}" "${output}"
(
    cd -- "${output_dir}"
    sha256sum -- "${output_name}" >"${output_name}.sha256"
)
chmod 600 "${output}.sha256"
trap - EXIT

echo "${output}"
