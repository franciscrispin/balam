#!/usr/bin/env bash
# Report whether OpenCode, the bot, and the Mini App are up and healthy.
# Read-only and side-effect free — safe to run any time. Prints a status block;
# always exits 0 so the agent reads the lines rather than reacting to an exit code.
#
# Use this instead of ad-hoc pgrep/curl/ss/ps/cat probes: it is one allowlistable
# command that answers all of those at once.

set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_env
print_status
