#!/usr/bin/env bash
set -u

SOURCE_NAME="${PHONE_MIC_SOURCE_NAME:-audio_phone_speaker_mic}"

if command -v pactl >/dev/null 2>&1 && pactl info >/dev/null 2>&1; then
  while IFS=$'\t' read -r module_id module_name module_args _; do
    [[ "$module_name" == "module-pipe-source" ]] || continue
    if [[ "$module_args" == *"source_name=$SOURCE_NAME"* ]]; then
      pactl unload-module "$module_id" >/dev/null 2>&1 || true
    fi
  done < <(pactl list modules short 2>/dev/null || true)
fi

if command -v adb >/dev/null 2>&1; then
  mapfile -t devices < <(adb devices | awk 'NR > 1 && $2 == "device" { print $1 }')
  for device in "${devices[@]}"; do
    for port in 5001 5002; do
      adb -s "$device" reverse --remove "tcp:$port" >/dev/null 2>&1 || true
    done
  done
fi
