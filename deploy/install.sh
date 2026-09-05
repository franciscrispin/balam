#!/usr/bin/env bash
# Install + (re)start the Balam systemd stack and the Cloudflare named tunnel (ADR-0013).
#
#   deploy/install.sh [--allow-shared-tunnel]
#
# Idempotent: renders the unit templates + copies the tunnel ingress config,
# reloads systemd, builds the Mini App, then starts opencode → bot → tunnel.
#
# Machine-independent: nothing here is hardcoded to one host. The units are
# TEMPLATES (*.service.in) rendered by deploy/lib.sh, which derives the OS user,
# its home, this checkout's path and the tool locations from the machine itself.
# Override any of them in deploy/deploy.env (see deploy.env.example).
#
# Prerequisites (one-time, not done here — they create public/account state;
# README.md "Prerequisites" and "One-time setup" have the full steps and checks):
#   - The bot's group privacy mode OFF (BotFather /setprivacy) and the forum
#     supergroup's -100… id in .env. Telegram reports neither when wrong; the
#     bot refuses a wrong-shaped id at boot and logs a WARNING for privacy mode.
#   - cloudflared installed and `cloudflared tunnel login` done on this host.
#   - A named tunnel + DNS hostname:
#       cloudflared tunnel create balam
#       cloudflared tunnel route dns balam <your-host>     # e.g. balam.example.com
#     then copy deploy/cloudflared-balam.example.yml to deploy/cloudflared-balam.yml
#     (git-ignored) and fill in the tunnel id + hostname. Tunnel names are
#     account-wide: before starting the tunnel this script asks Cloudflare who is
#     connected to it and refuses if another machine is (balam_tunnel_check in
#     lib.sh; --allow-shared-tunnel overrides).
#   - A BotFather Mini App made with /newapp (NOT Bot Settings → "Configure Mini
#     App", which is the main Mini App and has no short name) whose Web App URL
#     is https://<your-host>/.
#   - deploy/balam.env (git-ignored) with the public-mode overlay:
#       BALAM_PUBLIC_URL=https://<your-host>
#       BALAM_MINIAPP_SHORTNAME=<botfather short name>
#
# No deploy/cloudflared-balam.yml means no tunnel: the bot (plus OpenCode in
# opencode mode) is installed alone, /diff replies with a 127.0.0.1 URL, and
# balam.env is optional. With an ingress file, balam.env is required — a tunnel
# the bot does not know the URL of is a quiet half-install.
#
# This installs THE first instance. For a second bot on a second Claude account,
# use install-instance.sh instead (see "A second instance" in README.md).
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
balam_deploy_init

usage() {
  cat <<'USAGE'
Install (or re-install) the first Balam instance under systemd.

  deploy/install.sh [--allow-shared-tunnel]

  --allow-shared-tunnel   start the tunnel even though another machine is already
                          serving the same tunnel name (Cloudflare then balances
                          the Mini App between the two).

The header of this script lists the one-time prerequisites.
USAGE
}

ALLOW_SHARED_TUNNEL=${BALAM_ALLOW_SHARED_TUNNEL:-0}
for arg in "$@"; do
  case "$arg" in
    --allow-shared-tunnel) ALLOW_SHARED_TUNNEL=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

DEPLOY="$BALAM_REPO/deploy"
ENV_FILE="$BALAM_REPO/.env"

# --- what to install --------------------------------------------------------

# The tunnel is optional, and its ingress file is the switch: present means a
# public Mini App through cloudflared-balam.service, absent means the bot only.
TUNNEL=0
if [ -f "$DEPLOY/cloudflared-balam.yml" ]; then
  TUNNEL=1
fi

if [ "$TUNNEL" -eq 1 ] && [ ! -f "$DEPLOY/balam.env" ]; then
  echo "ERROR: $DEPLOY/cloudflared-balam.yml is present but $DEPLOY/balam.env is missing." >&2
  echo "       The tunnel would come up with the bot not knowing its public URL, so /diff" >&2
  echo "       would keep handing out 127.0.0.1 links. Create balam.env (see the header of" >&2
  echo "       this script), or remove the ingress file to install without a tunnel." >&2
  exit 1
fi
if [ "$TUNNEL" -eq 1 ] && [ ! -x "$BALAM_CLOUDFLARED" ]; then
  echo "ERROR: cloudflared not found ($BALAM_CLOUDFLARED). Install it — README.md, Prerequisites —" >&2
  echo "       or set BALAM_CLOUDFLARED in deploy/deploy.env, or remove $DEPLOY/cloudflared-balam.yml" >&2
  echo "       to install without a tunnel." >&2
  exit 1
fi
if [ "$TUNNEL" -eq 0 ] && [ -n "$(balam_env_get "$DEPLOY/balam.env" BALAM_PUBLIC_URL)" ]; then
  echo "  ! $DEPLOY/balam.env sets BALAM_PUBLIC_URL, but with no $DEPLOY/cloudflared-balam.yml" >&2
  echo "    no tunnel is installed here. /diff links will point at a host nothing serves," >&2
  echo "    unless something else fronts 127.0.0.1:\$BALAM_PORT." >&2
fi

# The unit owns CLAUDE_CONFIG_DIR when deploy.env pins it, and systemd applies
# EnvironmentFile= *over* Environment= whatever the order in the unit — so a line
# in the repo .env would silently win and put the bot on another Claude account.
if [ -n "$BALAM_PRIMARY_CLAUDE_CONFIG_DIR" ] && balam_env_has "$ENV_FILE" CLAUDE_CONFIG_DIR; then
  echo "ERROR: $ENV_FILE sets CLAUDE_CONFIG_DIR, which overrides the unit's" >&2
  echo "       BALAM_PRIMARY_CLAUDE_CONFIG_DIR=$BALAM_PRIMARY_CLAUDE_CONFIG_DIR. Remove that line." >&2
  exit 1
fi

# AGENT_BACKEND decides whether there is an OpenCode server at all (ADR-0014).
# In claude_sdk mode the agent runs in-process, so installing and ordering after
# balam-opencode.service would leave a unit that fails on every boot.
backend=$(balam_env_get "$ENV_FILE" AGENT_BACKEND)
backend=${backend:-opencode}
UNITS=(balam.service)
if [ "$TUNNEL" -eq 1 ]; then
  UNITS+=(cloudflared-balam.service)
fi
OPENCODE_AFTER=""
OPENCODE_REQUIRES=""
if [ "$backend" = "opencode" ]; then
  UNITS=(balam-opencode.service "${UNITS[@]}")
  OPENCODE_AFTER=" balam-opencode.service"
  OPENCODE_REQUIRES="# The bot waits for OpenCode in its post_init; ordering after it avoids a slow start.\nRequires=balam-opencode.service"
fi

primary_claude=""
if [ -n "$BALAM_PRIMARY_CLAUDE_CONFIG_DIR" ]; then
  primary_claude="Environment=CLAUDE_CONFIG_DIR=$BALAM_PRIMARY_CLAUDE_CONFIG_DIR"
fi

# --- the tunnel is this host's alone ----------------------------------------

if [ "$TUNNEL" -eq 1 ]; then
  echo "==> Checking that no other machine is serving tunnel '$BALAM_TUNNEL_NAME'"
  BALAM_ALLOW_SHARED_TUNNEL="$ALLOW_SHARED_TUNNEL" \
    balam_tunnel_check "$BALAM_TUNNEL_NAME" cloudflared-balam.service || exit 1
fi

# --- units, ingress, build, start -------------------------------------------

tunnel_label=none
if [ "$TUNNEL" -eq 1 ]; then
  tunnel_label=$BALAM_TUNNEL_NAME
fi
echo "==> Rendering unit files into /etc/systemd/system"
echo "    user=$BALAM_USER  repo=$BALAM_REPO  backend=$backend  tunnel=$tunnel_label"
for u in "${UNITS[@]}"; do
  balam_render "$DEPLOY/$u.in" "/etc/systemd/system/$u" \
    "OPENCODE_AFTER=$OPENCODE_AFTER" \
    "OPENCODE_REQUIRES=$OPENCODE_REQUIRES" \
    "PRIMARY_CLAUDE_ENV=$primary_claude"
done

if [ "$TUNNEL" -eq 1 ]; then
  echo "==> Installing tunnel ingress config to /etc/cloudflared/$BALAM_TUNNEL_NAME.yml"
  sudo mkdir -p /etc/cloudflared
  sudo install -m 0644 "$DEPLOY/cloudflared-balam.yml" "/etc/cloudflared/$BALAM_TUNNEL_NAME.yml"
else
  port=$(balam_env_get "$ENV_FILE" BALAM_PORT)
  echo "==> No $DEPLOY/cloudflared-balam.yml — installing without a tunnel."
  echo "    The Mini App is reachable at http://127.0.0.1:${port:-3000} only, and /diff replies"
  echo "    with that URL instead of an in-Telegram button. To add the tunnel later, create the"
  echo "    ingress file + balam.env and re-run (README.md, 'One-time setup')."
  if [ -f /etc/systemd/system/cloudflared-balam.service ]; then
    echo "  ! cloudflared-balam.service is still installed from an earlier run and is left as is."
    echo "    To retire it: sudo systemctl disable --now cloudflared-balam"
  fi
fi

sudo systemctl daemon-reload

echo "==> Building the Mini App (served by FastAPI from dist/)"
balam_build_miniapp "$BALAM_REPO"

echo "==> Enabling + (re)starting the units"
# `enable --now` starts a stopped unit but does NOTHING to a running one, so on a
# re-run the freshly rendered unit would sit in /etc/ unapplied — the installer
# would report success while the old Environment= (say, the previous
# CLAUDE_CONFIG_DIR) kept running. Restart explicitly; this script is documented
# as the way to apply an edit, so it has to actually apply it.
for u in "${UNITS[@]}"; do
  sudo systemctl enable --quiet "$u"
  sudo systemctl restart "$u"
done

echo "==> Done. Status:"
systemctl --no-pager --lines=0 status "${UNITS[@]}" || true
