#!/usr/bin/env bash
set -euo pipefail

# This script lives in:
# /home/cuav1/uas_streaming/useful_shell_scripts/
#
# This service unit lives in:
# /home/cuav1/uas_streaming/setup_mavlink_router.service

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVICE_SRC="${REPO_DIR}/setup_mavlink_router.service"
SERVICE_NAME="setup-mavlink-router.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"

if [[ ! -f "${SERVICE_SRC}" ]]; then
    echo "ERROR: Could not find service file:"
    echo "  ${SERVICE_SRC}"
    exit 1
fi

echo "Installing service file..."
sudo install -m 644 "${SERVICE_SRC}" "${SERVICE_DST}"

echo "Reloading systemd..."
sudo systemctl daemon-reload

# Stop the package-provided MAVLink Router service so it does not
# compete for /dev/ttyACM0 or MAVLink UDP ports.
sudo systemctl disable --now mavlink-router.service 2>/dev/null || true

echo "Enabling custom MAVLink Router service..."
sudo systemctl enable "${SERVICE_NAME}"

echo "Restarting custom MAVLink Router service..."
sudo systemctl restart "${SERVICE_NAME}"

echo
echo "Service status:"
sudo systemctl status "${SERVICE_NAME}" -l --no-pager