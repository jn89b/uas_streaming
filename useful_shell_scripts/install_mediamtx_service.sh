#!/usr/bin/env bash
# Installs mediamtx.service, which runs mediamtx_monitor.sh to keep the mediamtx container up.
# Run as the user that runs docker:  ./useful_shell_scripts/install_mediamtx_service.sh
set -euo pipefail

# Name of the service needed to restart and enable the service
SERVICE=mediamtx

# Either sudo or the user that ran the script
RUN_USER="${SUDO_USER:-$(id -un)}"

# Gets the directory that this install script is in so that there are no issues with users (/home/uav vs /home/cuav8)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Checks if Docker is installed
command -v docker >/dev/null || { echo "ERROR: docker not installed. Run ./useful_shell_scripts/install_docker.sh first."; exit 1; }

# Makes sure the user is in the docker group
[[ " $(id -nG "$RUN_USER") " == *" docker "* ]] || echo "WARNING: $RUN_USER is not in the docker group; the service will not be able to talk to docker."

# Makes the service in the systemd direcrtory
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=MediaMTX container monitor (uas_streaming)
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=$RUN_USER
ExecStart=/bin/bash $SCRIPT_DIR/mediamtx_monitor.sh
Restart=always
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

# Restarts the services, enables on boot, and starts.
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE.service"
sudo systemctl restart "$SERVICE.service"

# Prints the service output so the user can check for errors (There wont be any because this code is awesome)
sudo systemctl status "$SERVICE.service" --no-pager -l
echo "Logs: journalctl -u $SERVICE -f"
