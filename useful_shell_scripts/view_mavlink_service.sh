#!/usr/bin/env bash
set -euo pipefail

sudo systemctl status mavlink-router.service -l --no-pager

echo
echo "===== Full MAVLink Router logs ====="
sudo journalctl -u mavlink-router.service -b -n 100 -l --no-pager