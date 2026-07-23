#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: provision-inspect-client.sh --base-url URL --source-key-file PATH --config-dir PATH [--expected-sha256 HEX] [--force]

Copies an existing raw Northgate operator key into a protected diagnostics-client
configuration. The raw key is never accepted as a command argument.
EOF
}

base_url=""
source_key_file=""
config_dir=""
expected_sha256=""
force=false

while (($#)); do
    case "$1" in
        --base-url)
            base_url="${2:-}"
            shift 2
            ;;
        --source-key-file)
            source_key_file="${2:-}"
            shift 2
            ;;
        --config-dir)
            config_dir="${2:-}"
            shift 2
            ;;
        --expected-sha256)
            expected_sha256="${2:-}"
            shift 2
            ;;
        --force)
            force=true
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! "${base_url}" =~ ^https?://[^[:space:]]+$ ]]; then
    echo "--base-url must be an HTTP or HTTPS URL" >&2
    exit 2
fi
if [[ -z "${source_key_file}" || ! -f "${source_key_file}" || -L "${source_key_file}" ]]; then
    echo "--source-key-file must be a regular, non-symlink file" >&2
    exit 2
fi
if [[ -z "${config_dir}" || "${config_dir}" == "/" || -L "${config_dir}" ]]; then
    echo "--config-dir must be an explicit directory other than /" >&2
    exit 2
fi
source_mode="$((8#$(stat -c '%a' -- "${source_key_file}")))"
if ((source_mode & 077)); then
    echo "Source key file must not be accessible by group or others" >&2
    exit 2
fi
key_size="$(stat -c '%s' -- "${source_key_file}")"
if ((key_size == 0 || key_size > 4096)); then
    echo "Source key file must contain between 1 and 4096 bytes" >&2
    exit 2
fi
actual_sha256="$(tr -d '\r\n' < "${source_key_file}" | sha256sum | cut -d ' ' -f 1)"
if [[ -n "${expected_sha256}" && ! "${expected_sha256}" =~ ^[[:xdigit:]]{64}$ ]]; then
    echo "--expected-sha256 must contain exactly 64 hexadecimal characters" >&2
    exit 2
fi
if [[ -n "${expected_sha256}" && "${actual_sha256}" != "${expected_sha256,,}" ]]; then
    echo "Operator key SHA-256 does not match --expected-sha256" >&2
    exit 2
fi

key_path="${config_dir%/}/operator-key"
env_path="${config_dir%/}/inspect.env"
if [[ "${force}" != "true" && ( -e "${key_path}" || -e "${env_path}" ) ]]; then
    echo "Client configuration already exists; use --force only for an intentional rotation" >&2
    exit 2
fi

install -d -m 700 -- "${config_dir}"
install -m 600 -- "${source_key_file}" "${key_path}"
umask 077
{
    printf 'NORTHGATE_INSPECT_BASE_URL=%q\n' "${base_url%/}"
    printf 'NORTHGATE_INSPECT_OPERATOR_KEY_FILE=%q\n' "${key_path}"
    printf 'NORTHGATE_INSPECT_TIMEOUT_SECONDS=30\n'
} > "${env_path}"
chmod 600 -- "${env_path}"

echo "Diagnostics client configuration written to ${env_path}"
echo "Load it with: set -a; source ${env_path}; set +a"
