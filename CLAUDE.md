# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What Balam is

A Telegram bot backed by the [OpenCode](https://opencode.ai) coding agent, plus
a Telegram Mini App for richer views (git diffs, markdown, and a live view of
the agent's Chrome). It runs locally on an Ubuntu VM for **one** user.

**Read `docs/architecture-decisions.md` first.** The ADRs there are the
authoritative design and the reasons behind every choice below; this file only
summarizes. **ADR-0011 is load-bearing: the backend is Python** (FastAPI +
python-telegram-bot, OpenCode over HTTP). The frontend stays TypeScript
(ADR-0003).

> Status: early implementation. The bot↔agent round-trip over forum topics is
> built (config, OpenCode HTTP/SSE client, topic→session SQLite map, allowlist,
> animated draft streaming with GFM→MarkdownV2). Workspace **contexts**
> (`config.yaml`) and the `/context` command are built. The Mini App, noVNC
> view, and other slash commands are not implemented yet.

## Repo layout — two toolchains

This is a **polyglot** repo: a Python backend beside a TypeScript frontend. They
do not share a toolchain; the contract between them is the backend's
FastAPI-emitted OpenAPI schema, from which the frontend's types are generated
(ADR-0003/0011).

- `apps/backend` — **Python**, managed by **uv**. The core of the system.
- `apps/frontend` + `packages/shared` — **TypeScript**, in a **Bun workspace**.

## Commands

### Backend (Python / uv) — run from `apps/backend` or with `uv --directory apps/backend`

| Command                                          | What it does                                  |
| ------------------------------------------------ | --------------------------------------------- |
| `uv sync`                                        | Create the venv and install deps.             |
| `uv run balam`                                   | Run the bot (long polling).                   |
| `uv run pytest`                                  | Run all backend tests.                        |
| `uv run pytest -k <name>`                        | Run tests matching a name.                    |
| `uv run ruff check . && uv run ruff format .`    | Lint + format (the Biome equivalent for Py).  |

### Frontend + shared (TypeScript / Bun) — run from the repo root

| Command             | What it does                                          |
| ------------------- | ----------------------------------------------------- |
| `bun install`       | Install frontend + shared deps.                       |
| `bun run dev`       | Run the Mini App (Vite) in watch mode.                |
| `bun run build`     | Build the Mini App.                                   |
| `bun run typecheck` | Type-check `packages/*` + the frontend.               |
| `bun run lint`      | Biome lint/format check (`lint:fix` to autofix).      |

Tooling notes:

- **Backend:** Python 3.12+, `ruff` for lint + format (line width 100), `pytest`
  with `pytest-asyncio` (`asyncio_mode = auto`, so `async def test_*` just works).
  The OpenCode client is hand-written over `httpx` (ADR-0002/0011) — there is no
  TypeScript SDK in the backend. GFM→Telegram-MarkdownV2 rendering uses `mistune`
  (`balam/markdown.py`).
- **Frontend:** Biome (2-space, width 100, double quotes); TypeScript `strict`
  with `verbatimModuleSyntax`, so type-only imports **must** use `import type`.

## Architecture

Three layers (ADR-0003). Responsibilities are kept separate on purpose — keep
agent logic out of the UI and UI logic out of the agent:

```
Mini App frontend (apps/frontend, React+Vite, TypeScript) — diff/markdown viewers, live Chrome iframe
        │ HTTP / WebSocket
Balam backend (apps/backend, Python: FastAPI + python-telegram-bot) — Telegram bot, serves Mini App, runs git, proxies noVNC, talks to OpenCode
        │ HTTP + SSE  (httpx, raw OpenCode HTTP API)
OpenCode server (separate process, NOT in this repo) — the agent: model + local tools/files + browser-use skill
```

Backend modules (`apps/backend/src/balam/`): `config.py` (env validation),
`contexts.py` (`config.yaml` workspace contexts), `opencode.py` (httpx HTTP/SSE
client), `store.py` (sqlite3 topic→session map), `router.py` (topic→context→
session, lazy create), `markdown.py` (GFM→MarkdownV2), `streamer.py` (animated
`send_message_draft` streaming + finalize), `bot.py` (PTB, allowlist + message
handler + `/context`), `app.py` (boot). Telegram Bot API reference:
https://core.telegram.org/bots/api. Streaming uses native **`send_message_draft`**;
forum topics are addressed by `message_thread_id` (ADR-0009).

**Workspace contexts.** A *context* (adapted from OpenShrimp/open-udang) bundles
a working `directory` with an optional `model` (`provider/model`) and `effort`,
plus an `allowed_tools` list, so one bot can drive several projects. Contexts are
defined in `config.yaml` (see `config.example.yaml`); the file is optional —
without it Balam runs a single `default` context from `BALAM_WORKDIR`. Each
topic binds to one context, persisted in the `context` column of the
topic→session row; an unbound topic uses `default_context`. `/context` lists the
contexts and the topic's current binding; `/context <name>` rebinds the topic and
starts a fresh session in that workspace. The router passes the resolved
directory/model/effort to the OpenCode prompt (`model` → `{providerID, modelID}`,
`effort` → `variant`). NOTE: `allowed_tools`/`additional_directories` are parsed
and validated but **path-scoped permission enforcement is not wired into OpenCode
yet** (deliberately deferred).

`packages/shared` (TypeScript) holds types for the Mini App; `@opencode-ai/sdk`
is **no longer used** (the backend is Python). Frontend dev server is pinned to
port **5180** (`strictPort`) because 5173 is taken by another local project.

## Configuration

Copy `.env.example` to `.env` (pydantic-settings loads the repo-root `.env`; real
env vars from systemd take precedence). Key vars: `TELEGRAM_BOT_TOKEN`,
`ALLOWED_TELEGRAM_USER_ID`, `OPENCODE_BASE_URL`, `OPENCODE_SERVER_PASSWORD`,
`BALAM_WORKDIR` (fallback workspace when no `config.yaml`), `BALAM_DB_PATH`,
`VNC_WS_URL`. Workspace contexts live in `config.yaml` (copy
`config.example.yaml`; path overridable via `BALAM_CONFIG_PATH`, default
repo-root). Secrets stay in `.env`, never `config.yaml`.
