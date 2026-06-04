#!/usr/bin/env bash
# Open the headed browser with a NAMED PERSISTENT PROFILE — a real on-disk
# user-data-dir, so cookies, localStorage, IndexedDB and service workers
# survive across runs. Use this ONLY when the user explicitly asks to reuse a
# saved login (e.g. "open my logged-in account on X", "use the <name> profile").
# The default browser the skill uses is a fresh, throwaway one with no
# persisted state — never switch to a profile on your own initiative.
#
# Usage: profile.sh <profile-name> [url]
#   profile.sh <name> https://app.example.com
#   profile.sh <name>                      # open the profile, no url
#
# First run with a given name: the browser opens logged out. The USER logs in
# in the noVNC window — however that site does it (password, code, QR, SSO) —
# you do not type their credentials. After that the login persists; later
# `profile.sh <same-name>` is already logged in.
# (Site-specific login/upload quirks belong in this skill's references/ dir,
# not here — this script and SKILL.md stay site-agnostic.)
#
# Profiles live ON THE VM under $BROWSER_USE_PROFILE_ROOT
# (default ~/.cache/browser-use/profiles/<name>). No limit beyond disk space.
# One browser at a time per profile — Chromium locks the directory; if it
# complains the profile is in use, a previous browser did not close cleanly:
# run `playwright-cli close-all` (or `kill-all`) and retry.
# To log out / reset a profile: `rm -rf` its directory.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: profile.sh <profile-name> [url]" >&2
  exit 2
fi

NAME="$1"
URL="${2:-}"

# Keep the name to a single safe path segment so it can't escape the root.
case "$NAME" in
  */*|*\\*|.*|"") echo "profile.sh: invalid profile name '$NAME' (one path segment, no slashes, no leading dot)" >&2; exit 2 ;;
esac

ROOT="${BROWSER_USE_PROFILE_ROOT:-$HOME/.cache/browser-use/profiles}"
DIR="$ROOT/$NAME"
mkdir -p "$DIR"

export DISPLAY="${DISPLAY:-:${DISPLAY_NUM:-99}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/ensure.sh"

echo "profile.sh: opening browser with persistent profile '$NAME' ($DIR)"
if [ -n "$URL" ]; then
  exec playwright-cli open --headed --persistent --profile "$DIR" "$URL"
else
  exec playwright-cli open --headed --persistent --profile "$DIR"
fi
