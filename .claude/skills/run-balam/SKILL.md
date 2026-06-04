---
name: run-balam
description: >-
  Start (and gracefully stop) the Balam app locally on this VM — the three
  backing processes: the OpenCode server (the agent), the Balam backend (the
  bot, `uv run balam`), and the Mini App frontend (`bun run dev`). Use this whenever the
  user wants to "start/run/boot/bring up the app", "start the bot", "run balam",
  "get the backend running", "is the app up?", or to "stop/shut down/tear down
  the app" — even if they don't name the individual processes. This is the
  canonical way to launch Balam before testing it; the separate `browser-use`
  skill drives the bot through Telegram once it is up.
---

# Run Balam locally

Balam is a Telegram bot backed by an OpenCode coding agent (see `CLAUDE.md` and
`docs/architecture-decisions.md`). "Starting the app" means bringing up **three
long-lived processes**:

1. **OpenCode server** — the agent (a _separate_ program, not in this repo,
   **not** started by the backend). Balam is its HTTP/SSE client (ADR-0001/0002).
2. **Balam backend** — the bot itself, Python via `uv` (ADR-0011). It
   long-polls Telegram and proxies messages to OpenCode.
3. **Mini App frontend** — the Vite dev server (`bun run dev`, TypeScript/React,
   ADR-0003) that serves the richer views. Part of a normal full start.

**Order matters for the first two.** The backend waits for OpenCode to answer in
its `post_init` hook _before_ it starts polling Telegram (see
`apps/backend/src/balam/app.py`). Start OpenCode **first**; if it is down, the
backend log stops at `waiting for OpenCode …` and the bot never goes live. The
Mini App is **independent** — it doesn't gate on the other two, so it can start
any time (before, after, or alongside them).

---

## Prerequisites (quick checks)

- **`.env` at the repo root** holds the secrets/config (`TELEGRAM_BOT_TOKEN`,
  `OPENCODE_SERVER_PASSWORD`, `OPENCODE_BASE_URL`, `ALLOWED_TELEGRAM_USER_ID`,
  `BALAM_WORKDIR`, …). It is **deliberately read-protected** — do **not** `cat`
  it or read individual secrets out of it. Load it into the shell environment
  instead (next section).
- **`uv`** (Python runner) and the **`opencode`** CLI are installed on this VM.
- First time on a fresh checkout: `uv --directory apps/backend sync` to build the
  backend venv.

---

## Starting the app

Run each long-lived process in the **background** (with the Bash tool's
`run_in_background`, or a `tmux`/`screen` window) so it survives the turn, and
tee its output to a log you can tail. Use **`127.0.0.1:4096`** below only if that
matches `OPENCODE_BASE_URL` in `.env`; otherwise use the host/port from there.

### 0. First check what's already running (don't double-start)

All three processes are **singletons** — starting a second copy doesn't help and
actively hurts, so always check before launching:

```sh
pgrep -af 'opencode serve|uv .*run balam|bun run.*dev' || echo "nothing running"
```

- **OpenCode already up** — a second `opencode serve` just fails with
  `address already in use` on the port. Don't restart it; run the health-check
  below and reuse the running one.
- **The bot already up** — this is the dangerous one. Telegram allows **only one
  long-poller per bot token**, so a second `uv run balam` makes the two
  instances fight over `getUpdates` and Telegram returns `409 Conflict`
  ("terminated by other getUpdates request") — the bot effectively breaks. Never
  start a second instance.
- **The Mini App already up** — Vite is pinned to port **5180** with
  `strictPort: true`, so a second `bun run dev` exits immediately with a
  port-in-use error. Reuse the running one (`curl -sI http://localhost:5180`
  should answer).

So treat startup as **idempotent**: for each process, if it's already running
*and* healthy, leave it alone and move on. Only start the ones that are down. If
a process is running but **unhealthy** (e.g. OpenCode is up but `/doc` doesn't
return `401`/`200`, or the bot log shows `409 Conflict`), that's a stale/broken
instance — shut it down cleanly (see "Gracefully shutting down") and start fresh
rather than stacking another on top.

### 1. Start the OpenCode server (the agent)

*(Skip if step 0 showed it already running and the health-check passes.)*

The server needs the **same** password the backend uses
(`OPENCODE_SERVER_PASSWORD`). Rather than read it by hand, load the whole `.env`
into the process environment with `set -a` (auto-export) and start the server in
one command:

```sh
set -a && source /home/ubuntu/projects/balam/.env && set +a \
  && opencode serve --hostname 127.0.0.1 --port 4096
```

`set -a` makes every variable `source` reads get exported, so `opencode serve`
inherits `OPENCODE_SERVER_PASSWORD` (and everything else) without you ever
printing a secret. Run it in the background and redirect to e.g.
`/tmp/opencode-serve.log`.

**Verify it's healthy** — the Basic-auth handshake the backend performs. Load the
`.env` into *this* shell the same hands-off way (a fresh Bash call won't inherit
the server's environment), then let `curl` read the password from the
environment — never printed, and quoting is handled for you:

```sh
set -a && source /home/ubuntu/projects/balam/.env && set +a
curl -s -o /dev/null -w '%{http_code}\n'                                                       http://127.0.0.1:4096/doc   # expect 401 (auth required)
curl -s -o /dev/null -w '%{http_code}\n' -u "${OPENCODE_SERVER_USERNAME:-opencode}:$OPENCODE_SERVER_PASSWORD" http://127.0.0.1:4096/doc   # expect 200
```

`401` without auth **and** `200` with it is the healthy state. Connection-refused
means the server isn't up yet — give it a second and retry, or check the log.

### 2. Start the Balam backend (the bot)

*(Skip if step 0 showed it already running — a second poller causes `409
Conflict`, see above.)*

```sh
uv --directory apps/backend run balam
```

Run it in the background, tee to e.g. `/tmp/balam-bot.log`, and **watch the log
for this sequence**, in order:

- `[balam] INFO starting bot (owner <id>, workdir <dir>) ...`
- `[balam] INFO waiting for OpenCode at http://127.0.0.1:4096 ...`
- `[balam] INFO OpenCode is ready.` ← cleared the OpenCode gate
- `[telegram.ext.Application] INFO Application started` ← **now long-polling; the bot is live**

Poll the log for `Application started` rather than sleeping a fixed time. Once
you see it, the bot is up.

### 3. Start the Mini App frontend

*(Skip if step 0 showed it already running — a second `bun run dev` fails on the
pinned port 5180.)* Run from the **repo root**, in the background, tee to e.g.
`/tmp/balam-miniapp.log`:

```sh
bun run dev
```

First time on a fresh checkout: `bun install` at the repo root first. Vite is
ready when the log prints its `Local: http://localhost:5180/` line; confirm with:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5180   # expect 200
```

### 4. The app is up

The bot is answering the owner in Telegram and the Mini App is served on
`http://localhost:5180`. To actually exercise the round-trip (send a message,
watch the streamed reply), use the **`browser-use`** skill — that's its job, not
this one.

---

## Gracefully shutting down

Shut down in the **reverse** of startup order, and **never `kill -9`** — a clean
signal lets the backend run its `post_shutdown` hook, which closes the OpenCode
HTTP client and the SQLite session store cleanly (`app.py`). A hard kill skips
that and can leave the SQLite file mid-write.

### 1. Stop the bot first (graceful)

`app.run_polling` installs handlers for **SIGINT/SIGTERM**, so either works:

- If it's a foreground/interactive process: press **Ctrl-C** once.
- If it's a background process: send it SIGTERM (the default `kill` signal):

```sh
pkill -TERM -f 'uv .*run balam'        # or: kill -TERM <pid>
```

Wait for the log to show it stopped polling and shut down — give it a moment;
don't immediately re-signal or escalate to `-9`.

### 2. Then stop the OpenCode server

```sh
pkill -TERM -f 'opencode serve'        # or: kill -TERM <pid>
```

### 3. Stop the Mini App

Independent of the other two, so order doesn't matter — but still a clean signal,
not `-9`:

```sh
pkill -TERM -f 'bun run.*dev'          # or: kill -TERM <pid>
```

### 4. Confirm all are down

```sh
pgrep -af 'opencode serve|uv .*run balam|bun run.*dev' || echo "all stopped"
```

If a process genuinely refuses to exit after a SIGTERM and a brief wait, _then_
escalating to `kill -9 <pid>` is acceptable — but try the graceful signal first.

---

## Quick reference

| Action          | Command (background where long-lived)                                                |
| --------------- | ------------------------------------------------------------------------------------ |
| Check if up     | `pgrep -af 'opencode serve\|uv .*run balam\|bun run.*dev'` (start only what's down)  |
| Start agent     | `set -a && source .env && set +a && opencode serve --hostname 127.0.0.1 --port 4096` |
| Health-check    | `curl … /doc` → `401` no-auth, `200` with `-u opencode:$OPENCODE_SERVER_PASSWORD`    |
| Start bot       | `uv --directory apps/backend run balam` (wait for `Application started`)             |
| Start Mini App  | `bun run dev` (repo root; ready at `http://localhost:5180`)                          |
| First-run setup | `uv --directory apps/backend sync` (backend) + `bun install` (frontend)              |
| Stop bot        | `pkill -TERM -f 'uv .*run balam'` (or Ctrl-C)                                        |
| Stop agent      | `pkill -TERM -f 'opencode serve'`                                                    |
| Stop Mini App   | `pkill -TERM -f 'bun run.*dev'`                                                      |
| Confirm down    | `pgrep -af 'opencode serve\|uv .*run balam\|bun run.*dev'`                           |
