#!/usr/bin/env bash
# Show the tail of a process log without an ad-hoc cat/grep (each of which would
# be its own permission prompt).
#
# Usage: logs.sh [opencode|bot|frontend] [lines]
#   no args -> last 40 lines of all three.
#   logs.sh bot 80 -> last 80 lines of the bot log.

set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

which="${1:-all}"
lines="${2:-40}"

show() {  # show <label> <file>
  echo "==== $1 ($2) ===="
  if [ -f "$2" ]; then tail -n "$lines" "$2"; else echo "(no log yet)"; fi
  echo
}

case "$which" in
  opencode) show "OpenCode" "$OPENCODE_LOG" ;;
  bot)      show "Bot"      "$BOT_LOG" ;;
  frontend) show "Mini App" "$FRONTEND_LOG" ;;
  all)
    show "OpenCode" "$OPENCODE_LOG"
    show "Bot"      "$BOT_LOG"
    show "Mini App" "$FRONTEND_LOG"
    ;;
  *) echo "usage: logs.sh [opencode|bot|frontend] [lines]"; exit 2 ;;
esac
