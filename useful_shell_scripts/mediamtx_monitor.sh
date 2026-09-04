#!/usr/bin/env bash
# Keeps the mediamtx container up. Run by mediamtx.service (see install_mediamtx_service.sh).
set -u

# Changes the dir to the folder that this script is
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

# This script will check docker compose ps (local docker compose) and see if mediamtx container is running
# If not, run it in the background and check again in 30 seconds
while true; do
    if [[ -z "$(docker compose ps -q --status running mediamtx 2>/dev/null)" ]]; then
        echo "mediamtx not running, starting it"
        docker compose up -d mediamtx
    fi
    sleep 30
done
