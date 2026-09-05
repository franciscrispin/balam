---
name: run-balam
description: >-
  Start, stop, restart, and check the Balam app on this VM. Balam runs as
  systemd services: the Balam backend (the bot + Mini App server), the
  Cloudflare named tunnel that exposes the Mini App, and — only when the repo
  .env says AGENT_BACKEND=opencode — the OpenCode server. On a claude_sdk host
  balam-opencode.service does not exist. Use this whenever the user wants to
  "start the app", "start the bot", "run balam", "restart balam", "is the app
  up?", or to "stop/shut down the app" — even if they don't name the individual
  services. This is the canonical way to operate Balam; the separate
  `browser-use` skill drives the bot through Telegram once it is up.
---

# Operate Balam (systemd + Cloudflare tunnel)

Balam is a Telegram bot fronting a coding agent — OpenCode or the Claude Agent
SDK, chosen by `AGENT_BACKEND` in the repo `.env` (ADR-0014) — deployed under
**systemd** with a Cloudflare **named tunnel** in front of the Mini App
(ADR-0013). "Running the app" means two or three long-lived **systemd
services**, and which ones exist depends on the backend:

| Unit                        | What it does                                                                 | Exists when                   |
| --------------------------- | ---------------------------------------------------------------------------- | ----------------------------- |
| `balam.service`             | `uv run balam` — the bot **and** the Mini App server on `127.0.0.1:$BALAM_PORT` (3000 by default). In `claude_sdk` mode the agent is a `claude` subprocess **inside this unit**. | always                        |
| `cloudflared-balam.service` | named tunnel: `https://<host>` → that port (Mini App only)                   | always, for the first instance |
| `balam-opencode.service`    | `opencode serve` on `127.0.0.1:4096` — the agent                             | `AGENT_BACKEND=opencode` only |

**Find out which before naming units.** `systemctl` fails outright on a unit
that was never installed, so on a host you have not operated before, check:

```sh
grep '^AGENT_BACKEND' .env                                # opencode | claude_sdk (unset = opencode)
systemctl list-unit-files 'balam*' 'cloudflared-balam*'   # what is actually installed here
```

The commands below name `balam cloudflared-balam`; add `balam-opencode` (first
when starting, last when stopping) only on an `opencode` host.

The frontend is **not** a separate runtime process: `bun run build` produces
`apps/frontend/dist`, which the backend (`balam.service`) serves. OpenCode
(`:4096`) and any VNC ports are **never** tunneled.

`deploy/README.md` is the authoritative reference for this stack (prerequisites,
the one-time Telegram / tunnel / BotFather setup, the public-mode
`deploy/balam.env` overlay, ADR-0013). This skill is just the day-to-day operate
loop.

## Everyday operations

systemd handles ordering for you: the tunnel is ordered `After` the bot, and in
`opencode` mode `balam.service` `Requires` the OpenCode unit, so restarting the
bot pulls OpenCode up with it. Naming every unit is still clearest, and
idempotent.

| Action          | Command                                                                                         |
| --------------- | ----------------------------------------------------------------------------------------------- |
| **Start**       | `sudo systemctl start balam cloudflared-balam` (opencode: `balam-opencode balam cloudflared-balam`) |
| **Stop**        | `sudo systemctl stop cloudflared-balam balam` (opencode: `cloudflared-balam balam balam-opencode`)  |
| **Restart**     | `sudo systemctl restart --no-block balam` (bot + Mini App; in opencode mode pulls OpenCode up if down) |
| **Status**      | `systemctl --no-pager status balam cloudflared-balam` (opencode: add `balam-opencode`)          |
| **Logs**        | `journalctl -u balam -n 100 --no-pager` (or `-f` to follow)                                     |
| **Tunnel logs** | `journalctl -u cloudflared-balam -n 100 --no-pager`                                             |

After checking status, read the `Active:` line for each unit — `active
(running)` is healthy. The bot logs `Application started` once it's polling
Telegram.

**"Running" is not "working".** A bot whose unit is `active (running)`, with
`Application started` in the journal, can still ignore every plain message in a
topic while answering `/status`: group privacy mode is still on in BotFather, or
`ALLOWED_TELEGRAM_CHAT_ID` is the pre-Topics id of the group. Telegram reports
neither, so Balam does: a wrong-shaped chat id is a boot error, and privacy
mode, Topics off, or an unreachable chat each log a `WARNING` right after
`Application started`. Read the journal once after a first start.
`deploy/README.md`, "Telegram: the bot and the group", has the fixes.

## When you change things

The services run the code from the working tree, so a restart picks up edits —
but **what** you restart depends on what changed:

- **Backend code (`apps/backend`) or `.env` / `deploy/balam.env`:** `sudo
systemctl restart --no-block balam`.
- **Frontend code (`apps/frontend`):** rebuild first, since the backend serves
  the static build — `bun run build` (from repo root), then `sudo systemctl
restart --no-block balam`.
- **A unit template, `deploy/deploy.env`, or the tunnel ingress
  (`deploy/*.service.in`, `cloudflared-balam.yml`):** re-run `deploy/install.sh`.
  The units in `/etc/systemd/system` are **rendered** from the `.in` templates,
  not copied, so editing one in `/etc/` is pointless — the next install
  overwrites it. The script `daemon-reload`s, rebuilds and restarts.
- **`AGENT_BACKEND` itself:** re-run `deploy/install.sh` as well, not just a
  restart — the installer is what adds or drops `balam-opencode.service` and
  the bot unit's dependency on it.

## The 409 singleton trap

The bot poller is a **singleton**. If `balam.service` is up and you _also_ start
a second poller (e.g. `uv run balam` by hand, or the old dev scripts), Telegram
returns **`409 Conflict`** and the bot silently stops receiving messages. Never
run a second poller **on the same bot token**. If the bot looks dead and the logs
show 409, find and kill the rival poller (or just `sudo systemctl restart balam`
after ensuring nothing else is running `balam`).

Likewise `:3000` (or whatever `BALAM_PORT` says — the Mini App) and, in
`opencode` mode, `:4096` (OpenCode) are single-owner ports; a hand-started copy
will collide with the systemd one.

The trap is the **token**, not the machine. A separate instance with its own
BotFather token and its own port is fine — see below.

## Extra instances (a second Claude account)

This VM can run more than one Balam bot, each signed in to a different Claude
account. Instance `<name>` is a separate checkout beside this one
(`<this checkout>-<name>`, e.g. `~/projects/balam-<name>`), with its own bot
token, its own `BALAM_PORT`, and its own Claude login in `~/.claude-<name>` (the
unit sets `CLAUDE_CONFIG_DIR`). Extra instances are `claude_sdk`-only, so they
never have a `balam-opencode.service`.

| Action      | Command                                        |
| ----------- | ---------------------------------------------- |
| **Status**  | `systemctl status balam@<name>`                |
| **Logs**    | `journalctl -u balam@<name> -n 100 --no-pager` |
| **Restart** | `sudo systemctl restart --no-block balam@<name>` |
| **Install / update** | `deploy/install-instance.sh <name>`   |
| **Which account** | `CLAUDE_CONFIG_DIR=~/.claude-<name> claude auth status` |

`systemctl status 'balam@*'` lists them all. `deploy/README.md` has the full
setup, including the checks the install script makes and why each one matters.

## Restarting the bot from the bot

The agent can operate the stack when asked over Telegram — `config.yaml`'s
`balam` context pre-approves `systemctl`/`journalctl` scoped to the `*balam*`
units. One subtlety: restarting `balam` itself kills the very bot relaying the
reply — and in `claude_sdk` mode also the agent, which is a child of that unit
— so use **`--no-block`** so `systemctl` returns *before* systemd tears the bot
down:

```sh
sudo systemctl restart --no-block balam
```

Even so the confirmation reply is lost (the bot is gone) — it comes back in a few
seconds; just re-check with a status query. In `opencode` mode, never have the
bot restart `balam-opencode` while it's serving a request: that kills the
process running the command. Tunnel ops (`cloudflared-balam`) are safe from the
bot — a separate process from both the bot and the agent.

## First install on a fresh VM

`deploy/install.sh` renders the units from `deploy/*.service.in`, installs the
tunnel config, builds the Mini App, and enables+starts them. It installs
`balam-opencode.service` only when the repo `.env` says
`AGENT_BACKEND=opencode`. It assumes the **one-time** account/public state is
already done — group privacy mode off and the supergroup's `-100…` chat id in
`.env`, a named tunnel + DNS whose name no other machine is already serving
(`cloudflared tunnel list`), a BotFather Mini App made with `/newapp`, and
`deploy/balam.env` — see `deploy/README.md` for those steps. Without
`deploy/cloudflared-balam.yml` it installs the bot alone (no tunnel; `/diff`
gives a `127.0.0.1` URL). With it, it first asks Cloudflare whether another
machine is already serving that tunnel name and refuses if so
(`--allow-shared-tunnel` overrides). Deps are assumed installed; on a
brand-new checkout, once:

```sh
uv --directory apps/backend sync   # backend venv
bun install                        # frontend deps (repo root)
```

(`uv`, `bun` and `cloudflared` are expected to be on the VM already, plus
`opencode` in `opencode` mode, or a Claude login / `ANTHROPIC_API_KEY` in
`claude_sdk` mode.)

## Exercising the round-trip

To send a Telegram message and watch the streamed reply, that's the
**`browser-use`** skill's job, not this one.
