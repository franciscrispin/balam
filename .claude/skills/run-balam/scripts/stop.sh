#!/usr/bin/env bash
# Gracefully shut Balam down in reverse of startup order: bot -> OpenCode -> Mini App.
#
# Always SIGTERM, never SIGKILL: the backend installs SIGINT/SIGTERM handlers and
# runs a post_shutdown hook that closes the OpenCode HTTP client and the SQLite
# session store cleanly. A hard kill skips that and can leave the SQLite file
# mid-write. We send TERM, wait briefly, and only escalate to KILL if a process
# genuinely refuses to exit.

set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# Send SIGTERM to anything matching <pattern>; wait up to <secs>; escalate to
# SIGKILL only if still alive. Quiet when there's nothing to kill.
term_wait() {  # term_wait <label> <secs> <pattern...>
  local label="$1"; local secs="$2"; shift 2
  if ! pgrep -f "$*" >/dev/null 2>&1; then
    echo "$label: not running."
    return 0
  fi
  echo "$label: sending SIGTERM ..."
  pkill -TERM -f "$*" 2>/dev/null || true
  local deadline=$(( SECONDS + secs ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    pgrep -f "$*" >/dev/null 2>&1 || { echo "  stopped."; return 0; }
    sleep 1
  done
  echo "  still alive after ${secs}s — escalating to SIGKILL."
  pkill -KILL -f "$*" 2>/dev/null || true
}

# 1. Bot first (so it deregisters its poller and runs post_shutdown cleanly).
term_wait "Bot     " 10 'run balam'
# 2. Then the agent.
term_wait "OpenCode" 8 'opencode serve'
# 3. Mini App — independent. Match both `bun run dev` and a bare Vite under this repo.
term_wait "Mini App" 6 'apps/frontend/node_modules/.bin/vite'
pkill -TERM -f 'bun run.*dev' 2>/dev/null || true

echo
print_status
