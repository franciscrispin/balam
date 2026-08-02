# Balam — Codebase Guide

A quick orientation for someone new to the repo. For the *why* behind every
design choice, read `docs/architecture-decisions.md` (the ADRs are authoritative);
this doc maps **features → code** so you know what to read first.

## The one-paragraph mental model

Balam is a **Telegram bot, for one user, that fronts a coding agent**. You
message a Telegram forum topic; the backend maps that topic to an agent
*session*, forwards your text as a prompt, and streams the reply back into the
same topic — live, as it is generated. A Telegram Mini App adds richer views: a
git diff viewer, a markdown viewer, and a live view of the agent's Chrome.

The agent runtime is **pluggable** (ADR-0014): `AGENT_BACKEND` selects OpenCode
(a separate long-lived server) or the in-process Claude Agent SDK. Both implement
`AgentBackend` and emit the same normalized event vocabulary, so nothing above
that seam knows which one is running.

```
Telegram  ──(message)──▶  Balam backend (Python)  ──▶  OpenCode server  (AGENT_BACKEND=opencode)
   ▲                       bot · turns · streamer   └─▶  Claude Agent SDK (AGENT_BACKEND=claude_sdk)
   └──────(streamed reply, tool lines, approval buttons)─────────────────┘
```

## Two toolchains

| Half | Path | Stack | Role |
| --- | --- | --- | --- |
| **Backend** | `apps/backend` | Python, `uv`, FastAPI + python-telegram-bot (PTB) | The bot, the Mini App server, the agent client |
| **Frontend** | `apps/frontend` + `packages/shared` | TypeScript, Bun, React+Vite | The Mini App views |

The contract between halves is the backend's FastAPI OpenAPI schema; frontend
types are generated from it by `bun run gen:api`, and CI fails if the committed
types drift from the schema.

## Backend modules

All under `apps/backend/src/balam/`. Grouped by job rather than listed
alphabetically — the groups are the shape of the system.

### Entry and configuration

- **`app.py`** *(entry point)* — `uv run balam` lands here. Loads config and
  contexts, selects the agent backend, opens SQLite, starts the Mini App server
  as an asyncio task, re-arms `/schedule` timers, then long-polls.
- **`config.py`** — env/`.env` validation via pydantic-settings, failing fast
  with one combined message.
- **`contexts.py`** — loads the **required** `config.yaml`. A *context* = a
  working `directory` + optional `model`/`effort` + `allowed_tools` +
  `additional_directories` + `mcp` servers.
- **`auth.py`** — the ADR-0008 trust boundary: `is_owner` and
  `callback_authorized`. Message handlers get it as a PTB filter; callback
  queries carry no filter and must ask for it themselves.

### The Telegram surface

- **`bot.py`** — the plain-message path plus the registrar. It builds the PTB
  application, puts shared state in `bot_data`, and wires each handler to its
  command or callback pattern. Everything a handler *does* lives elsewhere; the
  dependency arrow points out of this file only.
- **`commands/`** — one module per command group: `session.py` (`/context`
  `/new` `/status` `/model` `/effort` `/rename` `/cancel`), `views.py` (`/diff`
  `/browser` `/artifacts`), `delete.py`, `schedule.py`.
- **`callbacks.py`** — the other end of the agent's questions: approval and
  question keyboard taps, resolving the future the turn is parked on.
- **`pickers.py`** — the paged multi-select shared by `/delete` and
  `/schedule cancel`.
- **`topics.py`** — naming, opening and linking forum topics. Takes no
  originating message, so `/context`, a General message and a `/schedule` timer
  all open topics the same way.
- **`message_text.py`** — turns a Telegram message into the text the agent sees,
  rendering back the forward/reply/quote gestures Telegram otherwise drops.
- **`telegram_utils.py`** — `thread_kwargs` (route a send to a topic) and
  `clear_keyboard`.

### Running a turn

- **`router.py`** — `TopicRef` → `ResolvedSession`. Maps a topic to its session
  within its bound context, lazily creating one and recreating one that vanished.
- **`store.py`** — the persistence behind the router: dependency-free `sqlite3`
  holding the topic→session map and the `schedules` table.
- **`turns.py`** — both the data (`TurnJob`, `TurnRegistry`: one in-flight turn
  per topic) and the act of running one — resolving the session, starting the
  background task, handing the slot to the next queued message, aborting on
  `/cancel`.
- **`streamer.py`** — the streaming transport. `DraftSession` and `stream_reply`:
  the animated draft / live-edit fallback, the flush loop, and the tail check
  that keeps the answer at the bottom of the topic.
- **`stream_render.py`** — the pure rendering half: tool lines and groups, the
  todo checklist, approval/question formatting and keyboards. Takes data, returns
  strings — no message is ever sent from here.
- **`markdown.py`** — GFM (what the agent emits) → Telegram MarkdownV2, chunked
  to ≤4096 chars at code-block-aware boundaries.
- **`rich_messages.py`** — the Bot API 10.1 native-GFM path, where available.
- **`schedules.py`** — `/schedule`'s timers (ADR-0016): the `<when>` parser,
  `JobQueue` registration, the fire path, and boot catch-up. `commands/schedule.py`
  holds only the command surface.

### Permissions and tools

- **`tools.py`** — the canonical tool registry. One `REGISTRY` of `ToolSpec`
  entries (wire name, display label, permission category, SDK spellings) that the
  streamer, the SDK backend and the permission layer all derive from.
- **`permissions.py`** — translates a context's `allowed_tools` into a native
  OpenCode permission ruleset, and evaluates that same ruleset in-process for the
  SDK backend.
- **`approvals.py`** — the local half of the hybrid model: the symlink-safe
  directory boundary (`realpath`) and the human approval keyboard. Decisions key
  on the **permission category**, not tool names, so no mutating tool is missed.
- **`attachments.py`** — downloads **every** Telegram media kind (photo,
  document, video, audio, voice, video note, animation, sticker) as raw bytes in
  a `data:` URL, and saves each one under `<workspace>/.balam/attachments/` so
  the agent's own tools can open the types it cannot be *shown*. The inbox lives
  inside the workspace on purpose — outside it, every read would hit the ADR-0012
  approval boundary — and carries a self-matching `.gitignore`, so it never
  appears in `git status` or `/diff`. Deciding what gets inlined for the model is
  a per-backend question, answered in `sdk_translate`.
  Two limits are the platform's, not ours: the Bot API refuses `getFile` above
  **20 MB** (carried as `PromptFile.error`, not raised, so the caption survives),
  and an album arrives as one message per item, i.e. one turn each.

### The agent seam (ADR-0014)

- **`agent/backend.py`** — the `AgentBackend` protocol and `TurnRequest`.
- **`agent/events.py`** — the normalized event vocabulary both runtimes emit.
- **`agent/opencode_backend.py`** + **`opencode.py`** — the OpenCode runtime and
  the hand-written `httpx` HTTP/SSE client under it.
- **`agent/claude_sdk_backend.py`** — the Claude Agent SDK runtime: the query
  loop, foreign-result detection, and the ADR-0015 background-work turn policy.
- **`agent/sdk_tasks.py`** — mirrors the CLI's `TaskCreate`/`TaskUpdate` pair into
  the todo vocabulary the streamer's checklist expects.
- **`agent/sdk_translate.py`** — the SDK↔OpenCode vocabulary boundary: tool
  names, input keys, MCP config shape, permission eval targets. Also turns
  attachments into Anthropic content blocks. Only three shapes can be inlined —
  a JPEG/PNG/GIF/WebP image, a PDF, or text — and nothing else is ever emitted as
  a block, because one unsupported source fails the *whole* turn at the API's
  schema check. Two traps this encodes: the `base64` document source accepts
  **only** `application/pdf` (so a CSV must be decoded into a `text` document),
  and `image/*` is wider than what the API decodes (HEIC, what an iPhone sends
  when a photo goes as a file, would 400 the turn). Every attachment — inlined or
  not — is then listed in a closing manifest naming its path on disk, which is
  how the agent reaches spreadsheets, archives, audio and video.
- **`mcp_config.py`** — per-context MCP server parsing (`${VAR}` from `.env`).

### The Mini App

- **`server.py`** — FastAPI: serves the built SPA, the `/api` routes, and `/mcp`.
- **`webapp_auth.py`** — verifies Telegram `initData` HMAC (ADR-0008).
- **`miniapp.py`** — launch links and buttons.
- **`git_diff.py`** — the diff the viewer renders.
- **`content_store.py`** — ephemeral markdown snapshots.
- **`vnc.py`** — the WebSocket↔TCP bridge to x11vnc for the live Chrome view.
- **`agent_tools.py`** — the agent-facing `send_file` tool, served to OpenCode as
  a remote MCP server and to the SDK in-process.

## Features → where to look

| Feature | Code |
| --- | --- |
| Message round-trip (text in → streamed reply) | `bot.py:_handle_message` → `turns.py:submit_turn` → `streamer.py:stream_reply` |
| Live animated streaming | `streamer.py` (`DraftSession`, the flush loop) |
| Tool-call lines and collapsed bursts | `stream_render.py` (`_render_tool_part`, `_render_tool_group`) |
| Interactive tool approval | `approvals.py` + `permissions.py` + `callbacks.py` |
| Which tools exist, and how each backend spells them | `tools.py` |
| Workspace contexts | `contexts.py`, `config.yaml` (`config.example.yaml`) |
| Topic ↔ session mapping | `router.py` + `store.py` |
| Opening / naming / linking topics | `topics.py` |
| Slash commands | `commands/` + `bot.py` (`BOT_COMMANDS`, `register_commands`) |
| Cancel a running turn | `commands/session.py` → `turns.py:abort_turn` |
| Trust boundary / allowlist | `auth.py` + `bot.py:build_application` |
| Choosing the agent runtime | `app.py` + `agent/` |
| Scheduled tasks (ADR-0016) | `schedules.py` + `commands/schedule.py` + `store.py` |
| Live browser view (ADR-0006) | `vnc.py` + `server.py` + `browser-view.tsx` |

## Running it

From `apps/backend` (or `uv --directory apps/backend`):

```
uv sync                                      # create venv + install
uv run balam                                 # run the bot (needs config.yaml + .env)
uv run pytest                                # tests  (uv run pytest -k <name> to filter)
uv run ruff check . && uv run ruff format .  # lint + format
```

From the repo root, for the Mini App: `bun install`, `bun run build`,
`bun run typecheck`, `bun run lint`, `bun run test`.

Prereqs: `.env` (copy `.env.example`), `config.yaml` (copy `config.example.yaml`),
and — only when `AGENT_BACKEND=opencode` — a running OpenCode server. The
`run-balam` skill is the canonical way to start everything locally.

## Things that will trip you up

- **Nothing imports `bot.py`.** Commands, callbacks, topics and turns are
  imported *by* it. If you find yourself wanting to import back into `bot.py`,
  the thing you want probably belongs in `topics.py` or `turns.py` — that is
  exactly the cycle `schedules.py` used to have.
- **`directory` is everywhere on purpose.** With the OpenCode backend, session
  create, prompt, abort *and* the event subscription all carry it; omitting it on
  the event stream silently breaks streaming.
- **One context per topic, for life.** Switching context never rebinds — it opens
  a new topic, so each topic's session history stays coherent.
- **Tests must stay hermetic.** `Config` reads real environment variables ahead
  of `.env`, and Balam runs under systemd with its whole environment exported. An
  autouse fixture in `conftest.py` strips it; without that, five tests fail only
  on the deployment machine.
- **The Mini App needs a build.** The backend serves `apps/frontend/dist`, so run
  `bun run build` after frontend changes or the SPA will not update.
- **`gen:api` is checked in CI.** Change a `server.py` route and you must run
  `bun run gen:api` and commit the result.
