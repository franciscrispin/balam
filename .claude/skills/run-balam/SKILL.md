---
name: run-balam
description: >-
  Start, stop, restart, and check the Balam app on this VM. Balam runs as three
  systemd services: the OpenCode server (the agent), the Balam backend (the bot
  + Mini App server), and the Cloudflare named tunnel that exposes the Mini App.
  Use this whenever the user wants to "start the app", "start the bot", "run
  balam", "restart balam" "is the app up?", or to
  "stop/shut down the app" — even if they don't name the individual services.
  This is the canonical way to operate Balam; the separate `browser-use` skill
  drives the bot through Telegram once it is up.
---

# Operate Balam (systemd + Cloudflare tunnel)

Balam is a Telegram bot backed by an OpenCode coding agent, deployed under
**systemd** with a Cloudflare **named tunnel** in front of the Mini App
(ADR-0013). "Running the app" means three long-lived **systemd services**:

| Unit                        | What it does                                                      |
| --------------------------- | ----------------------------------------------------------------- |
| `balam-opencode.service`    | `opencode serve` on `127.0.0.1:4096` — the agent                  |
| `balam.service`             | `uv run balam` — the bot **and** the Mini App server on `:3000`   |
| `cloudflared-balam.service` | named tunnel: `https://<host>` → `127.0.0.1:3000` (Mini App only) |

The frontend is **not** a separate runtime process: `bun run build` produces
`apps/frontend/dist`, which the backend (`balam.service`) serves. OpenCode
(`:4096`) and any VNC ports are **never** tunneled.

`deploy/README.md` is the authoritative reference for this stack (one-time
tunnel/BotFather setup, the public-mode `deploy/balam.env` overlay, ADR-0013).
This skill is just the day-to-day operate loop.

## Everyday operations

systemd handles ordering for you: `balam.service` `Requires` opencode, and the
tunnel is ordered `After` the bot. Starting/restarting the bot pulls OpenCode up
with it. Still, naming all three is clearest and is idempotent.

| Action          | Command                                                                 |
| --------------- | ----------------------------------------------------------------------- |
| **Start**       | `sudo systemctl start balam-opencode balam cloudflared-balam`           |
| **Stop**        | `sudo systemctl stop cloudflared-balam balam balam-opencode`            |
| **Restart**     | `sudo systemctl restart --no-block balam` (bot + Mini App; pulls OpenCode if down) |
| **Status**      | `systemctl --no-pager status balam-opencode balam cloudflared-balam`    |
| **Logs**        | `journalctl -u balam -n 100 --no-pager` (or `-f` to follow)             |
| **Tunnel logs** | `journalctl -u cloudflared-balam -n 100 --no-pager`                     |

After checking status, read the `Active:` line for each unit — `active
(running)` is healthy. The bot logs `Application started` once it's polling
Telegram.

## When you change things

The services run the code from the working tree, so a restart picks up edits —
but **what** you restart depends on what changed:

- **Backend code (`apps/backend`) or `.env` / `deploy/balam.env`:** `sudo
systemctl restart --no-block balam`.
- **Frontend code (`apps/frontend`):** rebuild first, since the backend serves
  the static build — `bun run build` (from repo root), then `sudo systemctl
restart --no-block balam`.
- **A unit file or the tunnel ingress (`deploy/*.service`,
  `cloudflared-balam.yml`):** re-run `deploy/install.sh` (it copies the units,
  `daemon-reload`s, rebuilds, and restarts), or copy by hand + `sudo systemctl
daemon-reload`.

## The 409 singleton trap

The bot poller is a **singleton**. If `balam.service` is up and you _also_ start
a second poller (e.g. `uv run balam` by hand, or the old dev scripts), Telegram
returns **`409 Conflict`** and the bot silently stops receiving messages. There
is one bot, one workspace — never run a second copy. If the bot looks dead and
the logs show 409, find and kill the rival poller (or just `sudo systemctl
restart balam` after ensuring nothing else is running `balam`).

Likewise `:4096` (OpenCode) and `:3000` (Mini App) are single-owner ports; a
hand-started copy will collide with the systemd one.

## Restarting the bot from the bot

The agent (OpenCode, under `balam-opencode.service`) can operate the stack when
asked over Telegram — `config.yaml`'s `balam` context pre-approves
`systemctl`/`journalctl` scoped to the `*balam*` units. One subtlety: restarting
`balam` itself kills the very bot relaying the reply, so use **`--no-block`** so
`systemctl` returns *before* systemd tears the bot down:

```sh
sudo systemctl restart --no-block balam
```

Even so the confirmation reply is lost (the bot is gone) — it comes back in a few
seconds; just re-check with a status query. Never have the bot restart
`balam-opencode` while it's serving a request: that kills the process running the
command. Tunnel ops (`cloudflared-balam`) are safe from the bot — separate
process from both the bot and the agent.

## First install on a fresh VM

`deploy/install.sh` copies the units + tunnel config, builds the Mini App, and
enables+starts all three. It assumes the **one-time** account/public state is
already done (named tunnel + DNS, BotFather Mini App, `deploy/balam.env`) — see
`deploy/README.md` for those steps. Deps are assumed installed; on a brand-new
checkout, once:

```sh
uv --directory apps/backend sync   # backend venv
bun install                        # frontend deps (repo root)
```

(`opencode`, `uv`, and `cloudflared` are expected to be on the VM already.)

## Exercising the round-trip

To send a Telegram message and watch the streamed reply, that's the
**`browser-use`** skill's job, not this one.
