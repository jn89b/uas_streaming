#!/usr/bin/env bash
# install_detect_service.sh
#
# Writes and enables a systemd unit that runs detect_stream.py at boot and
# restarts it if it exits. Installs nothing else.
#
# Lives in <repo>/useful_shell_scripts/; detect_stream.py is expected in <repo>/,
# one level up (override with SCRIPT=/path/to/detect_stream.py).
#
# Usage:
#     sudo ~/uas_streaming/useful_shell_scripts/install_detect_service.sh
#     sudo HEF=/home/cuav7/other.hef FPS=15 ~/uas_streaming/useful_shell_scripts/install_detect_service.sh
#     sudo ~/uas_streaming/useful_shell_scripts/install_detect_service.sh --uninstall
#
# Settings (override with environment variables):
#     SCRIPT        path to detect_stream.py      default: <repo>/detect_stream.py
#     HEF           path to the .hef              default: <home>/model.hef
#     OUT_SIZE      working size the model sees   default: 1280x720
#     PUBLISH_SIZE  size of the published stream  default: 960x540
#     LABELS        class names, e.g. "boat"      default: unset (COCO names for 80-class models, classN otherwise)
#     FPS           published frame rate          default: 30
#     EXTRA_ARGS    anything else, e.g. "--conf 0.5 --no-stamp"
#
# Afterwards:
#     journalctl -fu detect-stream          live log
#     sudo systemctl restart detect-stream  after editing detect_stream.py
#     sudo systemctl stop detect-stream     before running the script by hand

set -euo pipefail

SERVICE="detect-stream"
UNIT="/etc/systemd/system/${SERVICE}.service"

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo: sudo $0 $*" >&2
    exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "removed $SERVICE"
    exit 0
fi

RUN_USER="${SUDO_USER:-$USER}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$THIS_DIR/.." && pwd)"
SCRIPT="${SCRIPT:-$REPO_DIR/detect_stream.py}"

HEF="${HEF:-$RUN_HOME/model.hef}"
OUT_SIZE="${OUT_SIZE:-1280x720}"
PUBLISH_SIZE="${PUBLISH_SIZE:-960x540}"
LABELS="${LABELS:-}"
FPS="${FPS:-30}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
PYTHON="$(command -v python3)"
LABELS_ARG=""
[[ -n "$LABELS" ]] && LABELS_ARG="--labels \"${LABELS}\""

[[ -f "$SCRIPT" ]] || { echo "not found: $SCRIPT   (set SCRIPT=/path/to/detect_stream.py)" >&2; exit 1; }
[[ -f "$HEF" ]]    || { echo "not found: $HEF   (set HEF=/path/to/model.hef)" >&2; exit 1; }

cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=Hailo detection stream -> MediaMTX /detections
After=network-online.target docker.service docker-stream.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=$(dirname "$SCRIPT")
ExecStart=${PYTHON} ${SCRIPT} --hef "${HEF}" --out-size ${OUT_SIZE} --publish-size ${PUBLISH_SIZE} ${LABELS_ARG} --fps ${FPS} ${EXTRA_ARGS}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"

echo "installed $UNIT, running as $RUN_USER"
echo "  HEF=$HEF  OUT_SIZE=$OUT_SIZE  PUBLISH_SIZE=$PUBLISH_SIZE  LABELS=${LABELS:-<auto>}  FPS=$FPS"
sleep 3
systemctl --no-pager --lines=10 status "$SERVICE" || true
echo "follow the log with:   journalctl -fu $SERVICE"
