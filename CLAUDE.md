# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Balam is

A Telegram bot backed by the [OpenCode](https://opencode.ai) coding agent, plus a
Telegram Mini App for richer views (git diffs, markdown, and a live view of the
agent's Chrome). It runs locally on an Ubuntu VM for **one** user.

**Read `docs/architecture-decisions.md` first.** The nine ADRs there are the
authoritative design and the reasons behind every choice below; this file only
summarizes. Source files carry `TODO` markers that cite the ADR they implement.

> Status: this is a tooling scaffold. The app entry points boot and the workspace
> wiring is proven end-to-end, but the bot/agent/Mini App behavior in the ADRs is
> not implemented yet.

## Commands

Run from the repo root. The runtime is **Bun** (Node is a drop-in fallback per ADR-0004).

| Command                | What it does                                                               |
| ---------------------- | -------------------------------------------------------------------------- |
| `bun install`          | Install all workspace dependencies.                                        |
| `bun run dev`          | Run backend + Mini App in watch mode (`--filter './apps/*'`).              |
| `bun run build`        | Build both apps; the backend compiles to a single binary at `dist/balam`.  |
| `bun run typecheck`    | Type-check every workspace (`tsc --noEmit`).                               |
| `bun run lint`         | Lint + format check with Biome. `bun run lint:fix` to autofix.             |
| `bun run format`       | Format with Biome.                                                         |
| `bun run test`         | Run all tests (`bun test`).                                                |
| `bun test <path>`      | Run a single test file, e.g. `bun test apps/backend/src/scaffold.test.ts`. |
| `bun test -t "<name>"` | Run tests matching a name pattern.                                         |

Tooling notes:

- **Biome** does both linting and formatting (not ESLint/Prettier): 2-space indent,
  line width 100, double quotes.
- TypeScript is `strict` with `noUncheckedIndexedAccess` and `verbatimModuleSyntax`,
  so type-only imports **must** use `import type { … }`. All workspaces extend
  `tsconfig.base.json` and emit nothing — builds go through `bun build`/`vite`.
- Tests use Bun's built-in runner (`import { test, expect } from "bun:test"`).

## Architecture

Three layers (ADR-0003). Responsibilities are kept separate on purpose — keep agent
logic out of the UI and UI logic out of the agent:

```
Mini App frontend (apps/frontend, React+Vite) — diff/markdown viewers, live Chrome iframe
        │ HTTP / WebSocket
Balam backend (apps/backend, Bun) — Telegram bot, serves Mini App, runs git, proxies noVNC, talks to OpenCode
        │ HTTP + SSE  (@opencode-ai/sdk)
OpenCode server (separate process, NOT in this repo) — the agent: model + local tools/files + browser-use skill
```

Monorepo (Bun workspaces, `packages/*` + `apps/*`):

- `packages/shared` — types shared by backend and Mini App. **Consumed as raw TS
  source**: its `main`/`types`/`exports` point at `src/index.ts`, so edits are
  picked up with no build step, and `typecheck` covers it. Import as `@balam/shared`.
- `apps/backend` — the core of the system. Uses `grammy` for Telegram and
  `@opencode-ai/sdk` as the OpenCode client (SDK docs:
  https://opencode.ai/docs/sdk/).
- `apps/frontend` — the Telegram Mini App. Dev server is pinned to port **5180**
  (`strictPort`) because 5173 is taken by another local project.

## Configuration

Copy `.env.example` to `.env` (Bun loads it automatically). Key vars: `TELEGRAM_BOT_TOKEN`,
`ALLOWED_TELEGRAM_USER_ID`, `OPENCODE_BASE_URL`, `OPENCODE_SERVER_PASSWORD`,
`BALAM_PORT`, `BALAM_WORKDIR`, `VNC_WS_URL`.
