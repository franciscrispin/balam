# Scheduled Tasks Implementation Plan — `/schedule`

> **Status (2026-07-31): built, steps 1–9.** Shipped as ADR-0016 and
> `balam.schedules`. Step 10 (retiring the cron script) is deliberately *not*
> done — it is gated on the new path running cleanly for a few days first.
>
> Three things differed from the plan as written, all verified against the code:
>
> - **PTB numbers weekdays Sunday=0**, not Monday=0 (`JobQueue._CRON_MAPPING ==
>   ('sun', 'mon', …)`). The `days` column keeps the plan's Python numbering
>   (`Mon=0`), and `schedules._ptb_days` is the single place that converts.
> - **Questions hang unattended too**, not just approvals: `asyncio.gather` over
>   the question futures has no timeout either. §6's rule therefore covers both —
>   an unattended turn rejects questions as well as denying tools.
> - **`/delete`'s picker was generalized rather than copied.** `PendingDeletions`
>   became `PendingPicks`, a paged multi-select over `(id, label)` pairs that both
>   commands share, which is what §5 was asking for.

Concrete plan for the **scheduled tasks** feature, originally deferred as Tier 3
in the core feature recommendations ("*whole workflow; not core to interactive
use yet*") — that roadmap has since been retired, every tier having shipped.

A schedule is a saved `(when, context, prompt)` triple. When it fires, Balam opens
a fresh forum topic bound to that context and runs the prompt in it — the same
thing `/new <context> <prompt>` already does, on a timer. The owner wakes up to a
topic holding the answer, and replies in it to keep working.

The driving case is Chaska's daily brief, which today runs from a **cron script
outside this repo** (`personal/chaska/scripts/daily-brief.sh`). That script
reaches into Balam's `.env` for the bot token and hand-writes a `topic_sessions`
row to bind the topic. It works, but it re-implements `_open_context_topic` badly
(no rollback on a failed bind), re-implements `markdown.py` worse (a `sed` that
turns `**x**` into `<b>x</b>`, and awk chunking), and pins Balam's schema from a
file no test covers. Shipping this feature deletes it.

## Why the Tier 3 deferral no longer holds

The deferral predates the machinery this feature needs. As of today:

- `bot._open_context_topic` (`bot.py:593`) already creates the forum topic, binds
  it via `router.create_topic_session`, **and rolls the topic back** by deleting
  it if the bind fails.
- `bot._start_turn` (`bot.py:493`) already runs a turn from a plain
  `(chat_id, thread_id, TurnJob)` — **it takes no `Message`**. The streaming,
  approval keyboards, follow-ups, and queue drain all hang off it.
- `store.SessionStore` already carries a `PRAGMA user_version` migration ladder,
  and its `check_same_thread=False` comment already anticipates "job-queue
  workers" touching the connection.

So the feature is mostly wiring, plus two genuinely new decisions (§6 and §7).

## Grounding facts (verified against the current code)

- `python-telegram-bot` is pinned `[httpx,rate-limiter]` in
  `apps/backend/pyproject.toml`. **The `job-queue` extra is not installed and
  `apscheduler` is absent** — `Application.job_queue` is therefore `None` today.
- `store.py` is at `PRAGMA user_version = 2` (`_migrate_auto_named` → 1,
  `_migrate_title` → 2). A new table takes **version 3**.
- `_open_context_topic(message, bot, router, name, *, prompt)` uses `message` for
  exactly three things: `message.chat_id`, `message.reply_text(...)` on error, and
  the "Opened …" reply carrying the deep-link button.
- `_submit_turn(message, context, text, files, *, thread_id, ...)` uses `message`
  for `message.chat_id`, `_topic_title(message, thread_id)`, and the queued-turn
  reply. Everything after that is `_start_turn`, which needs no message.
- `approvals.Verdict` has **only `ALLOW` and `ASK`** (`approvals.py:81`). There is
  no `DENY`, and `PendingApprovals` has no timeout — `streamer.py:1142` awaits the
  inline-keyboard future indefinitely.
- Commands are declared in `BOT_COMMANDS` (`bot.py:1709`), published by
  `register_commands`, and wired in `build_application` (`bot.py:1788+`) with the
  `allowed` filter (the ADR-0008 trust boundary).
- `/delete` (`bot.py:1388`) is the existing **paged multi-select picker** pattern
  (`PendingDeletions` + `del:` / `delp:` / `deld:` / `delx:` callbacks). `/schedule
  cancel` should copy it rather than invent a second picker idiom.
- `app._post_init` (`app.py:105`) is the startup hook — where catch-up belongs.
- `balam.service` runs `Restart=on-failure`, `RestartSec=5s`.

## Design decisions

**Dynamic, not `config.yaml`.** Schedules are created conversationally with
`/schedule` and stored in SQLite. Contexts live in `config.yaml` (ADR-0012)
because they are infrastructure — a directory and a tool policy. A schedule is
user data: created, listed and cancelled from the phone, and edited far more
often. Putting it in `config.yaml` would mean editing a file on the VM to stop a
7am message.

**A new `schedules.py`.** Not `bot.py`. At the time this was written `bot.py` was
the largest file in the repo at 1,827 lines and still growing. `bot.py` gets the
command handlers only; the store, the parser, and the fire path live in the new
module. (Since resolved: the command surface moved to `commands/schedule.py` in
the `commands/` split, and `bot.py` is now the registrar plus the message path.)

**PTB's `JobQueue`, not a hand-rolled loop.** `run_daily(callback, time=, days=)`
takes a tz-aware `datetime.time` and handles DST. This is why the scope below is
"daily / weekdays / one weekday" rather than general cron: `run_daily` covers
every case in hand with no parser and no APScheduler API surface of our own.

**Timezone is explicit.** The VM runs UTC; the owner lives in `Asia/Singapore`.
`/schedule daily 07:30` must mean 07:30 *Singapore*, or the feature is a bug
generator. Add a required-with-default `BALAM_TIMEZONE` config value and resolve
every schedule against it.

## Build order

```
1. dependency + JobQueue wiring        (nothing works without it)
2. schedules table + migration          (v3)
3. the message-free seam                (refactor; no behavior change)
4. schedules.py: parse, store, fire
5. /schedule command surface
6. unattended approval policy           ← new design, not wiring
7. missed-run catch-up                  ← new design, not wiring
8. tests
9. docs + ADR-0016
10. retire the chaska cron script
```

Steps 1–5 give a working feature. **6 and 7 are what make it trustworthy**, and
neither should be cut — see the risk note at the end.

---

## 1. Dependency + JobQueue wiring

- `pyproject.toml`: `python-telegram-bot[httpx,rate-limiter,job-queue]>=22.6`.
  This pulls `APScheduler` (and `pytz`) transitively. Run `uv sync` and commit the
  lockfile.
- `config.py`: add `timezone: str = "Asia/Singapore"` (env `BALAM_TIMEZONE`),
  validated through `zoneinfo.ZoneInfo` at load so a typo fails fast next to the
  other trust-boundary checks rather than at 07:30.
- `app.py`: nothing structural — `ApplicationBuilder` creates the job queue once
  the extra is installed. Confirm `application.job_queue is not None` in
  `_post_init` and log loudly if it isn't; a silent `None` here would mean every
  schedule quietly never fires.

## 2. Persistence — the `schedules` table

In `store.py`, a new table plus `_migrate_schedules` guarded by
`user_version >= 3`:

```sql
CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    context     TEXT    NOT NULL,
    prompt      TEXT    NOT NULL,
    kind        TEXT    NOT NULL,  -- 'daily' | 'weekdays' | 'dow'
    hour        INTEGER NOT NULL,
    minute      INTEGER NOT NULL,
    days        TEXT,              -- CSV of 0-6 (Mon=0) for 'dow'/'weekdays'
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL,
    last_run_at INTEGER            -- epoch ms; NULL until first fire
);
```

Methods, mirroring the existing store's plain-`sqlite3` style: `add_schedule`,
`list_schedules(chat_id)`, `get_schedule(id)`, `delete_schedule(id)`,
`set_enabled(id, bool)`, `mark_run(id, when_ms)`.

`last_run_at` is load-bearing for §7 — write it **when the run starts**, not when
the turn finishes, so a crash mid-turn doesn't re-fire the whole thing on restart.

## 3. The message-free seam

The refactor that lets a timer reuse the `/new` path. No behavior change; do it as
its own commit so the diff is reviewable against the existing tests.

**`_open_context_topic`** → split in two:

```python
async def open_topic_in_context(bot, router, chat_id, name, *, prompt="") -> int | None:
    """Create the topic, bind it, greet inside it. No originating message."""
```

keeping the existing rollback-on-bind-failure. The command wrapper keeps the
`message.reply_text` + deep-link button and simply calls it. The scheduled path
calls it directly and skips the reply — the topic itself is the notification.

**`_submit_turn`** → extract its head:

```python
async def start_prompt(bot, app_data, chat_id, thread_id, prompt, *, title) -> None:
    """Resolve the topic's session, build the TurnJob, hand it to _start_turn."""
```

`_submit_turn` becomes a thin wrapper that supplies `_topic_title(message, …)` and
owns the queued-turn reply. A scheduled run targets a *brand-new* topic, so it can
never collide with a running turn — the queue branch is unreachable there, which is
why the extraction is clean.

## 4. `schedules.py`

```python
@dataclass(frozen=True)
class Schedule:
    id: int; chat_id: int; context: str; prompt: str
    kind: str; hour: int; minute: int; days: tuple[int, ...]
    enabled: bool; last_run_at: int | None

def parse_when(tokens: list[str]) -> When            # "daily 07:30" → When(...)
def describe(s: Schedule) -> str                     # → "every day at 07:30"
def register_all(app, store, tz) -> int              # boot: DB rows → JobQueue jobs
def register_one(app, schedule, tz) -> None
def unregister(app, schedule_id) -> None             # job.schedule_removal()
async def fire(context, schedule_id) -> None         # the JobQueue callback
```

`fire` is the whole feature in ~20 lines: load the row, bail if disabled or the
context vanished from `config.yaml`, `store.mark_run(...)`, then
`open_topic_in_context(...)` → `start_prompt(...)`.

Name jobs `schedule:<id>` so `unregister` can find them via
`app.job_queue.get_jobs_by_name`, and re-registering after an edit can't leave a
duplicate behind.

**A vanished context must not be silent.** If `config.yaml` no longer has the
named context, `fire` should message the General topic saying the schedule is
parked, and disable it — not raise into the JobQueue's error handler where the
owner will never see it.

## 5. The `/schedule` command surface

```
/schedule                                 list schedules (id, when, context, prompt, last run)
/schedule <when> <context> <prompt>       create
/schedule cancel                          paged picker, mirroring /delete
/schedule run <id>                        fire once now — test it without waiting for 7am
/schedule off <id> · /schedule on <id>    disable / re-enable without losing it
```

`<when>` grammar for v1:

| Form | Example | → `run_daily` |
| --- | --- | --- |
| `daily HH:MM` | `daily 07:30` | every day |
| `weekdays HH:MM` | `weekdays 09:00` | `days=(0,1,2,3,4)` |
| `<dow> HH:MM` | `fri 17:00` | `days=(4,)` |

Reject anything else with the three examples above rather than a grammar dump.
Raw 5-field cron is a deferred extension — `JobQueue.run_custom` takes an
APScheduler trigger, so it slots in later without disturbing this.

Parse with `contexts.match_name(...)` for the context token (consistent with
`/context` and `/new`) and `_command_remainder(text, args_consumed=3)` for the
prompt, so a multi-line prompt survives — `context.args` is a whitespace split and
would collapse it.

Add `BotCommand("schedule", "Run a prompt on a schedule: /schedule daily 07:30 <context> <prompt>")`
to `BOT_COMMANDS`, one `CommandHandler` with the `allowed` filter, and callback
handlers for the picker (`sch:` / `schp:` / `schd:` / `schx:` — distinct prefixes,
since PTB matches the first pattern that hits).

`/schedule run <id>` is worth building early: it turns a 24-hour feedback loop
into a 5-second one for everything else on this list.

## 6. Approvals with nobody watching ← new design

**The problem.** `decide()` returns `ALLOW` or `ASK`, and `ASK` awaits an
inline-keyboard tap with no timeout (`streamer.py:1142`). At 07:30 nobody taps. A
scheduled turn that reaches for `Bash` parks forever, holding its slot in
`TurnRegistry` — and because it holds the slot, the owner's own reply in that
topic queues behind a turn that will never finish. One unattended `ASK` wedges the
topic until a manual `/cancel`.

**The decision.** Add `Verdict.DENY` and an `unattended: bool` on the policy call.
For an unattended turn:

- reads inside the workspace → `ALLOW` (unchanged)
- everything else → `DENY`, with the denial surfaced in the topic as a normal
  tool line ("🚫 denied — Bash, scheduled run")

so the agent gets a refusal it can reason about and finish its turn, and the owner
can read what it wanted and re-run by hand.

**Where `unattended` stops.** It must be a property of the *turn*, not the topic.
Once the owner replies in the morning's topic, that reply is attended and gets the
normal keyboard. Thread it through `TurnJob` and clear it on any turn that starts
from a `Message`.

This preserves the Chaska brief's current read-only behaviour exactly, and it is
the honest default: a 7am job that can silently `rm -rf` on an ambiguous prompt is
worse than one that reports it was blocked.

## 7. Missed runs ← new design

`JobQueue` is in-memory. cron survives a Balam restart; this does not. Balam is
`Restart=on-failure` and gets restarted by hand on deploy — a restart at 07:29
loses the brief with **no trace anywhere**, which is precisely the failure mode
that erodes trust in a scheduled feature.

In `_post_init`, after `register_all`, run a catch-up pass:

> For each enabled schedule, compute its most recent due time in the configured
> timezone. If that time is in the past, later than `last_run_at`, and within a
> `CATCH_UP_WINDOW` (propose **6 hours**), fire it now and note in the topic that
> it is a late run.

The window matters: without it, a VM that was off for a week produces seven
topics at boot. Outside the window, log it and skip — a 3-day-old daily brief is
noise, not information.

## 8. Tests

Follow `tests/` conventions (`pytest-asyncio`, fake bot objects as in
`test_bot.py`):

- `test_schedules.py` — `parse_when` accepts the three forms and rejects junk;
  `describe` round-trips; catch-up fires only inside the window and only when
  `last_run_at` is older than the due time; DST boundaries (Singapore has none,
  but the tz-aware path shouldn't assume that).
- `test_store.py` — additions: schedule CRUD; the v2→v3 migration is a no-op on a
  fresh DB and lands on an existing one.
- `test_bot.py` — additions: `/schedule` create/list/cancel; a schedule naming an
  unknown context is refused at creation; the picker's callbacks.
- `test_approvals.py` — additions: `unattended=True` returns `DENY` for bash/edit
  and still `ALLOW`s in-workspace reads.
- One integration-style test that `fire` opens a topic and starts a turn, with the
  backend faked — the seam from §3 is what makes this testable without Telegram.

## 9. Docs

- **ADR-0016: Scheduled prompts are stored, not configured.** Decision: schedules
  are user data in SQLite driven by `/schedule`, not `config.yaml` entries;
  unattended turns deny anything past an in-workspace read; missed runs catch up
  within a bounded window. Consequences: a second, timer-driven entry point to the
  agent now exists — it is *internal* (no new external surface, ADR-0008's
  Telegram gate still governs everything a human sends), but it is the first path
  that starts a turn with no human in the loop, and that is what §6 constrains.
- `codebase-guide.md` — add `schedules.py` to the module list and a
  "Scheduled tasks" row to the features table.
- ~~`tech-debt.md`~~ — done, and the inventory has since been retired: the
  `commands/` split took the `/schedule` handlers out of `bot.py`.

## 10. Retire the cron script

Once `/schedule daily 07:30 chaska plan my day` has run cleanly for a few days:

1. `crontab -r` on the VM (the only entry is the brief).
2. Delete `personal/chaska/scripts/daily-brief.sh`.
3. Update `personal/chaska/CLAUDE.md` — the "runs on its own every morning"
   section describes the cron path and would otherwise go stale.
4. Keep `~/.chaska/` (the dated brief archive) — it is independent of the mechanism.

Note the one real regression: **cron survives Balam being down; `/schedule` does
not.** §7's catch-up narrows that to "Balam was down across the due time *and*
stayed down more than 6 hours". Accept it, or keep a cron-based watchdog — but
don't leave it undecided.

---

## Deferred

- Raw cron expressions (`run_custom` + an APScheduler `CronTrigger`).
- One-shot reminders (`run_once`) — same table with `kind='once'`.
- Reusing an existing topic instead of opening a new one per fire. The
  per-day-topic model is deliberate: it keeps each brief's session history
  separate, which is exactly ADR-0009's reasoning applied to time.
- Schedules that target the *General* topic rather than opening one.

## The risk worth naming

The tempting version of this feature is steps 1–5: a timer that calls `/new`.
That is two days of work and it will look finished. It will also, on some morning
nobody is watching, either hang on an approval nobody taps (§6) or silently skip a
day after a deploy (§7) — and both failures are invisible, which is the worst
property a scheduled job can have. The current cron script avoids both only
because it is crude: a fixed read-only allowlist and an OS-level scheduler.
Whatever replaces it has to be at least as boring in those two respects.
