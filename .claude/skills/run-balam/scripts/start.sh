#!/usr/bin/env bash
# Bring up Balam: OpenCode (the agent) -> the bot -> the Mini App.
#
# Idempotent by design. Each process is a singleton, and a second copy doesn't
# help and actively hurts (a second bot poller = 409 Conflict; a second
# `opencode serve`/Vite just fails on its port). So for each one: if it is
# already up and healthy, leave it alone; only start what's down.
#
# Ordering matters for the first two: the backend's post_init hook blocks on
# OpenCode answering before it polls Telegram, so OpenCode must be healthy first.
# The Mini App is independent and can start whenever.
#
# Long-lived processes are detached (nohup + disown) so they outlive this script,
# which itself returns as soon as everything is ready (or a bounded timeout
# elapses). Logs go to /tmp; secrets are loaded into the environment, never printed.

set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_env

# Spawn a long-lived process fully detached from this script.
spawn() {  # spawn <logfile> <cmd...>
  local log="$1"; shift
  nohup "$@" >"$log" 2>&1 </dev/null &
  disown
}

# Poll a predicate until it succeeds or we time out. Quiet; returns 0/1.
wait_for() {  # wait_for <seconds> <predicate-fn>
  local deadline=$(( SECONDS + $1 )); local fn="$2"
  while [ "$SECONDS" -lt "$deadline" ]; do
    "$fn" && return 0
    sleep 1
  done
  return 1
}

# --- 1. OpenCode -----------------------------------------------------------
if oc_healthy; then
  echo "OpenCode already up — skipping."
else
  echo "Starting OpenCode at $OC_BASE ..."
  spawn "$OPENCODE_LOG" opencode serve --hostname "$(oc_host)" --port "$(oc_port)"
  if wait_for 30 oc_healthy; then
    echo "  OpenCode is healthy."
  else
    echo "  WARN: OpenCode not healthy after 30s — see $OPENCODE_LOG."
    echo "  (The bot gates on OpenCode, so it won't go live until this clears.)"
  fi
fi

# --- 2. Bot ----------------------------------------------------------------
if bot_409; then
  echo "Bot log shows 409 Conflict (a stale/rival poller). Run stop.sh, then start.sh again."
elif bot_live; then
  echo "Bot already up — skipping."
elif bot_running; then
  echo "Bot process already running — waiting for it to go live ..."
  wait_for 30 bot_live || echo "  WARN: bot still not live — see $BOT_LOG."
else
  echo "Starting the bot ..."
  spawn "$BOT_LOG" uv --directory "$REPO_ROOT/apps/backend" run balam
  if wait_for 45 bot_live; then
    echo "  Bot is live (long-polling Telegram)."
  elif bot_409; then
    echo "  ERROR: 409 Conflict — another instance owns this token. Run stop.sh first."
  else
    echo "  WARN: bot not live after 45s — see $BOT_LOG."
  fi
fi

# --- 3. Mini App -----------------------------------------------------------
# Detect by port, not process name: an existing bare `node .../vite` counts as up.
if fe_healthy; then
  echo "Mini App already up — skipping."
else
  echo "Starting the Mini App ..."
  ( cd "$REPO_ROOT" && spawn "$FRONTEND_LOG" bun run dev )
  if wait_for 20 fe_healthy; then
    echo "  Mini App is serving $FRONTEND_URL."
  else
    echo "  WARN: Mini App not answering 200 after 20s — see $FRONTEND_LOG."
  fi
fi

echo
print_status
