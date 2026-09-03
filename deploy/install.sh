#!/usr/bin/env bash
# Install + (re)start the Balam systemd stack and the Cloudflare named tunnel (ADR-0013).
#
# Idempotent: renders the unit templates + copies the tunnel ingress config,
# reloads systemd, builds the Mini App, then starts opencode → bot → tunnel.
#
# Machine-independent: nothing here is hardcoded to one host. The units are
# TEMPLATES (*.service.in) rendered by deploy/lib.sh, which derives the OS user,
# its home, this checkout's path and the tool locations from the machine itself.
# Override any of them in deploy/deploy.env (see deploy.env.example).
#
# Prerequisites (one-time, not done here — they create public/account state):
#   - A named tunnel + DNS hostname:
#       cloudflared tunnel create balam
#       cloudflared tunnel route dns balam <your-host>     # e.g. balam.example.com
#     then copy deploy/cloudflared-balam.example.yml to deploy/cloudflared-balam.yml
#     (git-ignored) and fill in the tunnel id + hostname.
#   - A BotFather Mini App (/newapp) whose Web App URL is https://<your-host>/.
#   - deploy/balam.env (git-ignored) with the public-mode overlay:
#       BALAM_PUBLIC_URL=https://<your-host>
#       BALAM_MINIAPP_SHORTNAME=<botfather short name>
#
# This installs THE first instance. For a second bot on a second Claude account,
# use install-instance.sh instead (see "A second instance" in README.md).
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
. "$SCRIPT_DIR/lib.sh"
balam_deploy_init

DEPLOY="$BALAM_REPO/deploy"
ENV_FILE="$BALAM_REPO/.env"

if [ ! -f "$DEPLOY/balam.env" ]; then
  echo "ERROR: $DEPLOY/balam.env is missing — create it (see the header of this script)." >&2
  exit 1
fi

if [ ! -f "$DEPLOY/cloudflared-balam.yml" ]; then
  echo "ERROR: $DEPLOY/cloudflared-balam.yml is missing — copy cloudflared-balam.example.yml and fill it in." >&2
  exit 1
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
UNITS=(balam.service cloudflared-balam.service)
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

echo "==> Rendering unit files into /etc/systemd/system"
echo "    user=$BALAM_USER  repo=$BALAM_REPO  backend=$backend"
for u in "${UNITS[@]}"; do
  balam_render "$DEPLOY/$u.in" "/etc/systemd/system/$u" \
    "OPENCODE_AFTER=$OPENCODE_AFTER" \
    "OPENCODE_REQUIRES=$OPENCODE_REQUIRES" \
    "PRIMARY_CLAUDE_ENV=$primary_claude"
done

echo "==> Installing tunnel ingress config to /etc/cloudflared/$BALAM_TUNNEL_NAME.yml"
sudo mkdir -p /etc/cloudflared
sudo install -m 0644 "$DEPLOY/cloudflared-balam.yml" "/etc/cloudflared/$BALAM_TUNNEL_NAME.yml"

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
