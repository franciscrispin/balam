#!/usr/bin/env bash
# Make sure the headed-browser stack (Xvfb + x11vnc + websockify/noVNC) is up,
# and write the playwright-cli config so a newly opened browser window fills
# the whole Xvfb screen.
#
# Idempotent and safe to call at any time — including while a browser is
# already running: it starts the stack only if Xvfb is not already up (so it
# never kills a mid-session browser), and rewriting the config file does not
# affect an already-open browser, only the next `open`.
#
# Exposes no shell operators to the caller — run it as a single command so it
# can be allowlisted (`Bash(.../headed-browser/ensure.sh)`) and the agent
# never gets a permission prompt for it.
#
# Usage: ensure.sh
set -euo pipefail

DISPLAY_NUM=${DISPLAY_NUM:-99}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

if DISPLAY=":${DISPLAY_NUM}" xdpyinfo >/dev/null 2>&1; then
  echo "headed-browser: stack already running on :${DISPLAY_NUM} — noVNC at http://localhost:${NOVNC_PORT:-6081}/vnc.html"
else
  echo "headed-browser: stack not running — starting it"
  "$SCRIPT_DIR/start.sh"
fi

# Generate .playwright/cli.config.json so `playwright-cli open` launches a
# browser window that fills the current Xvfb framebuffer, and so the page
# viewport tracks the real window content area instead of a fixed emulated
# size (no letterboxing, no squish). Regenerated every run, so it always
# matches the current resolution — e.g. after restarting the stack with a
# different WIDTH/HEIGHT.
# Read all of xdpyinfo (awk prints at END) so the upstream process never gets
# SIGPIPE — that, with `pipefail`, would otherwise abort this script.
DIMS="$(DISPLAY=":${DISPLAY_NUM}" xdpyinfo 2>/dev/null | awk '/dimensions:/ {d=$2} END {print d}')" || DIMS=""
if [ -n "${DIMS:-}" ] && [[ "$DIMS" == *x* ]]; then
  W="${DIMS%x*}"
  H="${DIMS#*x}"
  CFG_DIR="$PROJECT_ROOT/.playwright"
  mkdir -p "$CFG_DIR"
  cat > "$CFG_DIR/cli.config.json" <<EOF
{
  "browser": {
    "launchOptions": { "args": ["--window-position=0,0", "--window-size=${W},${H}"] },
    "contextOptions": { "viewport": null }
  }
}
EOF
  echo "headed-browser: wrote $CFG_DIR/cli.config.json (browser window ${W}x${H}, viewport tracks the window)"
else
  echo "headed-browser: could not read framebuffer size from xdpyinfo — skipping cli.config.json (browser will use its default window size)" >&2
fi
