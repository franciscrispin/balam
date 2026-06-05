---
name: run-balam
description: >-
  Start and stop the Balam app locally on this VM — the three
  backing processes: the OpenCode server (the agent), the Balam backend (the
  bot, `uv run balam`), and the Mini App frontend (`bun run dev`). Use this whenever the
  user wants to "start the app", "start the bot", "run balam",
  "get the backend running", "is the app up?", or to "stop/shut down
  the app" — even if they don't name the individual processes. This is the
  canonical way to launch Balam before testing it; the separate `browser-use`
  skill drives the bot through Telegram once it is up.
---

# Run Balam locally

Balam is a Telegram bot backed by an OpenCode coding agent. "Starting the app"
means bringing up **three long-lived processes**:

1. **OpenCode server** — the agent (a _separate_ program, not in this repo,
   **not** started by the backend). Balam is its HTTP/SSE client.
2. **Balam backend** — the bot itself, Python via `uv`. It long-polls Telegram
   and proxies messages to OpenCode.
3. **Mini App frontend** — the Vite dev server (`bun run dev`, TypeScript/React)
   that serves the richer views.

## Use the bundled scripts

Everything below is driven by four scripts in this skill's `scripts/` directory.
**Prefer them over hand-typed shell.** They aren't just convenience: each one is
a single, stable command you can allowlist by exact path, so a normal start/stop
runs with **zero permission prompts**. The old approach — pasting ad-hoc
compound one-liners (`set -a && … && opencode serve`, `for … grep … cat`, `pkill
-TERM -f …`) — prompts on essentially every line, because the permission system
can't prefix-match compound shell against an allowlist. The scripts move all
that logic (env-loading, health-polling, log-waiting, graceful shutdown)
_inside_ a file the permission system sees as one command.

Run each script by its **exact relative path from the repo root**.

| Action            | Command                                                                      | What it does                                                                                                |
| ----------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Start the app** | `.claude/skills/run-balam/scripts/start.sh`                                  | Idempotent. Starts only what's down, in the right order, waits for each to be healthy, prints final status. |
| **Check status**  | `.claude/skills/run-balam/scripts/status.sh`                                 | Read-only. Reports UP / DOWN / unhealthy for all three. Safe any time.                                      |
| **Stop the app**  | `.claude/skills/run-balam/scripts/stop.sh`                                   | Graceful reverse-order SIGTERM shutdown, then confirms all down.                                            |
| **Read a log**    | `.claude/skills/run-balam/scripts/logs.sh [opencode\|bot\|frontend] [lines]` | Tail a process log without an ad-hoc `cat`/`grep`.                                                          |

To start the app, run `start.sh` and read the status block it prints at the end
— that's the whole job. The script already waits for OpenCode's auth handshake,
the bot's `Application started` line, and the Mini App's `200`, so there's
nothing to poll or verify by hand afterward. The processes are detached
(`nohup`), so they survive the turn; `start.sh` itself returns within a few
seconds once everything is ready.

To exercise the round-trip (send a Telegram message, watch the streamed reply),
that's the **`browser-use`** skill's job, not this one.

### What the scripts handle for you

You don't need to drive these by hand, but knowing what's baked in helps you
read the output and trust it:

- **Singletons & idempotency.** All three are singletons and a second copy
  hurts: a second bot poller makes Telegram return **`409 Conflict`** (the one
  failure that silently breaks the bot); a second `opencode serve` or Vite just
  fails on its port. `start.sh` checks health first and starts only what's down.
  If `status.sh`/`start.sh` reports the bot **BROKEN (409)**, that's a rival
  poller — run `stop.sh` then `start.sh` to recover.
- **Order.** OpenCode comes up first because the backend's `post_init` hook
  blocks on it before polling Telegram; the Mini App is independent.
- **Secrets, hands-off.** The scripts load the repo-root `.env` with `set -a`
  (auto-export) so child processes inherit `OPENCODE_SERVER_PASSWORD` etc. The
  secret only ever lives in the environment — never printed. **Do not `cat` the
  `.env` or read secrets out of it**; it is deliberately read-protected.
- **Frontend detected by port.** `status.sh` checks `:5180` for a `200`, so an
  already-running Vite (even a bare `node …/vite`) is correctly seen as UP
  rather than double-started.
- **Graceful stop.** `stop.sh` uses SIGTERM (never `-9` first) so the backend
  runs its `post_shutdown` hook and closes the OpenCode client and SQLite store
  cleanly; it only escalates to SIGKILL if a process refuses to exit.

### First run on a fresh checkout

The scripts assume deps are installed. On a brand-new checkout, once:

```sh
uv --directory apps/backend sync   # backend venv
bun install                        # frontend deps (repo root)
```

(`opencode` and `uv` are expected to be on the VM already.)

## Avoiding permission prompts (one-time allowlist)

For a fully prompt-free start/stop, add the four script paths to the project
allowlist in `.claude/settings.json` (`permissions.allow`):

```json
"Bash(.claude/skills/run-balam/scripts/start.sh)",
"Bash(.claude/skills/run-balam/scripts/status.sh)",
"Bash(.claude/skills/run-balam/scripts/stop.sh)",
"Bash(.claude/skills/run-balam/scripts/logs.sh:*)"
```

These are narrow (exact script paths, not broad grants like `Bash(pkill:*)` or
`Bash(curl:*)`). The `:*` on `logs.sh` allows its arguments. If you're the agent
and these aren't present yet, offer to add them (the `update-config` skill does
this); don't add them silently.

## Manual fallback

If for some reason the scripts can't be used (they're missing, or you're
debugging one), the equivalent manual steps are: start `opencode serve` on the
host/port from `OPENCODE_BASE_URL` with the `.env` loaded via `set -a`; wait for
`/doc` to return `401` unauthenticated and `200` with `-u
opencode:$OPENCODE_SERVER_PASSWORD`; then `uv --directory apps/backend run
balam` and wait for `Application started` in its log; then `bun run dev` from
the repo root and wait for `200` on `:5180`. Stop in reverse order with `pkill
-TERM -f`. Expect a permission prompt per command this way — that's exactly what
the scripts exist to avoid.
