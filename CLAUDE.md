# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What Balam is

A Telegram bot backed by the [OpenCode](https://opencode.ai) coding agent, plus
a Telegram Mini App for richer views (git diffs, markdown, a live view of the
agent's Chrome). Runs locally on an Ubuntu VM for **one** user.

**Read `docs/architecture-decisions.md` first** — the ADRs are the authoritative
design and the reasons behind the choices summarized here. Load-bearing:
**ADR-0011, the backend is Python** (FastAPI + python-telegram-bot, OpenCode over
HTTP); the frontend stays TypeScript (ADR-0003).

> Status: core features built — the bot↔agent round-trip over forum topics,
> workspace contexts + `/context`, plan mode (`/plan`), and the Mini App
> (diff viewer, markdown viewer, live noVNC browser view via `/browser`).

## Repo layout — two toolchains

**Polyglot** repo with no shared toolchain; the contract between halves is the
backend's FastAPI OpenAPI schema, from which frontend types are generated.

- `apps/backend` — **Python**, managed by **uv**. The core of the system.
- `apps/frontend` + `packages/shared` — **TypeScript**, in a **Bun workspace**.

## Commands

### Backend (Python / uv) — run from `apps/backend` or with `uv --directory apps/backend`

| Command                                       | What it does                  |
| --------------------------------------------- | ----------------------------- |
| `uv sync`                                     | Create the venv, install deps |
| `uv run balam`                                | Run the bot (long polling)    |
| `uv run pytest`                               | Run all backend tests         |
| `uv run pytest -k <name>`                     | Run tests matching a name     |
| `uv run ruff check . && uv run ruff format .` | Lint + format                 |

### Frontend + shared (TypeScript / Bun) — run from the repo root

| Command             | What it does                             |
| ------------------- | ---------------------------------------- |
| `bun install`       | Install frontend + shared deps           |
| `bun run dev`       | Run the Mini App (Vite) in watch mode    |
| `bun run build`     | Build the Mini App                       |
| `bun run typecheck` | Type-check `packages/*` + the frontend   |
| `bun run lint`      | Biome lint/format (`lint:fix` autofixes) |

Tooling gotchas:

- **Backend:** Python 3.12+, ruff (line width 100), pytest-asyncio with
  `asyncio_mode = auto` (so `async def test_*` just works). The OpenCode client
  is hand-written over `httpx` (ADR-0002/0011) — no TypeScript SDK in the
  backend. GFM→Telegram-MarkdownV2 uses `mistune`.
- **Frontend:** Biome (2-space, width 100, double quotes); TypeScript `strict`
  with `verbatimModuleSyntax`, so type-only imports **must** use `import type`.
- Frontend dev server is pinned to port **5180** (`strictPort`); 5173 is taken
  by another local project.

## Architecture

Three layers (ADR-0003) — keep agent logic out of the UI and UI logic out of the
agent:

```
Mini App frontend (apps/frontend, React+Vite, TS) — diff/markdown viewers, live Chrome iframe
        │ HTTP / WebSocket
Balam backend (apps/backend, Python: FastAPI + python-telegram-bot) — bot, serves Mini App, runs git, proxies noVNC, talks to OpenCode
        │ HTTP + SSE  (httpx, raw OpenCode HTTP API)
OpenCode server (separate process, NOT in this repo) — the agent: model + local tools/files + browser-use skill
```

Backend modules (`apps/backend/src/balam/`): `config.py` (env validation),
`contexts.py` (`config.yaml` workspace contexts), `opencode.py` (httpx HTTP/SSE
client), `store.py` (sqlite3 topic→session map), `router.py` (topic→context→
session, lazy create; registers Balam's per-topic MCP tool server),
`markdown.py` (GFM→MarkdownV2), `streamer.py` (animated `send_message_draft`
streaming), `bot.py` (PTB: allowlist, chat scoping, message handler, `/context`,
`setMyCommands`), `server.py` (FastAPI Mini App + `/api` + `/mcp` routes),
`agent_tools.py` (agent-facing `send_file` tool served to OpenCode as a remote
MCP server, per-topic scope tokens), `content_store.py` (ephemeral markdown
snapshots for the Mini App viewer), `miniapp.py` (Mini App launch links/buttons),
`vnc.py` (live browser view: `/api/vnc/ws` WebSocket↔TCP bridge to x11vnc,
ADR-0006), `app.py` (boot).

Telegram specifics (ADR-0009): streaming uses native `send_message_draft`; forum
topics are addressed by `message_thread_id`. Bot API ref:
https://core.telegram.org/bots/api.

**Workspace contexts** (ADR-0012, adapted from open-shrimp). A _context_ bundles
a working `directory` with optional `model`/`effort`, an `allowed_tools` list, and
optional `mcp` servers (local stdio or remote http/sse; registered with OpenCode
before each session — `${VAR}` in values is filled from `.env`), so one bot drives
several projects. Defined in the **required** `config.yaml`
(see `config.example.yaml`). Each topic binds to one context for its lifetime
(`default_context` for unbound topics like General). `/context` lists contexts +
the current binding; `/context <name>` **creates a new topic** bound to `<name>`
and replies with a "Go to topic" link — it does not rebind the current topic.
`allowed_tools`/`additional_directories` are enforced via the **hybrid** model in
ADR-0012: `permissions.py` translates them into a native OpenCode permission
ruleset (pre-approved tools run without prompting), while the symlink-safe
directory boundary and the human-approval keyboard stay local in `approvals.py`.

## Configuration

- **Secrets / env:** copy `.env.example` → `.env` (loaded by pydantic-settings;
  systemd env vars take precedence). `ALLOWED_TELEGRAM_CHAT_ID` (optional `-100…`
  id) scopes the bot to the "workspace" forum supergroup; unset → legacy
  owner-anywhere DM behavior (ADR-0008 trust boundary unchanged).
- **Contexts:** copy `config.example.yaml` → `config.yaml` (**required**; path
  via `BALAM_CONFIG_PATH`). Secrets stay in `.env`, never `config.yaml`.
