#!/usr/bin/env bash
# Capture the quick tunnel's current public URL and point the bot at it (ADR-0013).
#
# A `trycloudflare` quick tunnel gets a fresh https://<random>.trycloudflare.com
# host every time cloudflared-balam.service (re)starts. This reads that URL from
# the service journal, writes the public-mode overlay env (deploy/balam.env), and
# restarts balam.service so the /diff web_app button uses the live URL.
#
# Run this after starting OR restarting cloudflared-balam.service.
set -euo pipefail

ENV_FILE=/home/ubuntu/projects/balam/deploy/balam.env

url=""
for _ in $(seq 1 30); do
  # `|| true`: grep exits 1 until the URL is in the journal — don't let set -e abort.
  url=$(sudo journalctl -u cloudflared-balam --no-pager -o cat 2>/dev/null \
        | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)
  [ -n "$url" ] && break
  sleep 1
done

if [ -z "$url" ]; then
  echo "refresh-tunnel-url: no trycloudflare URL found in cloudflared-balam logs" >&2
  echo "  is cloudflared-balam.service running? (systemctl status cloudflared-balam)" >&2
  exit 1
fi

# Update only BALAM_PUBLIC_URL, preserving any other overlay lines a named-tunnel
# operator may have set (e.g. BALAM_MINIAPP_SHORTNAME). | as sed delimiter so the
# URL's slashes don't clash.
if [ -f "$ENV_FILE" ] && grep -q '^BALAM_PUBLIC_URL=' "$ENV_FILE"; then
  tmp=$(mktemp)
  sed "s|^BALAM_PUBLIC_URL=.*|BALAM_PUBLIC_URL=$url|" "$ENV_FILE" > "$tmp"
  mv "$tmp" "$ENV_FILE"
elif [ -f "$ENV_FILE" ]; then
  printf 'BALAM_PUBLIC_URL=%s\n' "$url" >> "$ENV_FILE"
else
  cat > "$ENV_FILE" <<EOF
# Public-mode overlay for balam.service (ADR-0013). BALAM_PUBLIC_URL is rewritten
# by deploy/refresh-tunnel-url.sh on each quick-tunnel (re)start; other lines are
# preserved. Do not commit.
BALAM_PUBLIC_URL=$url
EOF
fi

echo "wrote $ENV_FILE:"
echo "  BALAM_PUBLIC_URL=$url"

sudo systemctl restart balam
echo "restarted balam.service — /diff now links to $url"
