#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

[[ -x "$PYTHON" ]] || {
  echo "Ambiente virtual não encontrado: $PYTHON" >&2
  exit 1
}

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_SERVER="${PULSE_SERVER:-unix:$XDG_RUNTIME_DIR/pulse/native}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

for attempt in $(seq 1 60); do
  if pactl info >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" -eq 60 ]]; then
    echo "PipeWire/Pulse não ficou disponível em 30 segundos." >&2
    exit 1
  fi
  sleep 0.5
done

adb start-server
cd "$PROJECT_DIR"
exec "$PYTHON" -u "$PROJECT_DIR/audio_sender.py"
