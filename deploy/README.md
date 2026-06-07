# Deploy — Balam under systemd + Cloudflare tunnel (ADR-0013)

Runs the three Balam services under systemd and exposes **only** the Mini App to
the internet through a Cloudflare **named tunnel** (stable hostname), with Telegram
`initData` (ADR-0008) as the trust boundary. Read **ADR-0013** first — it states
the security conditions this setup enforces.

## What runs

| Unit                        | What it does                                              |
| --------------------------- | -------------------------------------------------------- |
| `balam-opencode.service`    | `opencode serve` on `127.0.0.1:4096` (the agent)         |
| `balam.service`             | `uv run balam` — the bot **and** the Mini App API/server on `127.0.0.1:3000` |
| `cloudflared-balam.service` | named tunnel: `https://<your-host>` → `127.0.0.1:3000` only (`/etc/cloudflared/balam.yml`) |

The frontend is **not** a runtime service: `bun run build` produces `apps/frontend/dist`,
which FastAPI serves. OpenCode (`:4096`) and any VNC ports are **never** tunneled.

## In-Telegram Mini App: direct link

Telegram allows the in-app `web_app` button **only in private chats**. Balam is
scoped to a forum **supergroup**, so `/diff` instead sends a **direct Mini App link**
`t.me/<bot>/<shortname>?startapp=diff__<context>`, which opens the app inside
Telegram's webview (with signed `initData`) in any chat type. This needs a
BotFather-registered Mini App and a **stable** hostname (hence the named tunnel —
the BotFather Web App URL is fixed).

## One-time setup (creates public / account state — not in `install.sh`)

1. **Named tunnel + DNS** (stable hostname):
   ```sh
   cloudflared tunnel create balam
   cloudflared tunnel route dns balam <your-host>      # e.g. francis-balam.glintsintern.com
   ```
   Put the tunnel id + hostname in `cloudflared-balam.yml` (ingress → `127.0.0.1:3000`).
2. **BotFather Mini App** (`/newapp` on your bot): Web App URL = `https://<your-host>/`,
   pick a short name (e.g. `diff`).
3. **`deploy/balam.env`** (git-ignored — public-mode overlay):
   ```
   BALAM_PUBLIC_URL=https://<your-host>
   BALAM_MINIAPP_SHORTNAME=diff
   ```

The Mini App API always requires valid Telegram `initData` (ADR-0008/0013) — there
is no auth bypass. Both backend units also read the repo-root `.env` (bot token,
OpenCode password).

## Install / start

```sh
deploy/install.sh
```

Copies the units + `/etc/cloudflared/balam.yml`, builds the Mini App, and starts
opencode + bot + tunnel.

## Operate

```sh
systemctl status balam balam-opencode cloudflared-balam
journalctl -u balam -f                 # bot + Mini App logs
journalctl -u cloudflared-balam -f     # tunnel logs
sudo systemctl restart balam           # after editing code / .env / balam.env
```

## Alternative: quick (ephemeral) tunnel

For a throwaway test without DNS/BotFather, point `cloudflared-balam.service` at a
quick tunnel (`cloudflared tunnel --url http://127.0.0.1:3000 --config /dev/null`).
Its hostname changes on every restart, so run `deploy/refresh-tunnel-url.sh` after
each (re)start to rewrite `BALAM_PUBLIC_URL` in `balam.env` and restart the bot.
Note: a quick tunnel can't back a BotFather direct link (the URL must be stable),
so `/diff` in a group falls back to a browser URL button.
