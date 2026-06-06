# Balam — Codebase Guide

A quick orientation for someone new to the repo. For the *why* behind every
design choice, read `docs/architecture-decisions.md` (the ADRs are authoritative);
this doc maps **features → code** so you know what to read first.

## The one-paragraph mental model

Balam is a **Telegram bot, for one user, that fronts the [OpenCode] coding
agent**. You message a Telegram forum topic; the backend maps that topic to an
OpenCode *session*, forwards your text as a prompt, and streams the agent's reply
back into the same topic — live, as it's generated. A planned TypeScript Mini App
will add richer views (diffs, markdown, the agent's live Chrome). Today the
bot↔agent round-trip, workspace contexts, slash commands, and interactive tool
approval are built; the Mini App is still a scaffold.

```
Telegram  ──(message)──▶  Balam backend (Python)  ──HTTP+SSE──▶  OpenCode server
   ▲                          bot · router · streamer                (the agent)
   └──────(streamed reply, tool lines, approval buttons)─────────────────┘
```

## Two toolchains

| Half | Path | Stack | Role |
| --- | --- | --- | --- |
| **Backend** | `apps/backend` | Python, `uv`, FastAPI + python-telegram-bot (PTB) | The whole system today |
| **Frontend** | `apps/frontend` + `packages/shared` | TypeScript, Bun, React+Vite | Mini App (scaffold only) |

The contract between halves is the backend's FastAPI OpenAPI schema (frontend
types are generated from it). The frontend is just `App.tsx` + a placeholder
shared type right now — ignore it until you touch the Mini App.

## Backend modules — read in this order

All under `apps/backend/src/balam/`. The flow of a single message touches them in
roughly this sequence:

1. **`app.py`** *(entry point)* — `uv run balam` lands here. Loads config, loads
   contexts, waits for the OpenCode server to be ready (in PTB's `post_init`),
   then starts long polling. Note `allowed_updates=["message", "callback_query"]`
   — both the round-trip *and* approval-button taps must be requested or Telegram
   drops them.

2. **`config.py`** — env/`.env` validation via pydantic-settings. The trust
   boundary lives here: `allowed_telegram_user_id` (required) and the optional
   `allowed_telegram_chat_id` (scopes the bot to one forum supergroup). Fails
   fast with one combined error message.

3. **`contexts.py`** — loads the **required** `config.yaml` into typed models. A
   *context* = a working `directory` + optional `model`/`effort` +
   `allowed_tools`/`additional_directories`. `default_context` covers unbound
   topics. (`allowed_tools` is parsed/validated but **not yet enforced** — see
   the deferred note in ADR-0012.)

4. **`bot.py`** *(the hub)* — builds the PTB `Application`, installs the allowlist
   filter (`filters.User` ⊕ optional `filters.Chat`), and wires handlers:
   - `_handle_message` — the round-trip. Resolves the topic→session, then launches
     `stream_reply` as a **background task** registered in `TurnRegistry` so the
     handler returns immediately and `/cancel` can interrupt it.
   - `/new`, `/context`, `/status`, `/cancel` command handlers.
   - `_handle_approval_callback` — handles taps on approval inline keyboards;
     **re-checks the trust boundary by hand** (CallbackQueryHandler has no filter).
   - `_open_context_topic` / `_topic_link` — `/context <name>` and `/new <name>`
     create a *brand-new* forum topic bound to that context and reply with a
     one-tap deep link (never rebinds an existing topic — one context per topic
     for life).

5. **`router.py`** — `TopicRef` → `ResolvedSession`. Maps a topic to its OpenCode
   session within its bound context; lazily **creates** a session on the first
   message, and **recreates** one that vanished server-side. Returns everything
   the streamer needs (session id, directory, provider/model/effort, extra dirs).

6. **`store.py`** — the persistence behind the router: a dependency-free
   `sqlite3` table mapping `(chat_id, thread_id) → (session_id, context)`. The
   General topic (no `message_thread_id`) normalizes to thread id `0`.

7. **`opencode.py`** *(the agent client)* — a thin hand-written `httpx` client
   over OpenCode's raw HTTP API (no SDK). Owns: Basic-auth transport, the
   readiness poll, `create_session` / `prompt_async` / `abort` / `reply_permission`,
   and the **SSE event stream** (`events()`). Two load-bearing details: it sets
   `ASK_ALL_PERMISSIONS` at session create (so OpenCode *asks* before every tool
   call), and the `directory` param on the event stream is **required** or you
   only get global events, never the session deltas.

8. **`streamer.py`** *(the most intricate file)* — `stream_reply` orchestrates one
   turn:
   - Subscribes to the SSE stream **before** prompting (no missed early deltas).
   - Accumulates interleaved assistant **text** + rendered **tool-call lines**
     (`_join_stream`), and a background loop flushes an animated Telegram **draft**
     (`send_message_draft`) every ~0.5s. `DraftSession` is the transport-agnostic,
     unit-tested core.
   - Handles `permission.asked` events: each spawns a task that calls
     `approvals.decide` and either auto-allows or sends an approval keyboard.
   - Finalizes into real message(s) on `session.idle`/`session.error`, splitting
     at Telegram's 4096-char cap. Degrades gracefully if drafts or MarkdownV2 fail.

9. **`approvals.py`** *(tool-approval policy)* — the directory-boundary decision
   plus `PendingApprovals` (token → future bridge for the inline keyboard).
   Decisions key on OpenCode's **permission category** (`read`/`edit`/`bash`/…),
   *not* tool names, so no mutating tool is ever missed. Policy: reads in-workspace
   auto-allow; edits in-workspace auto-allow only after "accept all edits";
   everything else (Bash, network, out-of-scope paths) asks. `is_within` uses
   `realpath` so symlinks/`..` can't escape the boundary.

10. **`turns.py`** — `TurnRegistry`: one in-flight turn per topic, so `/cancel`
    can abort it (local task + server-side abort) and `/status` can report it.

11. **`markdown.py`** — GFM (what the agent emits) → Telegram MarkdownV2 (a
    stricter, aggressively-escaped dialect) via `mistune`, then chunked to ≤4096
    chars at code-block-aware boundaries.

12. **`telegram_utils.py`** — tiny helper: `thread_kwargs` builds the
    `message_thread_id` kwarg that routes a send to the right forum topic.

## Features → where to look

| Feature | Code |
| --- | --- |
| Message round-trip (text in → streamed reply) | `bot.py:_handle_message` → `streamer.py:stream_reply` |
| Live animated streaming | `streamer.py` (`DraftSession`, `flush_loop`) |
| Tool-call lines in the reply | `streamer.py:_render_tool_part`, `_join_stream` |
| Interactive tool approval (buttons) | `approvals.py` + `streamer.py:request_approval` + `bot.py:_handle_approval_callback` |
| Workspace contexts | `contexts.py`, `config.yaml` (`config.example.yaml`) |
| Topic ↔ session mapping | `router.py` + `store.py` |
| Slash commands `/new /context /status /cancel` | `bot.py` (`_handle_*`, `BOT_COMMANDS`, `register_commands`) |
| Cancel a running turn | `bot.py:_handle_cancel` + `turns.py` + `opencode.abort_session` |
| Trust boundary / allowlist | `config.py` + `bot.py:build_application` (filters) |
| OpenCode HTTP/SSE | `opencode.py` |

## Running it

From `apps/backend` (or `uv --directory apps/backend`):

```
uv sync                                      # create venv + install
uv run balam                                 # run the bot (needs OpenCode + config.yaml + .env)
uv run pytest                                # tests  (uv run pytest -k <name> to filter)
uv run ruff check . && uv run ruff format .  # lint + format
```

Prereqs: a running **OpenCode server** (separate process, not in this repo),
`.env` (copy `.env.example`), and `config.yaml` (copy `config.example.yaml`). The
`run-balam` skill is the canonical way to start all backing processes locally.

## Things that will trip you up

- **OpenCode must be running** before the bot is useful — `app.py` blocks on
  `wait_for_ready` and the agent client talks to it over HTTP/SSE.
- **`directory` is everywhere on purpose.** Session create, prompt, abort, *and*
  the event subscription all carry it; OpenCode scopes session events to a
  worktree, so omitting it silently breaks streaming.
- **One context per topic, for life.** Switching context never rebinds — it opens
  a new topic. This keeps each topic's session history coherent.
- **`allowed_tools` enforcement is deferred.** It's validated but not wired into
  OpenCode yet; human approval (`approvals.py`) is today's backstop.
- The frontend is a **scaffold** — don't expect a working Mini App yet.

[OpenCode]: https://opencode.ai
