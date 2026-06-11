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
- **Plan mode** — `/plan` keeps a topic read-only until you approve the plan.
- **Tool approvals** — pre-approved tools run without prompting; anything else
  asks via an inline keyboard, behind a symlink-safe directory boundary.
- **Mini App viewers** — `/diff` opens a git diff viewer, and the agent can share
  files into a markdown viewer with its `send_file` tool.
- **Topic management** — `/new`, `/rename`, `/status`, and `/cancel`.

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

## Repository structure

A Python backend beside a TypeScript frontend, with no shared toolchain:

- `apps/backend` — **Python** (uv): Telegram bot, OpenCode HTTP/SSE client, Mini
  App host, and noVNC proxy. This is the core of the system.
- `apps/frontend` — the Telegram Mini App (React + Vite, TypeScript).
- `packages/shared` — TypeScript types for the Mini App.

The contract between the two sides is the backend's FastAPI-emitted OpenAPI
schema, from which the frontend's types are generated.
