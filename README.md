# Balam

Balam is a Telegram bot backed by the [OpenCode](https://opencode.ai) coding
agent, with a Telegram Mini App for richer views (git diffs, markdown, and a
live view of the agent's Chrome). It runs locally on an Ubuntu VM for a single
user.

The design and the reasons behind it live in
[docs/architecture-decisions.md](docs/architecture-decisions.md). Read that first.

## Layout

This is a Bun-workspaces monorepo:

- `packages/shared` — types shared by the backend and the Mini App.
- `apps/backend` — the Bun server: Telegram bot, OpenCode client, Mini App host,
  and noVNC proxy. This is the core of the system.
- `apps/frontend` — the Telegram Mini App (React + Vite).

## Prerequisites

- [Bun](https://bun.sh) — the runtime (Node is a fallback; see ADR-0004).
- [OpenCode](https://opencode.ai) — installed and run as a local server.

## Setup

```sh
bun install
cp .env.example .env   # then fill in the values
```

## Common commands

| Command | What it does |
| --- | --- |
| `bun run dev` | Run the backend and the Mini App in watch mode. |
| `bun run build` | Build both apps (the backend compiles to a single binary). |
| `bun run typecheck` | Type-check every workspace. |
| `bun run lint` | Lint and check formatting with Biome. |
| `bun run format` | Format the code with Biome. |
| `bun run test` | Run tests with `bun test`. |

## Status

This is a tooling scaffold. The app entry points boot but do not yet implement
the behavior described in the ADRs. Look for `TODO` markers.
