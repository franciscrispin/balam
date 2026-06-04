# Plan: initial Telegram bot ↔ agent round-trip over forum topics

Status: Implemented (in **Python** per ADR-0011) Date: 2026-05-21

> **Implementation note.** This plan was written when the backend was TypeScript.
> It was implemented in Python after ADR-0011 reversed the language choice. The
> design below is unchanged in intent; only the language and a few mechanics
> differ. File mapping (`apps/backend/src/balam/`):
>
> | Plan component | Implemented as            | Notes                                              |
> | -------------- | ------------------------- | -------------------------------------------------- |
> | `config.ts`    | `config.py`               | `pydantic-settings`, fail-fast.                    |
> | `store.ts`     | `store.py`                | stdlib `sqlite3` (not `bun:sqlite`).               |
> | `opencode.ts`  | `opencode.py`             | raw `httpx` HTTP/SSE (not `@opencode-ai/sdk`).     |
> | `router.ts`    | `router.py`               | unchanged.                                         |
> | `streamer.ts`  | `streamer.py` + `markdown.py` | `send_message_draft` streaming + GFM→MarkdownV2 via `mistune`. |
> | `bot.ts`       | `bot.py`                  | `python-telegram-bot`; allowlist via `filters.User`. |
> | `index.ts`     | `app.py`                  | boot via PTB `run_polling` + `post_init`/`post_shutdown`. |
>
> The "streamed reply" path uses native `send_message_draft` (it **does** work in
> forum topics with `message_thread_id`), not the throttled `editMessageText`
> fallback this plan's draft originally assumed.

This plan implements the first working slice of Balam: a single user messaging
the bot inside Telegram **forum topics**, with each topic mapped to its own
OpenCode session. It builds directly on the ADRs in
[`architecture-decisions.md`](./architecture-decisions.md) — chiefly ADR-0008
(allowlist by user ID) and ADR-0009 (one forum topic = one OpenCode session).

## Decisions for this slice

| Decision            | Choice                                     | Rationale                                                   |
| ------------------- | ------------------------------------------ | ----------------------------------------------------------- |
| Scope               | Core round-trip only                       | Smallest shippable slice; no commands/Mini App yet.         |
| Update mode         | **Long polling** (`bot.start()`)           | No public URL/TLS; fits localhost-only posture (ADR-0007).  |
| Topic→session store | **SQLite** (`bun:sqlite`)                  | Survives restart (ADR-0009); built-in, no extra dependency. |
| Reply delivery      | **Streamed** via native `sendMessageDraft` | Live UX; native streaming since Bot API 9.5 (see below).    |

## Goal & boundary

One message in a forum topic → routed to that topic's OpenCode session → the
agent's reply streamed back into the same topic. Allowlisted to one user.

- **In scope:** config validation, OpenCode health-check + client, allowlist
  guard, topic→session SQLite map, lazy session creation, streamed reply.
- **Out of scope (later slices):** slash commands, Mini App, noVNC / live
  Chrome, git diffs, webhooks, onboarding helpers.

## Manual prerequisites (operator, before/while building)

1. **BotFather**: create the bot, put the token in `.env`
   (`TELEGRAM_BOT_TOKEN`).
2. **Numeric Telegram user ID** → `ALLOWED_TELEGRAM_USER_ID` (ADR-0008).
3. **A group with Topics enabled** (a "forum"), bot **added as admin** _or_
   **privacy mode off** (`/setprivacy` in BotFather) — otherwise Telegram does
   not deliver ordinary topic messages to the bot. This is the most common
   gotcha.
4. **OpenCode server** running on `127.0.0.1:4096` with
   `OPENCODE_SERVER_PASSWORD` set (ADR-0001 / 0007). The backend waits for it on
   boot.

## Components (new files under `apps/backend/src/`)

| File          | Responsibility                                                                                                                                                                                                                                               | ADR         |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------- |
| `config.ts`   | Load + validate env; fail fast with a clear message if a required var is missing.                                                                                                                                                                            | 0008 / 0007 |
| `opencode.ts` | Wrap `@opencode-ai/sdk`: health-check (poll `/doc` / event stream) and **wait** for readiness; create session; send prompt; expose the SSE event stream.                                                                                                     | 0001 / 0002 |
| `store.ts`    | `bun:sqlite` DB. Table `topic_sessions(chat_id, thread_id, session_id, created_at)`. Lookup/insert the mapping. Survives restart.                                                                                                                            | 0009        |
| `router.ts`   | Resolve `(chat_id, message_thread_id)` → `session_id`; create an OpenCode session lazily on the first message in a topic; General topic = catch-all session.                                                                                                 | 0009        |
| `streamer.ts` | Consume OpenCode SSE → stream via native **`sendMessageDraft`** as text is generated, split at Telegram's 4096-char limit, finalize with `sendMessage`. Fall back to a throttled `editMessageText` loop (~1 edit/sec) where `sendMessageDraft` does not fit. | 0010        |
| `bot.ts`      | grammY bot: allowlist middleware (silently drop non-owner), `message:text` handler wired through router + streamer, long-polling via `bot.start()`.                                                                                                          | 0008        |
| `index.ts`    | Orchestrate: validate config → wait for OpenCode → open SQLite → start bot → graceful shutdown. Replaces the current scaffold stub.                                                                                                                          | all         |

`packages/shared` only needs an addition if a type is genuinely shared (e.g. a
topic-key shape); for this slice the backend can stay self-contained.

## Build order (each step independently runnable)

1. **`config.ts`** — validate env, surface what's missing.
2. **`store.ts`** — SQLite map + unit tests (in-memory DB; no Telegram/OpenCode
   needed).
3. **`opencode.ts`** — health-check + minimal "create session, send prompt, get
   streamed events" against the running server.
4. **`bot.ts` allowlist + echo** — prove updates arrive, owner-only, topic-aware
   (echo `message_thread_id` back). Verifies the manual setup (admin/privacy).
5. **`router.ts`** — wire topic→session, lazy create, persist.
6. **`streamer.ts`** — replace echo with the real streamed agent reply.
7. **`index.ts`** — full boot sequence + graceful shutdown.

## Streaming detail

Telegram **does** have native streaming: `sendMessageDraft` renders partial
messages as they are generated, without flicker (ADR-0010). The primary path is:
stream OpenCode's SSE through `sendMessageDraft`, then finalize with a real
`sendMessage`. Must handle:

- the 4096-char split (start a new message when a part fills up),
- finalizing the draft into a sent message,
- the fallback path — one `sendMessage` + throttled `editMessageText`
  (~1 edit/sec per chat; 20/min in a group) — for any case `sendMessageDraft`
  does not cover, with the same split/flush handling.

References: [Bot API](https://core.telegram.org/bots/api), [Bot API
changelog](https://core.telegram.org/bots/api-changelog) (`sendMessageDraft`),
[rate limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).
Forum topics are addressed by the `message_thread_id` field.

## Testing & verification

- **Unit (`bun:test`):** store CRUD, allowlist guard, message-splitter, and
  edit-throttle logic — all pure/deterministic.
- **Manual:** new topic → first message creates a session and streams a reply; a
  second topic gets an independent session; restart the backend → existing
  topics still resolve their sessions (proves SQLite persistence); a non-owner
  message is ignored.

## Edge handling (minimal for this slice)

- Session missing after a server restart → recreate (don't silently drop,
  ADR-0009).
- OpenCode error → post a short error message into the topic.
- Non-allowed user → silent drop.
