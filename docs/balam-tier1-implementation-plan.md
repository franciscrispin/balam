# Tier 1 Implementation Plan — the chat loop

Concrete plan to build [Tier 1](./balam-core-feature-recommendations.md): session
commands, tool-call visibility, interactive approval, and attachments. Everything
here extends the existing backend (`apps/backend/src/balam/`); no Mini App work.

Grounding facts (verified against the current code and the OpenCode-backed
reference, open-shrimp):

- The SSE loop lives in `opencode.events()` and is consumed in
  `streamer.stream_reply` → `consume()`. New event types are handled by adding
  branches there.
- Commands are registered in `bot.BOT_COMMANDS` + `register_commands`, and wired in
  `build_application` with the `allowed` filter (the ADR-0008 trust boundary).
- The topic→session map is `store.SessionStore` (`get_row` / `set` / `delete`);
  `router.resolve` lazily (re)creates a session, so deleting a row is enough to start
  fresh.
- OpenCode endpoints to add to the `OpenCode` client: `POST /session/{id}/abort`
  (cancel) and `POST /permission/{id}/reply` (approval). OpenCode signals an approval
  request with a **`permission.asked`** SSE event whose `permission` property is a
  *category* (e.g. `"edit"`, `"bash"`), not a tool name.

## Build order (dependencies matter)

```
1. /new, /status, /cancel   (independent; ship first)
2. tool-call visibility      (adds a tool-part cache the approval step reuses)
3. interactive approval      (needs #2's tool-part cache + a new approvals module)
4. attachments               (registers its upload dir in #3's auto-approve set)
```

---

## 1. Session commands: `/new`, `/status`, `/cancel`

**Goal:** start a fresh session in the current topic, report state, abort a turn.

**Changes:**

- `opencode.py`: add `async def abort_session(self, session_id, *, directory)` →
  `POST /session/{id}/abort` (best-effort; log on failure). Mirrors open-shrimp's
  `abort_session`.
- New `turns.py` (small): a `TurnRegistry` mapping `(chat_id, thread_key)` →
  `{task: asyncio.Task, session_id: str}`. `stream_reply` is currently awaited
  directly in `bot._handle_message`; wrap it in a task, register on start, clear in a
  `finally`. This handle is what `/cancel` needs.
- `bot.py`:
  - `_handle_new`: `router`’s store row is dropped (`store.delete`) so the next
    message lazily recreates the session via `router.resolve`; also cancel any
    in-flight turn for the topic. Reply "🆕 Started a new session." (The old OpenCode
    session is left orphaned server-side but unreferenced — consistent with the
    lazy-create model.)
  - `_handle_status`: read `router.current_context_name(ref)`, the bound row’s
    `session_id`, and the context’s `directory`/model/effort from `contexts`; reply a
    plain-text summary. Note whether a turn is currently running (registry lookup).
  - `_handle_cancel`: look up the registry; `task.cancel()` **and**
    `opencode.abort_session(session_id, directory=...)`; reply "🛑 Cancelled." or "No
    running turn."
  - Add the three to `BOT_COMMANDS` and register a `CommandHandler` for each with the
    `allowed` filter.

**Tests:** `test_bot.py` — `/new` deletes the row then a message recreates; `/status`
text contains context + session; `/cancel` cancels a registered task and calls abort
(fake OpenCode). Reuse the existing fakes.

---

## 2. Tool-call visibility in the stream

**Goal:** show what the agent *did*, not just its prose — prerequisite for approvals
to be meaningful.

**Changes:**

- `streamer.py` `consume()`: today it only handles `part.get("type") == "text"`. Add a
  branch for `part.get("type") == "tool"`:
  - Maintain a `tool_parts` cache keyed by `callID`, holding `(tool, input, status)`
    from `part["state"]` (`state.input`, `state.status`). This **doubles as the cache
    the approval step reads** (#3), so build it cleanly here.
  - When a tool part reaches a terminal status (`completed`/`error`), render a compact
    line into the stream: e.g. `🔧 Read src/foo.py` and, for `Bash`, the command plus
    output **truncated to ~50 lines / 1500 chars** (open-shrimp’s caps) with a
    "(truncated)" marker. Full output goes to the Mini App later (Tier 2/3); for now,
    inline-truncate.
  - Interleave tool lines with text parts in arrival order (extend the existing
    `order` counter to cover both, so the final message reads top-to-bottom correctly).

**Tests:** `test_streamer.py` — feed a fake event sequence (assistant text + a tool
part) and assert the finalized message contains the formatted tool line and respects
the truncation cap.

---

## 3. Interactive tool approval

**Goal:** gate mutating/out-of-scope tool calls behind a Telegram inline keyboard;
auto-approve reads inside the workspace. This is the bulk of Tier 1.

**Mechanism (from open-shrimp, the OpenCode-backed reference):** OpenCode fires
`permission.asked` on the SSE bus; the bot decides, then replies with
`POST /permission/{id}/reply` carrying `{"reply": "once"}` / `{"reply": "always"}`
(allow) or `{"reply": "reject", "message": …}` (deny).

**Changes:**

- `opencode.py`: add `async def reply_permission(self, request_id, reply, *, message=None)`
  → `POST /permission/{id}/reply`.
- New `approvals.py`:
  - `category_to_tool`: map the `permission.asked` `permission` category → a tool name
    (`edit`→Edit/Write, `bash`→Bash, …), disambiguating via the cached tool part for
    the `callID` when needed (lift open-shrimp’s `tool_names` table).
  - `is_within(path, dirs)`: realpath prefix check —
    `real == d or real.startswith(d + os.sep)` — over `ctx.directory` +
    `additional_directories` + the attachment upload dir.
  - Decision function: reads inside the allowed dirs → auto-allow (`reply="once"`);
    mutations inside allowed dirs → keyboard unless the session’s **accept-all-edits**
    flag is set; anything out-of-scope → keyboard. A per-session `set[str]` /
    `bool` holds session-level "always"/"accept all edits" state.
  - `PendingApprovals`: maps a short callback token → `asyncio.Future[bool]`, resolved
    by the callback handler.
- `streamer.py`: in `consume()`, add a `permission.asked` branch (filtered by
  `sessionID`) that spawns a task: recover `tool_input` from the tool-part cache (#2),
  run the decision function, and either reply to OpenCode directly (auto cases) or send
  an inline keyboard (`Allow once` / `Accept all edits` / `Deny`) and await the
  `Future` before replying. Spawn per request so the stream loop isn’t blocked.
- `bot.py`: register a `CallbackQueryHandler` (with the `allowed` filter) that parses
  the token + choice and resolves the matching `Future`; record "accept all edits"
  when chosen.

**Verify before building (load-bearing):**

- **Does OpenCode ask at all?** Confirm the server is configured so edit/bash raise
  `permission.asked` rather than auto-running. If it auto-runs, set the session’s
  permission policy on create (`POST /session`) / prompt so it asks. Inspect the
  pinned OpenCode’s `/doc` for the permission schema and the exact `permission.asked`
  property names before committing the effort estimate.

**Tests:** `approvals.py` unit tests for `is_within` (in/out of dir, symlinks via
realpath) and the decision matrix; `test_streamer.py` for the permission→reply path
with a fake that captures the `reply_permission` call.

**Scope note:** this ships the **directory-boundary** routing only. The `allowed_tools`
hard-enforcement engine stays deferred (ADR-0012); human approval is the backstop.

---

## 4. Inbound file attachments

**Goal:** accept images / PDFs / text files and let the agent read them.

**Changes:**

- New `attachments.py`:
  - `UPLOAD_BASE = Path(tempfile.gettempdir()) / "balam_uploads"`.
  - `save_attachments(files, thread_key) -> list[Path]`: write each to
    `UPLOAD_BASE / str(thread_key) /` via `NamedTemporaryFile(delete=False, dir=…)`
    with a `balam_<sanitized-name>_` prefix and a MIME-derived suffix. (Key by
    **thread/topic**, not chat, to match Balam’s one-topic-one-session model.)
  - `cleanup(paths)`: delete after the turn (call from a `finally`).
  - Expose `UPLOAD_BASE` so `approvals.is_within` includes
    `UPLOAD_BASE / str(thread_key)` in the auto-approve dirs (so reading an upload
    needs no prompt — exactly how both reference apps wire it).
- `bot.py` `_handle_message`:
  - Broaden the `MessageHandler` filter from `filters.TEXT` to
    `(filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND & allowed`.
  - Download the attachment bytes (PTB `get_file` → `download_as_bytearray`), save via
    `save_attachments`, and build the prompt: the caption/text plus a line naming each
    saved path so OpenCode reads it. Pass through to `stream_reply`; `cleanup` in
    `finally`.

**OpenCode visibility:** Balam runs OpenCode locally on the same VM (ADR-0001/0007,
un-sandboxed), so a `/tmp/balam_uploads/...` path is directly readable. If a future
ADR sandboxes OpenCode, this dir must be mounted in — leave a comment at the
definition site.

**Tests:** `test_attachments.py` — save writes to the per-thread dir with the right
suffix; `cleanup` removes it. `test_bot.py` — a photo message saves a file and the
prompt references its path (fake OpenCode/bot).

---

## New modules / touch list

| File | New? | Purpose |
| --- | --- | --- |
| `opencode.py` | edit | `abort_session`, `reply_permission` |
| `turns.py` | new | per-topic in-flight turn registry (for `/cancel`) |
| `approvals.py` | new | category→tool map, `is_within`, decision, pending-future registry |
| `attachments.py` | new | upload-dir storage + cleanup |
| `streamer.py` | edit | tool-part cache + rendering; `permission.asked` handling |
| `bot.py` | edit | `/new` `/status` `/cancel` handlers, callback handler, broadened message filter, `BOT_COMMANDS` |

## Open questions to resolve first

1. **OpenCode permission policy** — confirm/enable that the server raises
   `permission.asked` for edit/bash, and learn the exact event property shape from
   `/doc`. (Blocks #3.)
2. **`/new` semantics** — drop-the-row + lazy-recreate (recommended, simplest) vs.
   eagerly create + greet. Pick one.
3. **Tool-output overflow** — inline-truncate for Tier 1 (recommended) vs. wait for
   the Mini App document view (Tier 2). Truncating now is fine and reversible.
