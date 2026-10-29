#!/bin/sh
set -eu
# Synthetic sink only: exercises the real PulseAudio client without hardware.
apk add --no-cache pulseaudio >/dev/null
pulseaudio --daemonize=yes --exit-idle-time=-1 \
  --load="module-null-sink sink_name=linkwatch_ci" \
  --load="module-native-protocol-unix socket=/tmp/linkwatch-pulse.sock auth-anonymous=1"
export PULSE_SERVER=unix:/tmp/linkwatch-pulse.sock
trap 'pulseaudio --kill || true' EXIT
python -c 'from pathlib import Path; from app.alerts import create_sounds; create_sounds(Path("/tmp/linkwatch-sounds"))'
paplay --device=linkwatch_ci /tmp/linkwatch-sounds/down.wav
paplay --device=linkwatch_ci /tmp/linkwatch-sounds/recovery.wav
echo "Both generated sounds played to the synthetic PulseAudio sink"
