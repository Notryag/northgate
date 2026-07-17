#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"

cd -- "${repo_root}"
docker compose build northgate
backup_path="$("${script_dir}/compose-backup.sh" "${1:-}")"

docker compose stop northgate >/dev/null
if ! docker compose run --rm --no-deps northgate alembic upgrade head; then
    echo "Migration failed. Northgate remains stopped. Backup: ${backup_path}" >&2
    exit 1
fi
if ! docker compose up -d --wait northgate; then
    echo "Upgrade applied, but readiness failed. Northgate requires investigation." >&2
    echo "Pre-upgrade backup: ${backup_path}" >&2
    exit 1
fi

echo "Upgrade complete. Pre-upgrade backup: ${backup_path}"
