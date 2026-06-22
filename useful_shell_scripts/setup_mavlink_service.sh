#!/usr/bin/env bash
set -euo pipefail

# Script lives in: uas_streaming/useful_shell_scripts/
# main.conf lives in: uas_streaming/main.conf
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CONFIG_SRC="${REPO_DIR}/main.conf"
CONFIG_DST="/etc/mavlink-router/main.conf"

if [[ ! -f "${CONFIG_SRC}" ]]; then
    echo "ERROR: Could not find config:"
    echo "  ${CONFIG_SRC}"
    exit 1
fi

sudo install -D -m 644 "${CONFIG_SRC}" "${CONFIG_DST}"
mavlink-routerd -c /etc/mavlink-router/main.conf

echo "Copied MAVLink Router config:"
echo "  ${CONFIG_SRC}"
echo "to:"
echo "  ${CONFIG_DST}"