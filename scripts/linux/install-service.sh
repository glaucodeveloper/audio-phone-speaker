#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
SERVICE_NAME="${AUDIO_SPEAKER_SERVICE_NAME:-audio-phone-speaker}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/$SERVICE_NAME.service"
ENV_FILE="$PROJECT_DIR/.audio-speaker.env"
PYTHON_BIN="${PYTHON_BIN:-python3}"

fail() {
  printf 'Erro: %s\n' "$*" >&2
  exit 1
}

command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "$PYTHON_BIN não encontrado."
command -v adb >/dev/null 2>&1 || fail "adb não encontrado. Instale Android platform-tools."
command -v pactl >/dev/null 2>&1 || fail "pactl não encontrado. Instale pipewire-pulse ou pulseaudio-utils."

if [[ ! -d "$PROJECT_DIR/.venv" ]]; then
  "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
fi

"$PROJECT_DIR/.venv/bin/python" -m pip install --upgrade pip wheel setuptools
"$PROJECT_DIR/.venv/bin/python" -m pip install -r "$PROJECT_DIR/requirements-audio-sender.txt"

"$PROJECT_DIR/.venv/bin/python" - <<'PY'
import numpy
import soundcard
print("Dependências Python Linux: OK")
PY

cat > "$ENV_FILE" <<'EOF'
PYTHONUNBUFFERED=1
PHONE_MIC_VIRTUAL_ENABLED=1
PHONE_MIC_SET_DEFAULT=1
PHONE_MIC_SOURCE_NAME=audio_phone_speaker_mic
PHONE_MIC_DESCRIPTION="Audio Phone Microphone"
PHONE_MIC_SAMPLE_RATE=48000
PHONE_MIC_CHANNELS=1
EOF
chmod 600 "$ENV_FILE"

chmod +x \
  "$PROJECT_DIR/scripts/linux/run-sender.sh" \
  "$PROJECT_DIR/scripts/linux/cleanup-sender.sh" \
  "$PROJECT_DIR/scripts/linux/install-service.sh" \
  "$PROJECT_DIR/scripts/linux/uninstall-service.sh"

ADB_BIN="$(realpath "$(command -v adb)")"
ADB_DIR="$(dirname "$ADB_BIN")"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=Audio Phone Speaker duplex bridge
After=graphical-session.target pipewire.service pipewire-pulse.service
Wants=graphical-session.target pipewire.service pipewire-pulse.service
StartLimitIntervalSec=30
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=-$ENV_FILE
Environment=XDG_RUNTIME_DIR=%t
Environment=PULSE_SERVER=unix:%t/pulse/native
Environment=PATH=$ADB_DIR:$HOME/Android/Sdk/platform-tools:$HOME/Android/sdk/platform-tools:/opt/android-sdk/platform-tools:/usr/local/bin:/usr/bin:/bin
ExecStart=$PROJECT_DIR/scripts/linux/run-sender.sh
ExecStopPost=$PROJECT_DIR/scripts/linux/cleanup-sender.sh
Restart=always
RestartSec=3
TimeoutStartSec=45
TimeoutStopSec=15
KillSignal=SIGINT
KillMode=mixed
UMask=0077
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user reset-failed "$SERVICE_NAME.service" >/dev/null 2>&1 || true
systemctl --user enable --now "$SERVICE_NAME.service"

sleep 2
printf '\n'
systemctl --user status "$SERVICE_NAME.service" --no-pager --full || true
printf '\nLogs:\n  journalctl --user -u %s.service -f\n' "$SERVICE_NAME"
printf 'Microfone:\n  pactl list sources short | grep audio_phone_speaker_mic\n'
