#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
PYTHON="${AUDIO_PHONE_SPEAKER_PYTHON:-$PROJECT_DIR/.venv/bin/python}"

export PYTHONUNBUFFERED=1

for platform_tools in \
  "${ANDROID_HOME:-}/platform-tools" \
  "$HOME/Android/Sdk/platform-tools" \
  "$HOME/Android/sdk/platform-tools" \
  "/opt/android-sdk/platform-tools"
do
  if [[ -n "$platform_tools" && -d "$platform_tools" ]]; then
    export PATH="$platform_tools:$PATH"
  fi
done

if [[ ! -x "$PYTHON" ]]; then
  echo "Erro: ambiente virtual não encontrado em $PYTHON" >&2
  echo "Execute: $PROJECT_DIR/scripts/linux/install-service.sh" >&2
  exit 1
fi

if ! command -v adb >/dev/null 2>&1; then
  echo "Erro: adb não encontrado no PATH." >&2
  exit 1
fi

cd "$PROJECT_DIR"
echo "Projeto: $PROJECT_DIR"
echo "Python: $PYTHON"
echo "ADB: $(command -v adb)"

adb start-server
exec "$PYTHON" -u "$PROJECT_DIR/audio_sender.py"
