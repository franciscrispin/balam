#!/usr/bin/env bash
# Shared helpers for the run-balam scripts.
#
# Why this file exists: the start/stop/status/logs scripts all need the same few
# things — find the repo root, load the secrets from .env without ever printing
# them, work out where OpenCode is listening, and answer "is each process up and
# healthy?". Keeping that logic here means each entry-point script is a tiny,
# single-purpose command the user can allowlist by exact path (see SKILL.md).
#
# Not meant to be run directly — it is sourced by the other scripts.

set -uo pipefail

# --- locate the repo -------------------------------------------------------
# This file lives at <repo>/.claude/skills/run-balam/scripts/lib.sh, so the repo
# root is four directories up. Resolving it from BASH_SOURCE (not $PWD) means the
# scripts work no matter what directory they are invoked from.
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$_LIB_DIR/../../../.." && pwd)"

# --- logs ------------------------------------------------------------------
LOG_DIR="${BALAM_LOG_DIR:-/tmp}"
OPENCODE_LOG="$LOG_DIR/opencode-serve.log"
BOT_LOG="$LOG_DIR/balam-bot.log"
FRONTEND_LOG="$LOG_DIR/balam-miniapp.log"

# --- load .env -------------------------------------------------------------
# `set -a` auto-exports everything `source` reads, so child processes (opencode,
# the bot) inherit OPENCODE_SERVER_PASSWORD and friends. The secret is never
# echoed — it only ever lives in the environment. The .env is intentionally
# read-protected from the agent's Read tool; sourcing it at runtime in a shell is
# fine and is the sanctioned hands-off way to use it.
load_env() {
  set -a
  # shellcheck disable=SC1090
  [ -f "$REPO_ROOT/.env" ] && source "$REPO_ROOT/.env"
  set +a
}

# --- where is OpenCode? ----------------------------------------------------
# Derive host/port from OPENCODE_BASE_URL (default http://127.0.0.1:4096) so the
# scripts follow whatever the .env says rather than hard-coding the port.
OC_BASE="${OPENCODE_BASE_URL:-http://127.0.0.1:4096}"
oc_hostport() { printf '%s' "${OC_BASE#*://}"; }            # strips scheme -> host:port
oc_host() { local hp; hp="$(oc_hostport)"; printf '%s' "${hp%%:*}"; }
oc_port() { local hp; hp="$(oc_hostport)"; printf '%s' "${hp##*:}"; }

FRONTEND_URL="${BALAM_FRONTEND_URL:-http://localhost:5180}"

# --- health probes ---------------------------------------------------------
# Each returns 0 (healthy) / 1 (down or unhealthy) and is quiet; callers decide
# what to print. Curl is given a short timeout so a wedged port can't hang us.

# OpenCode is healthy when /doc says 401 without auth AND 200 with the Basic-auth
# password — exactly the handshake the backend performs on boot.
oc_healthy() {
  local user="${OPENCODE_SERVER_USERNAME:-opencode}"
  local no_auth with_auth
  no_auth="$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$OC_BASE/doc" 2>/dev/null)"
  with_auth="$(curl -s -m 3 -o /dev/null -w '%{http_code}' \
    -u "${user}:${OPENCODE_SERVER_PASSWORD:-}" "$OC_BASE/doc" 2>/dev/null)"
  [ "$no_auth" = "401" ] && [ "$with_auth" = "200" ]
}

# The bot is a single Telegram long-poller. "Running" = a process exists;
# "live" = its log reached "Application started" and is not stuck in a 409
# Conflict (two pollers fighting over getUpdates — the one failure that silently
# breaks the bot).
bot_running() { pgrep -f 'run balam' >/dev/null 2>&1; }
bot_409() { [ -f "$BOT_LOG" ] && grep -q '409 Conflict' "$BOT_LOG" 2>/dev/null; }
bot_live() {
  bot_running && [ -f "$BOT_LOG" ] \
    && grep -q 'Application started' "$BOT_LOG" 2>/dev/null && ! bot_409
}

# The frontend is healthy when port 5180 answers 200 — checked by URL, not by
# process name, because Vite may be running as a bare `node .../vite` that the
# usual `bun run dev` pattern would miss.
fe_healthy() {
  [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$FRONTEND_URL" 2>/dev/null)" = "200" ]
}

# --- status summary --------------------------------------------------------
# One human-readable block the agent can read instead of probing with ad-hoc
# cat/grep/ps/ss commands (each of which would otherwise be its own prompt).
print_status() {
  echo "Balam status:"
  if oc_healthy; then
    echo "  OpenCode  : UP    ($OC_BASE — 401 no-auth / 200 with-auth)"
  elif curl -s -m 3 -o /dev/null "$OC_BASE/doc" 2>/dev/null; then
    echo "  OpenCode  : UNHEALTHY (listening at $OC_BASE but auth handshake failed)"
  else
    echo "  OpenCode  : DOWN  (no listener at $OC_BASE)"
  fi

  if bot_409; then
    echo "  Bot       : BROKEN (409 Conflict — another poller owns this token; stop & restart)"
  elif bot_live; then
    echo "  Bot       : UP    (Application started — long-polling Telegram)"
  elif bot_running; then
    echo "  Bot       : STARTING (process up, not yet 'Application started')"
  else
    echo "  Bot       : DOWN"
  fi

  if fe_healthy; then
    echo "  Mini App  : UP    ($FRONTEND_URL — 200)"
  else
    echo "  Mini App  : DOWN  (no 200 at $FRONTEND_URL)"
  fi
}
