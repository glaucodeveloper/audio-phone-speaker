#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="${AUDIO_SPEAKER_SERVICE_NAME:-audio-speaker}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now "$SERVICE_NAME.service" >/dev/null 2>&1 || true
rm -f "$UNIT_DIR/$SERVICE_NAME.service"
systemctl --user daemon-reload
systemctl --user reset-failed "$SERVICE_NAME.service" >/dev/null 2>&1 || true

echo "Serviço $SERVICE_NAME removido."
