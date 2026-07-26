#!/usr/bin/env bash
set -Eeuo pipefail

UNIT_FILE="$HOME/.config/systemd/user/audio-phone-speaker.service"

systemctl --user disable --now audio-phone-speaker.service 2>/dev/null || true
rm -f "$UNIT_FILE"
systemctl --user daemon-reload
systemctl --user reset-failed audio-phone-speaker.service 2>/dev/null || true

echo "Serviço removido: $UNIT_FILE"
