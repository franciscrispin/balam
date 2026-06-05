# Balam

Balam is a Telegram bot backed by the [OpenCode](https://opencode.ai) coding
agent, with a Telegram Mini App for richer views (git diffs, markdown, and a
live view of the agent's Chrome). It runs locally on an Ubuntu VM for a single
user.

The design and the reasons behind it live in
[docs/architecture-decisions.md](docs/architecture-decisions.md). Read that first.

## Layout

This is a **polyglot** repo — a Python backend beside a TypeScript frontend, with
no shared toolchain (ADR-0011):

- `apps/backend` — **Python** (uv): Telegram bot, OpenCode HTTP/SSE client, Mini
  App host, and noVNC proxy. This is the core of the system.
- `apps/frontend` — the Telegram Mini App (React + Vite, TypeScript).
- `packages/shared` — TypeScript types for the Mini App.

The contract between the two sides is the backend's FastAPI-emitted OpenAPI
schema, from which the frontend's types are generated (ADR-0003).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — the Python backend's runtime and package
  manager.
- [Bun](https://bun.sh) — the frontend + shared workspace runtime.
- [OpenCode](https://opencode.ai) — installed and run as a local server.

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

## Status

Early implementation. The bot↔agent round-trip over forum topics and the
workspace `/context` command are built; the Mini App, noVNC view, and the
remaining slash commands are not yet. See `CLAUDE.md` for details.
