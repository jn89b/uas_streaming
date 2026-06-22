#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="setup-mavlink-router.service"

echo "===== MAVLink Router Service Status ====="
sudo systemctl status "${SERVICE_NAME}" -l --no-pager || true

echo
echo "===== Full MAVLink Router Logs ====="
sudo journalctl -u "${SERVICE_NAME}" -b -n 100 -l --no-pager