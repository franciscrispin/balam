#!/usr/bin/env bash
# Shared machine facts + unit-file rendering for the Balam deploy scripts.
#
# systemd unit files are static: no variables, no includes, no way to say "this
# user's home". So the units in this directory are TEMPLATES (*.service.in) with
# @TOKEN@ placeholders, and the install scripts render them into
# /etc/systemd/system. That is what keeps deploy/ machine-independent instead of
# forking a per-machine copy of every unit.
#
# Every value below is DERIVED from the machine by default — the OS user running
# the install, its home, the checkout this script lives in, the uv/cloudflared on
# PATH. A deployment that matches those defaults needs no configuration at all.
# deploy/deploy.env (git-ignored) overrides any of them; see deploy.env.example.
#
# Sourced, never executed: `. "$(dirname "$0")/lib.sh"`.

# --- facts ------------------------------------------------------------------

balam_deploy_init() {
  BALAM_DEPLOY_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

  # The checkout being installed FROM is the one this script lives in — not a
  # configured path. An installer that could target a different checkout than the
  # one you are reading is a footgun, so this is deliberately not overridable.
  BALAM_REPO=$(cd -- "$BALAM_DEPLOY_DIR/.." && pwd)

  # shellcheck source=/dev/null
  [ -f "$BALAM_DEPLOY_DIR/deploy.env" ] && . "$BALAM_DEPLOY_DIR/deploy.env"

  BALAM_USER=${BALAM_USER:-$(id -un)}
  BALAM_HOME=${BALAM_HOME:-$(getent passwd "$BALAM_USER" | cut -d: -f6)}
  if [ -z "$BALAM_HOME" ]; then
    echo "cannot determine the home directory of user '$BALAM_USER' — set BALAM_HOME in deploy/deploy.env" >&2
    return 1
  fi

  # Where extra instances are checked out: alongside the primary checkout, so
  # balam-<name> sits next to balam.
  BALAM_INSTANCE_ROOT=${BALAM_INSTANCE_ROOT:-$(dirname "$BALAM_REPO")}
  # The instance checkout basename is "<primary basename>-<name>".
  BALAM_INSTANCE_PREFIX=${BALAM_INSTANCE_PREFIX:-$(basename "$BALAM_REPO")}

  BALAM_UV=${BALAM_UV:-$(command -v uv || echo "$BALAM_HOME/.local/bin/uv")}
  BALAM_BUN=${BALAM_BUN:-$(command -v bun || echo "$BALAM_HOME/.bun/bin/bun")}
  BALAM_CLOUDFLARED=${BALAM_CLOUDFLARED:-$(command -v cloudflared || echo /usr/local/bin/cloudflared)}
  BALAM_OPENCODE=${BALAM_OPENCODE:-$(command -v opencode || echo "$BALAM_HOME/.opencode/bin/opencode")}
  BALAM_OPENCODE_DB=${BALAM_OPENCODE_DB:-$BALAM_HOME/.local/share/opencode/balam.db}
  BALAM_GOPATH=${BALAM_GOPATH:-$BALAM_HOME/go}
  BALAM_UNIT_PATH=${BALAM_UNIT_PATH:-$(balam_default_path)}

  # Optional: pin the FIRST instance to a non-default Claude config directory.
  # Unset (the common case) leaves it on ~/.claude. Set it when ~/.claude belongs
  # to a person rather than to the bot — e.g. the operator's own interactive
  # login — so the bot's account and the operator's account stay separate.
  BALAM_PRIMARY_CLAUDE_CONFIG_DIR=${BALAM_PRIMARY_CLAUDE_CONFIG_DIR:-}

  # Where a NEW instance's Claude config dir is seeded from: the global skills and
  # settings a fresh directory would otherwise lack. Defaults to the operator's
  # own ~/.claude, which is where machine-wide skills live even when no bot uses
  # that directory.
  BALAM_SEED_FROM=${BALAM_SEED_FROM:-$BALAM_HOME/.claude}

  # The tunnel name, and so the unit's `tunnel run <name>` argument and the
  # ingress file at /etc/cloudflared/<name>.yml.
  BALAM_TUNNEL_NAME=${BALAM_TUNNEL_NAME:-balam}
}

# The agent subprocess (the `claude` CLI, or opencode) is spawned by the unit, so
# its tool shells see ONLY the unit's PATH — systemd does not source a profile.
# Probe for the user-installed toolchains that are actually present rather than
# hardcoding one machine's list.
balam_default_path() {
  local dirs=() d node
  for d in "$BALAM_HOME/.bun/bin" "$BALAM_HOME/.opencode/bin" "$BALAM_HOME/.local/bin"; do
    [ -d "$d" ] && dirs+=("$d")
  done
  # nvm keeps one bin dir per node version; take the highest.
  node=$(ls -d "$BALAM_HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
  [ -n "$node" ] && dirs+=("$node")
  [ -d "$BALAM_HOME/.cargo/bin" ] && dirs+=("$BALAM_HOME/.cargo/bin")
  dirs+=(/usr/local/sbin /usr/local/bin /usr/sbin /usr/bin /sbin /bin /usr/games /usr/local/games)
  for d in /snap/bin /usr/local/go/bin "$BALAM_GOPATH/bin"; do
    [ -d "$d" ] && dirs+=("$d")
  done
  (
    IFS=:
    printf '%s' "${dirs[*]}"
  )
}

# --- building the Mini App --------------------------------------------------

# balam_build_miniapp <repo>
#
# `bun run build` at the workspace root re-invokes bun through a plain shell
# (`bun run --filter './apps/frontend' build`), so calling it by absolute path is
# NOT enough — bun has to be ON PATH for that child, or the build dies with
# "bun: command not found". The installer often runs from a shell whose PATH
# lacks it (a systemd timer, a cron job, an agent), so put it there explicitly.
balam_build_miniapp() {
  (
    cd "$1" || exit 1
    PATH="$(dirname "$BALAM_BUN"):$PATH"
    export PATH
    "$BALAM_BUN" run build >/dev/null
  )
}

# --- reading the instance's own .env ---------------------------------------

# Read one KEY=value out of an env file. Last assignment wins, quotes stripped,
# commented lines ignored — close enough to how systemd and pydantic-settings
# read the same file.
balam_env_get() {
  [ -f "$1" ] || return 0
  sed -n "s/^[[:space:]]*$2[[:space:]]*=[[:space:]]*//p" "$1" | tail -1 |
    sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

balam_env_has() { [ -f "$1" ] && grep -qE "^[[:space:]]*$2[[:space:]]*=" "$1"; }

# --- rendering --------------------------------------------------------------

# balam_render <template.in> <dest> [EXTRA_TOKEN=value ...]
#
# Tokens are @NAME@. Values are paths, so `|` is the sed delimiter; a value
# containing `|` would break this, and none of these can.
balam_render() {
  local template=$1 dest=$2
  shift 2

  local -a script=(
    -e "s|@USER@|$BALAM_USER|g"
    -e "s|@HOME@|$BALAM_HOME|g"
    -e "s|@REPO@|$BALAM_REPO|g"
    -e "s|@INSTANCE_ROOT@|$BALAM_INSTANCE_ROOT|g"
    -e "s|@INSTANCE_PREFIX@|$BALAM_INSTANCE_PREFIX|g"
    -e "s|@UV@|$BALAM_UV|g"
    -e "s|@CLOUDFLARED@|$BALAM_CLOUDFLARED|g"
    -e "s|@OPENCODE@|$BALAM_OPENCODE|g"
    -e "s|@OPENCODE_DB@|$BALAM_OPENCODE_DB|g"
    -e "s|@PATH@|$BALAM_UNIT_PATH|g"
    -e "s|@GOPATH@|$BALAM_GOPATH|g"
    -e "s|@TUNNEL_NAME@|$BALAM_TUNNEL_NAME|g"
  )
  local kv
  for kv in "$@"; do
    script+=(-e "s|@${kv%%=*}@|${kv#*=}|g")
  done

  local tmp
  tmp=$(mktemp)
  {
    printf '# GENERATED by deploy/%s from %s — do not edit this file.\n' \
      "$(basename "${BASH_SOURCE[1]:-install.sh}")" "$(basename "$template")"
    printf '# Edit the template in the checkout, or deploy/deploy.env, and re-run the installer.\n'
    sed "${script[@]}" "$template"
  } >"$tmp"

  # Any @TOKEN@ left over means a template gained a placeholder that lib.sh does
  # not know about — silently shipping it would produce a unit systemd rejects.
  local leftover
  # `|| true`: grep exits 1 when it finds nothing, which is the good case, and
  # the callers run under `set -e`.
  leftover=$( (grep -o '@[A-Z_]\{2,\}@' "$tmp" || true) | sort -u | tr '\n' ' ')
  if [ -n "$leftover" ]; then
    rm -f "$tmp"
    echo "unsubstituted token(s) in $(basename "$template"): $leftover" >&2
    return 1
  fi

  sudo install -m 0644 -o root -g root "$tmp" "$dest"
  rm -f "$tmp"
  echo "    $(basename "$dest")"
}

# --- the named tunnel -------------------------------------------------------

# balam_tunnel_check <tunnel name> <this host's tunnel unit>
#
# Tunnel names are account-wide, so the name this host is about to run may
# already be running on another machine, and nothing on this host would show
# it. Cloudflare balances a tunnel's traffic across ALL of its connectors, so a
# second machine joining the same tunnel — a copied credentials file, or
# `cloudflared tunnel token <name>` — round-robins the Mini App between two VMs
# while both look healthy. So: ask Cloudflare who is connected, subtract this
# host's own connector (cloudflared logs "Connector ID: <uuid>" at every start
# and the unit's journal keeps them), and refuse if anyone else is left.
#
# BALAM_ALLOW_SHARED_TUNNEL=1 (the installers' --allow-shared-tunnel flag)
# downgrades the refusal to a warning. A deliberate two-connector setup is the
# only reason to.
#
# `--config /dev/null` stops cloudflared from auto-loading ~/.cloudflared/config.yml,
# which belongs to whatever OTHER tunnel this machine runs and makes a bare
# `tunnel info <name>` resolve that tunnel's credentials instead of the name.
# The account certificate (~/.cloudflared/cert.pem, from `cloudflared tunnel
# login`) is found regardless.
#
# Exit status: 0 = the tunnel is exclusively ours, or the refusal was overridden,
# or the check could not run at all (a network blip must not block a re-install
# of a working system — a warning says so); 1 = another machine is serving the
# tunnel, or the tunnel does not exist on this account.
balam_tunnel_check() {
  local name=$1 unit=$2
  local py
  py=$(command -v python3 || true)
  if [ -z "$py" ] && [ -x "$BALAM_REPO/apps/backend/.venv/bin/python" ]; then
    py="$BALAM_REPO/apps/backend/.venv/bin/python"
  fi
  if [ -z "$py" ]; then
    echo "  ! no python3 to parse cloudflared's output — cannot check whether tunnel '$name' is served elsewhere." >&2
    echo "    Verify by hand: cloudflared --config /dev/null tunnel info $name" >&2
    return 0
  fi

  local err listing info
  err=$(mktemp)
  # stdout only: cloudflared prints "your version is outdated" on stderr, and
  # that line would break the JSON.
  if ! listing=$("$BALAM_CLOUDFLARED" --config /dev/null tunnel list -n "$name" -o json 2>"$err"); then
    echo "  ! could not ask Cloudflare about tunnel '$name' (is 'cloudflared tunnel login' done on this host?):" >&2
    sed 's/^/    /' "$err" >&2
    rm -f "$err"
    echo "  ! skipping the shared-tunnel check. Verify by hand: cloudflared --config /dev/null tunnel info $name" >&2
    return 0
  fi
  if [ "$(printf '%s' "$listing" | "$py" -c 'import json,sys; print(len(json.load(sys.stdin) or []))')" = "0" ]; then
    rm -f "$err"
    echo "tunnel '$name' does not exist on this Cloudflare account." >&2
    echo "  Create it (cloudflared tunnel create $name) and route DNS to it, or point BALAM_TUNNEL_NAME" >&2
    echo "  in deploy/deploy.env at the tunnel you did create. Existing ones: cloudflared tunnel list" >&2
    return 1
  fi
  if ! info=$("$BALAM_CLOUDFLARED" --config /dev/null tunnel info -o json "$name" 2>"$err"); then
    echo "  ! could not read the connectors of tunnel '$name':" >&2
    sed 's/^/    /' "$err" >&2
    rm -f "$err"
    echo "  ! skipping the shared-tunnel check. Verify by hand: cloudflared --config /dev/null tunnel info $name" >&2
    return 0
  fi
  rm -f "$err"

  local ours foreign
  # `|| true`: grep exits 1 when the unit has never run here, which is the
  # normal first-install case, and the callers run under `set -e`.
  ours=$(sudo journalctl -u "$unit" -o cat --no-pager 2>/dev/null |
    grep -o 'Connector ID: [0-9a-f-]\{36\}' | awk '{print $3}' | sort -u | tr '\n' ' ' || true)
  # The JSON travels in the environment: stdin is taken by the heredoc.
  if ! foreign=$(BALAM_TUNNEL_INFO="$info" "$py" - "$ours" <<'PY'
import json
import os
import sys

ours = set(sys.argv[1].split())
info = json.loads(os.environ["BALAM_TUNNEL_INFO"])
for c in info.get("conns") or []:
    if c.get("id") in ours:
        continue
    ips = sorted({e.get("origin_ip") or "?" for e in c.get("conns") or []})
    print(
        f"{c.get('id', '?')}  from {', '.join(ips) or '?'}  "
        f"{c.get('arch', '?')} cloudflared {c.get('version', '?')}  running since {c.get('run_at', '?')}"
    )
PY
  ); then
    echo "  ! could not parse cloudflared's answer for tunnel '$name'; skipping the shared-tunnel check." >&2
    echo "    Verify by hand: cloudflared --config /dev/null tunnel info $name" >&2
    return 0
  fi
  if [ -z "$foreign" ]; then
    return 0
  fi

  if [ "${BALAM_ALLOW_SHARED_TUNNEL:-0}" = "1" ]; then
    echo "  ! tunnel '$name' is also served by another machine (--allow-shared-tunnel: continuing):" >&2
    printf '%s\n' "$foreign" | sed 's/^/    /' >&2
    return 0
  fi

  cat >&2 <<EOF
Tunnel '$name' is already being served by another machine:
$(printf '%s\n' "$foreign" | sed 's/^/    /')

Cloudflare balances a tunnel's requests across every connector, so starting the
tunnel here would round-robin the Mini App between this VM and that one — and
nothing on either machine would report it. Either:
  - use a tunnel of your own: cloudflared tunnel create <new name>, route DNS to
    it, point deploy/cloudflared-balam.yml at its id, and (first instance only)
    set BALAM_TUNNEL_NAME=<new name> in deploy/deploy.env;
  - stop the tunnel on the other machine first; or
  - pass --allow-shared-tunnel if two connectors is really what you want.
(If that connector is this host's own $unit and its journal was cleared,
 restart the unit and re-run: the check knows this host by the connector ids
 the unit logged.)
EOF
  return 1
}
