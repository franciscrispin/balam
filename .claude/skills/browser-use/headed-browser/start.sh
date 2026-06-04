#!/usr/bin/env bash
# Start Xvfb + x11vnc + websockify/noVNC so agents can run a headed browser
# on this VM while the user watches from a browser via http://localhost:6080.
# Idempotent: re-running cleanly restarts everything.
set -euo pipefail

DISPLAY_NUM=${DISPLAY_NUM:-99}
DISPLAY_VAL=":${DISPLAY_NUM}"
VNC_PORT=${VNC_PORT:-5900}
NOVNC_PORT=${NOVNC_PORT:-6080}
WIDTH=${WIDTH:-1440}
HEIGHT=${HEIGHT:-900}
DEPTH=${DEPTH:-24}

STATE_DIR="$HOME/.cache/headed-browser"
NOVNC_DIR="$HOME/.local/share/novnc"
mkdir -p "$STATE_DIR"

kill_pidfile() {
  local pid_file="$1"
  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    kill "$(cat "$pid_file")" 2>/dev/null || true
    sleep 0.2
    kill -9 "$(cat "$pid_file")" 2>/dev/null || true
  fi
  rm -f "$pid_file"
}

for f in websockify x11vnc xvfb; do kill_pidfile "$STATE_DIR/$f.pid"; done
fuser -k "${VNC_PORT}/tcp" 2>/dev/null || true
fuser -k "${NOVNC_PORT}/tcp" 2>/dev/null || true
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true

# nohup + </dev/null so these survive the shell/SSH session that launched
# them going away (SIGHUP). Even if they do die, `ensure.sh` restarts them.
nohup Xvfb "$DISPLAY_VAL" -screen 0 "${WIDTH}x${HEIGHT}x${DEPTH}" -nolisten tcp -ac \
  > "$STATE_DIR/xvfb.log" 2>&1 < /dev/null &
echo $! > "$STATE_DIR/xvfb.pid"

for _ in $(seq 1 30); do
  [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
  sleep 0.1
done

# x11vnc -bg already daemonizes (fork + setsid), so it is HUP-immune on its own.
x11vnc -display "$DISPLAY_VAL" -rfbport "$VNC_PORT" \
  -localhost -nopw -forever -shared -quiet -bg \
  -o "$STATE_DIR/x11vnc.log"
sleep 0.3
pgrep -f "x11vnc -display ${DISPLAY_VAL} " | head -1 > "$STATE_DIR/x11vnc.pid"

nohup websockify --web "$NOVNC_DIR" "$NOVNC_PORT" "localhost:${VNC_PORT}" \
  > "$STATE_DIR/websockify.log" 2>&1 < /dev/null &
echo $! > "$STATE_DIR/websockify.pid"

sleep 0.5
cat <<EOF

Headed browser stack running:
  Xvfb display:  ${DISPLAY_VAL}  (${WIDTH}x${HEIGHT})
  VNC (local):   localhost:${VNC_PORT}
  noVNC viewer:  http://localhost:${NOVNC_PORT}/vnc.html

From your machine, forward port ${NOVNC_PORT} and open the noVNC URL.
To run a headed browser:
  DISPLAY=${DISPLAY_VAL} playwright-cli open --headed https://example.com

Logs: ${STATE_DIR}/{xvfb,x11vnc,websockify}.log
Stop: .claude/skills/browser-use/headed-browser/stop.sh
EOF
