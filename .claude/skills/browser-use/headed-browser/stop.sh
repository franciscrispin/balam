#!/usr/bin/env bash
set -euo pipefail

DISPLAY_NUM=${DISPLAY_NUM:-99}
STATE_DIR="$HOME/.cache/headed-browser"

for f in websockify x11vnc xvfb; do
  pid_file="$STATE_DIR/$f.pid"
  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    kill "$(cat "$pid_file")" 2>/dev/null || true
    sleep 0.2
    kill -9 "$(cat "$pid_file")" 2>/dev/null || true
  fi
  rm -f "$pid_file"
done
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true
echo "Headed browser stack stopped."
