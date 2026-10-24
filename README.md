# Balam

Balam is a Telegram bot backed by the [OpenCode](https://opencode.ai) coding
agent, with a Telegram Mini App for richer views (git diffs, markdown, and a
live view of the agent's Chrome). It runs locally on an Ubuntu VM for a single
user.

The design and the reasons behind it live in
[docs/architecture-decisions.md](docs/architecture-decisions.md). Read that first.

## Features

- **Agent chat in Telegram** — each forum topic is its own OpenCode session;
  replies stream live as animated drafts.
- **Workspace contexts** — one bot drives several projects. `/context <name>`
  opens a new topic bound to that project's directory, model, and allowed tools.
- **Tool approvals** — pre-approved tools run without prompting; anything else
  asks via an inline keyboard, behind a symlink-safe directory boundary.
- **Mini App viewers** — `/diff` opens a git diff viewer, and the agent can share
  files into a markdown viewer with its `send_file` tool.
- **Topic management** — `/new` (optionally `/new <context> <first message>` to
  open a topic and start working in it at once), `/rename`, `/status`, `/cancel`.
- **Scheduled prompts** — `/schedule daily 07:30 <context> <prompt>` opens a fresh
  topic and runs that prompt on a timer, in your timezone (`BALAM_TIMEZONE`). A
  scheduled turn has nobody to approve tools, so it is refused anything past a
  read inside its workspace, and runs missed while Balam was down are caught up
  within six hours of when they were due.

- **Integrations** (optional): a local service, such as a payments checker,
  can post a Telegram message with buttons through Balam and be told when an
  allowed person taps one, by adding a block to `config.yaml`. No plugin code is
  loaded and a tap never goes through the agent. See [Integrations](#integrations).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — the Python backend's runtime and package
  manager.
- [Bun](https://bun.sh) — the frontend + shared workspace runtime.
- An agent runtime, chosen by `AGENT_BACKEND`: [OpenCode](https://opencode.ai)
  installed and run as a local server, or, for `claude_sdk`, a Claude login
  (`claude auth login`) or `ANTHROPIC_API_KEY`.
- A Telegram bot from BotFather with **group privacy mode disabled**, and the
  `-100…` id of the forum supergroup it lives in. Both fail silently when wrong;
  [deploy/README.md](deploy/README.md#telegram-the-bot-and-the-group) has the
  steps and the checks.

## Setup

```sh
cp .env.example .env                 # then fill in the values
uv --directory apps/backend sync     # backend deps
bun install                          # frontend + shared deps
```

Workspace contexts (per-directory agent workspaces) live in `config.yaml` — copy
`config.example.yaml` if you need more than the single `default` context.

## Common commands

### Backend (Python / uv) — from `apps/backend` or with `uv --directory apps/backend`

| Command | What it does |
| --- | --- |
| `uv run balam` | Run the bot (long polling). |
| `uv run pytest` | Run the backend tests. |
| `uv run ruff check . && uv run ruff format .` | Lint + format. |

### Frontend + shared (TypeScript / Bun) — from the repo root

| Command | What it does |
| --- | --- |
| `bun run dev` | Run the Mini App (Vite) in watch mode. |
| `bun run build` | Build the Mini App. |
| `bun run typecheck` | Type-check `packages/*` + the frontend. |
| `bun run lint` | Lint and check formatting with Biome. |

## Integrations

An integration is a service on the same machine that wants to ask the team
something with buttons and hear back when someone taps. Telegram gives a bot's
updates to one consumer, which is Balam, so Balam relays. The design and its
limits are in ADR-0018.

**Set it up** with a block in `config.yaml` (see `config.example.yaml`) and the
secret in `.env`:

```yaml
integrations:
  payments:
    url: http://127.0.0.1:8770/balam        # where taps are delivered (loopback only)
    secret: ${PAYMENTS_INTEGRATION_SECRET}  # from .env, 16+ characters
    tappers: [123456789]                    # optional: who may tap; default everyone allowed
    topic: "Payments"                       # optional: default topic, made if missing
```

Every `tappers` id must also be on the allowlist; Balam refuses to boot
otherwise. Messages go to `ALLOWED_TELEGRAM_CHAT_ID`, or the owner's private
chat (no topics) when it is unset.

**Post a message** to Balam's loopback server, with the secret as a bearer token:

```http
POST http://127.0.0.1:<BALAM_PORT>/integrations/payments/messages
Authorization: Bearer <secret>
Content-Type: application/json

{
  "text": "Record S$1,500 deposit on INV900?",
  "image": "<optional base64 photo, sent just above the text>",
  "buttons": [
    {"label": "Approve", "action": "approve", "data": {"match": 7}},
    {"label": "Not this one", "action": "reject", "data": {"match": 7}}
  ],
  "topic": "Payments",
  "parse_mode": "HTML"
}
```

Only `text` is required (up to 3,800 characters; `parse_mode` is `HTML`,
`MarkdownV2` or absent for plain text). Up to 8 buttons, one per row; `data` is
any JSON up to 2 KB and stays in Balam's SQLite, so the callback data on the
button is only `int:<token>`. The answer is
`{"message_id", "chat_id", "thread_id", "image_message_id"}`. To **edit** a
message the integration posted, send the same call with its `message_id` (no
`image`): the text and buttons are replaced and the old buttons stop working.

Errors: `401` bad secret, `404` unknown integration or message (and any request
that did not come from this machine, including through the tunnel), `422` bad
body, `502` Telegram refused it.

**A tap.** When an allowed person taps, Balam removes every button on the
message and shows "Working..." at once, so a second tap is impossible. It then
POSTs to the integration's `url`:

```json
{"action": "approve", "data": {"match": 7}, "user": {"id": 123456789, "name": "Ana"}, "message_id": 101}
```

with `X-Balam-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed by the
secret>`. Check it with a constant-time compare before acting. Answer within
30 seconds with:

```json
{"text": "Recorded.", "buttons": [], "toast": "Done", "parse_mode": null}
```

Balam puts `text` and `buttons` on the message and shows `toast` to whoever
tapped, if the answer came within 10 seconds (Telegram stops taking toasts
soon after a tap). Work that takes longer should answer at once ("Recording...") and edit
the message later through the post call. If the service fails, times out or
answers something unreadable, the message says so and gets **no** buttons back,
since the service may already have acted.

## Repository structure

A Python backend beside a TypeScript frontend, with no shared toolchain:

- `apps/backend` — **Python** (uv): Telegram bot, OpenCode HTTP/SSE client, Mini
  App host, and noVNC proxy. This is the core of the system.
- `apps/frontend` — the Telegram Mini App (React + Vite, TypeScript).
- `packages/shared` — TypeScript types for the Mini App.

The contract between the two sides is the backend's FastAPI-emitted OpenAPI
schema, from which the frontend's types are generated.
