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
#     sudo HEF=/home/cuav7/hailo26/model.hef ~/uas_streaming/useful_shell_scripts/install_detect_service.sh
#     sudo HEF=... FPS=15 PUBLISH_SIZE=640x360 ~/uas_streaming/useful_shell_scripts/install_detect_service.sh
#     sudo ~/uas_streaming/useful_shell_scripts/install_detect_service.sh --uninstall
#
# Settings (environment variables):
#     HEF           path to the .hef              default: <home>/model.hef
#     WORK_SIZE     resolution the model sees     default: 1280x720  (OUT_SIZE accepted as alias)
#     PUBLISH_SIZE  published resolution          default: 960x540
#     FPS           published frame rate          default: 30
#     LABELS        class names, e.g. "boat"      default: unset (COCO for 80-class models, classN otherwise)
#     EXTRA_ARGS    anything else, e.g. "--conf 0.5 --no-stamp"   (see: python3 detect_stream.py --help)
#     SCRIPT        path to detect_stream.py      default: <repo>/detect_stream.py
#
# Afterwards:
#     journalctl -fu detect-stream          live log
#     sudo systemctl restart detect-stream  after editing the code
#     sudo systemctl stop detect-stream     before running the script by hand (the HAT is single-user)

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
WORK_SIZE="${WORK_SIZE:-${OUT_SIZE:-1280x720}}"
PUBLISH_SIZE="${PUBLISH_SIZE:-960x540}"
FPS="${FPS:-30}"
LABELS="${LABELS:-}"
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
ExecStart=${PYTHON} ${SCRIPT} --hef "${HEF}" --work-size ${WORK_SIZE} --publish-size ${PUBLISH_SIZE} ${LABELS_ARG} --fps ${FPS} ${EXTRA_ARGS}
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
echo "  HEF=$HEF  WORK_SIZE=$WORK_SIZE  PUBLISH_SIZE=$PUBLISH_SIZE  LABELS=${LABELS:-<auto>}  FPS=$FPS  EXTRA_ARGS=${EXTRA_ARGS:-<none>}"
sleep 3
systemctl --no-pager --lines=10 status "$SERVICE" || true
echo "follow the log with:   journalctl -fu $SERVICE"
