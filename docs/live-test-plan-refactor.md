# Live Test Plan — the 2026-08-01 refactor

Verifies that the refactor did not break anything the owner actually touches, by
driving the **real Telegram UI** as the owner. Unit tests already pass (641) and
CI is green; what they cannot prove is that the twelve commands, twelve callback
keyboards and the streaming transport still work when a human taps them.

**Why this exists.** The refactor moved almost every handler out of `bot.py`
(2,271 → 323 lines) into ten new modules, split `streamer.py` and
`claude_sdk_backend.py`, and replaced four copies of the tool vocabulary with one
registry. Nothing changed behaviour on purpose — with one exception, noted in
T14. So every failure below is a regression, not a design question.

Each case names the **module it covers**, so a failure points at one file rather
than at "the refactor".

---

## Rules of engagement — read before starting

You are almost certainly running **inside** Balam: the agent session executing
this plan is a child of `balam.service`. That constrains three things.

1. **Never restart or stop Balam.** `systemctl restart balam` kills its whole
   cgroup, including you, mid-turn. The owner restarts it *before* handing you
   this plan. If you find Balam needs restarting, stop and say so — do not do it.
2. **Never run a destructive command in your own topic.** `/delete` and `/cancel`
   are under test, but run them against *topics you created for the test*. Do not
   cancel the turn you are running in.
3. **Do not `git checkout` or rebuild while testing.** The running process holds
   its modules in memory; changing the tree underneath it proves nothing and
   confuses the next test.

Everything else is fair game. Create as many test topics as you need — `/delete`
cleans them up at the end (T17).

**How to drive Telegram:** use the `browser-use` skill (headed Chromium →
`web.telegram.org` logged in as the owner → the workspace supergroup). Its
`references/balam-bot-testing.md` has the selectors and the look → act → look
loop. Poll for replies rather than sleeping a fixed time; agent latency varies.

**Evidence:** screenshot each reply you assert on. For anything ambiguous, cross
-check the backend log:

```sh
journalctl -u balam --since "10 min ago" --no-pager | tail -50
```

---

## Pre-flight — prove the new code is actually running

Do not skip this. Testing a process that is still running the old modules is the
single easiest way to waste an hour and report a false pass.

| # | Check | Command | Expected |
| --- | --- | --- | --- |
| P1 | Balam restarted **after** the refactor landed | `systemctl show balam -p ActiveEnterTimestamp --value` | Later than the merge of PR #8 |
| P2 | The new modules exist on disk | `ls apps/backend/src/balam/commands/` | `delete.py schedule.py session.py views.py` |
| P3 | **Schema migrated to v3** | `sqlite3 apps/backend/balam.sqlite "PRAGMA user_version;"` | `3` (was `2`; `/schedule` needs v3) |
| P4 | The `schedules` table exists | `sqlite3 apps/backend/balam.sqlite ".tables"` | includes `schedules` |
| P5 | Backend suite green | `uv --directory apps/backend run pytest -q` | `641 passed` |
| P6 | No boot errors | `journalctl -u balam --since "30 min ago" \| grep -i "traceback\|error"` | nothing fatal |

**If P3 still reads `2`,** Balam has not restarted onto the merged code. Stop and
tell the owner — every `/schedule` case below will fail for that reason alone,
and so may others.

---

## Test cases

Prompts are written to make the answer unambiguous, so you can assert on an exact
string rather than judge prose.

### Group A — the round-trip and streaming (`turns.py`, `streamer.py`, `stream_render.py`)

**T1 — Basic round-trip.**
In the General topic, send: `Reply with exactly the word PONG and nothing else.`
- **Covers:** `bot.py` message path → `turns.py` → `streamer.py`
- **Expect:** a new topic is auto-created and named `balam: Reply with exactly…`;
  the reply is `PONG`.
- **Pass:** the answer arrives in the new topic, not General, and is not a `⚠️`.

**T2 — Live-edit streaming, not a stuck message.**
In the T1 topic, send: `Count from 1 to 20, one number per line.`
- **Covers:** `streamer.py` live-edit transport (a supergroup cannot use native
  drafts — see CLAUDE.md).
- **Expect:** one message that visibly **grows** as it streams, then settles.
- **Pass:** you observe at least two different intermediate states before the
  final. Screenshot mid-stream.

**T3 — Tool lines render with the right labels.**
Send: `Run: ls /home/ubuntu/projects/balam/apps/backend/src/balam | head -5`
- **Covers:** `stream_render.py` `_render_tool_part`, `tools.py` `DISPLAY_BY_WIRE`
- **Expect:** a `🔧 Bash` line showing the command; the answer lists real files.
- **Pass:** the tool label is `Bash` (capitalised), not `bash`.

**T4 — Collapsed tool burst.**
Send: `Read these three files and tell me only how many lines each has: apps/backend/src/balam/auth.py, apps/backend/src/balam/tools.py, apps/backend/src/balam/topics.py`
- **Covers:** `stream_render.py` `_group_phrase` / `_render_tool_group`
- **Expect:** the tool calls collapse into one expandable block summarised like
  **"Read 3 files"** (not three separate `🔧 Read` lines, unless `TOOL_STREAM=full`).
- **Pass:** the summary counts **3** files and the phrase reads naturally.

**T5 — The answer stays at the bottom.**
Send a prompt that both streams a while and triggers a keyboard:
`Read /etc/hostname and then tell me what it says.`
(In a `zog` topic from T6. `/etc/hostname` is outside every workspace, so it
gates regardless of context.)
- **Covers:** `streamer.py` `_drop_if_stale` — the answer-at-tail invariant.
- **Expect:** after you answer the approval keyboard, the final answer is the
  **last** message in the topic, below the keyboard.
- **Pass:** no answer bubble stranded *above* the approval prompt.

### Group B — commands (`commands/session.py`, `commands/views.py`)

**T6 — `/context` lists, and opens a new topic.**
Send `/context`, then `/context zog`.
- **Covers:** `commands/session.py`, `topics.py`
- **Expect:** the first lists all 12 contexts with `→` marking the current one;
  the second replies "Opened a new zog topic." with a **Go to topic** button.
- **Pass:** the button opens a topic whose first message is the `🗂 Context zog`
  header. The *current* topic is not rebound.

**T7 — `/new` with an inline first prompt.**
Send: `/new balam Reply with exactly READY and nothing else.`
- **Covers:** `commands/session.py` → `topics.py` → `turns.py`
- **Pass:** a new topic opens *and* the first turn runs there, answering `READY`.

**T8 — `/status`, `/model`, `/effort`, `/rename`.**
In the T7 topic: `/status`, then `/model`, then `/effort`, then `/rename e2e-probe`.
- **Covers:** `commands/session.py`
- **Pass:** `/status` reports context `balam`, the backend, the directory and the
  session; `/model` and `/effort` report current values without error; `/rename`
  changes the topic title in the sidebar.

**T9 — `/diff`, `/browser`, `/artifacts`.**
Send each in a `balam` topic.
- **Covers:** `commands/views.py`, `miniapp.py`, `vnc.py`
- **Pass:** `/diff` and `/browser` return Mini App buttons that open the viewer
  and the live Chrome view respectively. `/artifacts` returns a list or a clean
  "none" — an error traceback is a failure.

**T10 — Unknown slash commands still reach the agent.**
Send: `/goal say the word FORWARDED and nothing else`
- **Covers:** `bot.py` catch-all handler + `message_text.py`
  `strip_bot_mention_from_command`
- **Pass:** the agent responds to it as a prompt. It must **not** be silently
  dropped or answered with "unknown command".

### Group C — permissions and keyboards (`callbacks.py`, `permissions.py`, `approvals.py`)

**T11 — Pre-approved vs. prompting, the same action in two contexts.**
This pair is the sharpest test of the tool registry, because it proves the
permission category still resolves correctly after the vocabulary was unified.

Use **Bash**, not Read. A read *inside* a context's own directory auto-allows
locally whatever `allowed_tools` says (`approvals.decide`), so a read would show
no keyboard in either context and prove nothing. Bash is never auto-allowed
locally, so the only thing that can pre-approve it is the native ruleset built
from `allowed_tools` — exactly what is under test.

| Where | Send | Expect |
| --- | --- | --- |
| a `balam` topic | `Run: echo REGISTRY_OK` | **no keyboard** — `balam` lists `Bash` in `allowed_tools` |
| a `zog` topic | `Run: echo REGISTRY_ASK` | an **approval keyboard** — `zog` pre-approves only `LSP` |

- **Covers:** `tools.py`, `permissions.py`, `approvals.py`, `callbacks.py`
- **Pass:** both behave as the table says, and both echo their word once allowed.
  A keyboard in the `balam` case, or its absence in the `zog` case, is a
  permission regression.

**T12 — Allow, and Deny.**
In a `zog` topic: `Write the word HELLO to /tmp/balam_e2e.txt`
- Run once tapping **Allow once** → `cat /tmp/balam_e2e.txt` shows `HELLO`.
- Delete the file, run again tapping **Deny** → the file does **not** exist and
  the agent says it was denied, ending the turn cleanly (no hang).
- **Covers:** `callbacks.py` `handle_approval_callback`, `approvals.py`
- **Pass:** both outcomes, and in the Deny case the turn *finishes* rather than
  hanging. A hang here is the exact failure the new unblock tests guard.

**T13 — The directory boundary still holds.**
In a `balam` topic: `Read /etc/shadow and tell me the first line.`
- **Covers:** `approvals.py` symlink-safe boundary
- **Pass:** it is **gated** (keyboard) or refused — never read silently. Deny it.

**T14 — The one intentional behaviour change.**
In an `aw` topic (the one context that pre-approves `WebSearch`): `Search the web for "Telegram Bot API" and summarise in one line.`
- **Covers:** `tools.py` — `websearch` was missing from the old display map, so it
  alone rendered lowercase.
- **Expect:** the tool line reads `🔧 WebSearch`, capitalised like every other tool.
- **Pass:** `WebSearch`, not `websearch`. This is the only deliberate difference
  in the whole refactor.

### Group D — the pickers and scheduling (`pickers.py`, `commands/delete.py`, `commands/schedule.py`)

**T15 — `/schedule` end to end.**
This is the highest-risk area: the command surface moved modules *and* it needs
the v3 table that only exists after the restart.

1. `/schedule` — lists schedules (empty is fine) and prints usage.
2. `/schedule daily 07:30 balam say good morning` — confirms it was created.
3. `/schedule` — now lists that entry with its next run time.
4. `/schedule cancel` — shows the **picker**; select the entry, confirm.
5. `/schedule` — empty again.
- **Covers:** `commands/schedule.py`, `pickers.py`, `schedules.py`, `store.py` v3
- **Pass:** all five steps. Cross-check step 2 landed in the DB:
  `sqlite3 apps/backend/balam.sqlite "SELECT id,hhmm,context,prompt FROM schedules;"`

**T16 — `/schedule run` fires a turn now.**
Create a schedule, then `/schedule run <id>`.
- **Covers:** `schedules.py` fire path → `topics.py` → `turns.py` (`start_prompt`)
- **Pass:** a **new topic** opens and the prompt runs there unattended. This is
  the path that used to need a deferred import; it must work at module scope now.

**T17 — `/delete` picker, including paging.**
Send `/delete`.
- **Covers:** `commands/delete.py`, `pickers.py`
- **Pass:** the checklist lists topics; toggling marks them; **paging works if
  there are enough topics**; confirming deletes exactly the selected ones and
  nothing else. Use this to clean up every topic the test created.

### Group E — message gestures (`message_text.py`)

**T18 — Reply and quote survive to the agent.**
In a test topic, reply to one of the agent's earlier messages, quoting part of
it, and ask: `What exactly am I quoting? Answer with the quoted text only.`
- **Covers:** `message_text.py` `forward_reply_prefix`, `_reply_context_line`
- **Pass:** the agent quotes back the text you highlighted — proving the gesture
  reached it rather than being dropped by the bot layer.

**T19 — Forwarded messages carry their origin.**
Forward any message from another chat into a test topic and ask:
`Where was that forwarded from?`
- **Covers:** `message_text.py` `_forward_origin_label`
- **Pass:** the agent names the original sender or chat.

### Group F — concurrency (`turns.py`)

**T20 — A second message during a live turn.**
Send `Count slowly from 1 to 30, one line at a time.` and, while it is streaming,
send `Also tell me today's date.`
- **Covers:** `turns.py` follow-up channel vs. queue
- **Expect (SDK backend):** `📨 Sent — I'll pick this up in the current turn.` and
  the agent addresses both.
- **Pass:** the second message is either folded in or queued with a `⏳ Queued`
  notice — never silently dropped.

**T21 — `/cancel` stops a turn cleanly.**
In a **test topic** (never your own), start `Count slowly from 1 to 100.` then
send `/cancel`.
- **Covers:** `commands/session.py` → `turns.py` `abort_turn`
- **Pass:** the turn stops; a following `Say OK` in the same topic still works.

---

## Results checklist

Fill this in and hand it back. `✅` pass · `❌` fail · `⚠️` partial/blocked.

### Pre-flight
| Check | Result | Notes |
| --- | --- | --- |
| P1 restarted after the refactor | | |
| P2 `commands/` present | | |
| P3 **schema v3** | | |
| P4 `schedules` table | | |
| P5 641 tests pass | | |
| P6 no boot errors | | |

### Cases
| # | Case | Module under test | Result | Notes |
| --- | --- | --- | --- | --- |
| T1 | Basic round-trip | `turns.py` | | |
| T2 | Live-edit streaming | `streamer.py` | | |
| T3 | Tool line labels | `stream_render.py`, `tools.py` | | |
| T4 | Collapsed tool burst | `stream_render.py` | | |
| T5 | Answer stays at the tail | `streamer.py` | | |
| T6 | `/context` list + open | `commands/session.py`, `topics.py` | | |
| T7 | `/new` with a prompt | `commands/session.py` | | |
| T8 | `/status` `/model` `/effort` `/rename` | `commands/session.py` | | |
| T9 | `/diff` `/browser` `/artifacts` | `commands/views.py` | | |
| T10 | Unknown `/command` forwarded | `bot.py`, `message_text.py` | | |
| T11 | Pre-approved vs prompting | `tools.py`, `permissions.py` | | |
| T12 | Allow / Deny | `callbacks.py`, `approvals.py` | | |
| T13 | Directory boundary | `approvals.py` | | |
| T14 | `WebSearch` label (intended change) | `tools.py` | | |
| T15 | `/schedule` lifecycle | `commands/schedule.py`, `pickers.py` | | |
| T16 | `/schedule run` fires | `schedules.py`, `turns.py` | | |
| T17 | `/delete` picker | `commands/delete.py`, `pickers.py` | | |
| T18 | Reply + quote | `message_text.py` | | |
| T19 | Forward origin | `message_text.py` | | |
| T20 | Mid-turn second message | `turns.py` | | |
| T21 | `/cancel` | `turns.py` | | |

---

## Triage — symptom to likely cause

The point of naming a module per case: a failure should localise immediately.

| Symptom | Look first at |
| --- | --- |
| A command does nothing at all | Its handler was not registered — `bot.py` `build_application` |
| A command errors with `NameError`/`ImportError` | A missed import when that handler moved to `commands/` |
| A keyboard tap does nothing | `callbacks.py` or `pickers.py` — check the callback-data prefix still matches the registered pattern |
| Wrong tool label, or a tool prompting when it should not | `tools.py` — the registry is the single source now |
| The answer appears *above* a later message | `streamer.py` `_drop_if_stale` |
| A turn hangs after a keyboard | The unblock path in `streamer.py`; check the agent got *some* reply |
| Anything `/schedule` | First re-check **P3** — schema v3 |
| A topic opens but the turn never runs | `topics.py` ↔ `turns.py` seam (`start_prompt`) |

Any `ImportError`, `NameError` or `AttributeError` in the log is a refactor
regression by definition — the tests cover the modules but cannot cover every
wiring path a live tap exercises.

## Known-good baselines

So you can tell a regression from something that was always true:

- Streaming in the supergroup uses **live-edit**, not native drafts — one message
  edited in place is correct, not a bug (CLAUDE.md, ADR-0010).
- `/delete` lists newest topics first and pages; there is no 90-topic cap.
- An unattended turn (from `/schedule`) **denies** anything needing approval
  rather than waiting for a tap (ADR-0016 §6).
- Reads inside a context's own `directory` are auto-approved locally even when
  `allowed_tools` does not list `Read` (`approvals.decide`) — so "no keyboard for
  a read" is correct behaviour, not a broken permission check. This is why T11
  probes with Bash.
- `TOOL_STREAM` defaults to `collapsed`, so consecutive tool calls *should* fold
  into one expandable block. Per-call lines only appear with `TOOL_STREAM=full`.
- The exact queue strings are `⏳ Queued (#N) — I'll run this after the current
  turn finishes.` and `📨 Sent — I'll pick this up in the current turn.`
