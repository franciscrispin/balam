#!/usr/bin/env bash
# Install + (re)start the Balam systemd stack and the Cloudflare named tunnel (ADR-0013).
#
# Idempotent: copies the unit files + the tunnel ingress config, reloads systemd,
# builds the Mini App, then starts opencode → bot → tunnel.
#
# Prerequisites (one-time, not done here — they create public/account state):
#   - A named tunnel + DNS hostname:
#       cloudflared tunnel create balam
#       cloudflared tunnel route dns balam <your-host>     # e.g. francis-balam.glintsintern.com
#     then set that hostname in deploy/cloudflared-balam.yml (ingress) and tunnel id.
#   - A BotFather Mini App (/newapp) whose Web App URL is https://<your-host>/.
#   - deploy/balam.env (git-ignored) with the public-mode overlay:
#       BALAM_PUBLIC_URL=https://<your-host>
#       BALAM_MINIAPP_SHORTNAME=<botfather short name>
set -euo pipefail

REPO=/home/ubuntu/projects/balam
DEPLOY="$REPO/deploy"
UNITS=(balam-opencode.service balam.service cloudflared-balam.service)

if [ ! -f "$DEPLOY/balam.env" ]; then
  echo "ERROR: $DEPLOY/balam.env is missing — create it (see the header of this script)." >&2
  exit 1
fi

echo "==> Installing unit files to /etc/systemd/system"
for u in "${UNITS[@]}"; do
  sudo cp "$DEPLOY/$u" "/etc/systemd/system/$u"
done

echo "==> Installing tunnel ingress config to /etc/cloudflared/balam.yml"
sudo mkdir -p /etc/cloudflared
sudo cp "$DEPLOY/cloudflared-balam.yml" /etc/cloudflared/balam.yml

sudo systemctl daemon-reload

echo "==> Building the Mini App (served by FastAPI from dist/)"
cd "$REPO" && bun run build >/dev/null

echo "==> Enabling + starting OpenCode, the bot, and the tunnel"
sudo systemctl enable --now balam-opencode.service
sudo systemctl enable --now balam.service
sudo systemctl enable --now cloudflared-balam.service

echo "==> Done. Status:"
systemctl --no-pager --lines=0 status balam-opencode balam cloudflared-balam || true
