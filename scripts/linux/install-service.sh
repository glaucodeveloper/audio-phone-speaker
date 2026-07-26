#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/audio-phone-speaker.service"
PYTHON_BIN="${PYTHON_BIN:-python3}"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  echo "Erro: $PYTHON_BIN não encontrado." >&2
  exit 1
}

command -v adb >/dev/null 2>&1 || {
  echo "Erro: adb não encontrado. Instale Android platform-tools." >&2
  exit 1
}

if [[ ! -d "$PROJECT_DIR/.venv" ]]; then
  "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
fi

"$PROJECT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$PROJECT_DIR/.venv/bin/python" -m pip install \
  -r "$PROJECT_DIR/requirements-audio-sender.txt"

chmod +x \
  "$PROJECT_DIR/scripts/linux/run-sender.sh" \
  "$PROJECT_DIR/scripts/linux/install-service.sh" \
  "$PROJECT_DIR/scripts/linux/uninstall-service.sh"

ADB_DIR="$(dirname "$(command -v adb)")"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=Audio Phone Speaker sender
After=graphical-session.target pipewire.service pipewire-pulse.service pulseaudio.service
Wants=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/scripts/linux/run-sender.sh
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$ADB_DIR:$HOME/Android/Sdk/platform-tools:$HOME/Android/sdk/platform-tools:/opt/android-sdk/platform-tools:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

environment_names=()
for name in \
  XDG_RUNTIME_DIR \
  DBUS_SESSION_BUS_ADDRESS \
  PULSE_SERVER \
  DISPLAY \
  WAYLAND_DISPLAY
do
  if [[ -n "${!name:-}" ]]; then
    environment_names+=("$name")
  fi
done

if ((${#environment_names[@]})); then
  systemctl --user import-environment "${environment_names[@]}" || true
fi

systemctl --user daemon-reload
systemctl --user enable --now audio-phone-speaker.service
systemctl --user restart audio-phone-speaker.service

echo
systemctl --user status audio-phone-speaker.service --no-pager --full || true
echo
echo "Logs:"
echo "  journalctl --user -u audio-phone-speaker.service -f"
