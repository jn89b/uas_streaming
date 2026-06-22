#!/usr/bin/env bash
set -euo pipefail

# Script location:
# /home/cuav1/uas_streaming/useful_shell_scripts/setup_mavlink_router.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

CONFIG_SRC="${REPO_DIR}/main.conf"
CONFIG_DST="/etc/mavlink-router/main.conf"

if [[ ! -f "${CONFIG_SRC}" ]]; then
    echo "ERROR: Could not find config:"
    echo "  ${CONFIG_SRC}"
    exit 1
fi

# systemd runs this script as root, so do NOT use sudo here.
install -D -m 644 "${CONFIG_SRC}" "${CONFIG_DST}"

echo "Copied MAVLink Router config:"
echo "  ${CONFIG_SRC}"
echo "to:"
echo "  ${CONFIG_DST}"

# Replace this shell process with MAVLink Router so systemd tracks it properly.
exec /usr/bin/mavlink-routerd -c "${CONFIG_DST}"