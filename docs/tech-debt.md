# Tech debt inventory

_Snapshot taken 2026-08-01, after the refactor on `refactor/tech-debt`. Backend
is 11,509 lines of Python across 44 modules; the frontend is a small React/Vite
app, ~1,435 hand-written lines plus a 310-line generated types file. Findings are
ordered by leverage (impact × how often the area changes), not by severity alone._

_Previous snapshots: 2026-08-01 (pre-refactor, `feature/scheduled-tasks` @
727d9b8) and 2026-07-13 (`main` @ c4355c2). Everything below was re-measured
against the current tree._

## How this was measured

- **Churn** = number of commits touching a file across all 170 commits. High
  churn + high line count = a hotspot where debt compounds fastest.
- Signals gathered: LOC/function counts, `git log` churn, duplicated
  vocabularies, broad `except`, `# type: ignore`, timing constants, type-sync
  mechanism, gitignore hygiene, test hermeticity, CI coverage, import cycles.
- Claims were checked by running things, not by reading alone.

---

## Tier 1 — structural, high-leverage

### 1. `streamer.py` is now the largest module (1,115 lines, 32 commits — #2 churn)

Its rendering layer moved out to `stream_render.py` (485 lines), which was the
split the previous snapshot called for. What remains is genuinely one job —
`DraftSession` and `stream_reply`: the draft/live-edit transport, the flush
loop, and the tail check that keeps the answer at the bottom of the topic.

It is still the biggest file in the backend, but it is no longer a _god module_
(one file holding several unrelated jobs). Splitting it further would mean
cutting into the streaming state machine itself, where the answer-at-tail and
collapsed-tool-stream invariants live. That is a real risk with a much smaller
payoff than the split already done.

**Direction:** leave it unless it grows again. If it does, the next natural seam
is the tool-burst collapsing state, not the transport.

### 2. The streaming invariants are under-specified in tests

The two subtle behaviours in `streamer.py` — the answer bubble being deleted and
re-sent whenever anything lands below it, and tool bursts collapsing into
expandable blockquotes — are load-bearing, and both were found by live testing
rather than by a failing test. `test_streamer.py` covers them partially, through
`DraftSession` with a fake transport.

This is now the highest-value work in the backend: it is what would make further
change to `streamer.py` safe, and it is the reason item 1 is left alone.

**Direction:** table-drive the tail-check cases (a message landing below the
bubble mid-stream, at finalize, and both) against the fake transport, so the
invariant is stated once in tests rather than implied across several scenarios.

---

## Tier 2 — worth doing

### 3. Broad `except Exception` (48 uses)

Unchanged in count, but no longer concentrated. Previously 38 of the 48 sat in
the two god modules; the largest single file now holds 16 (`streamer.py`), with
the rest spread thinly (`topics.py` 7, `turns.py` 3, `callbacks.py` 3,
`commands/delete.py` 3).

Most log at `debug` with `exc_info=True` or carry an explanatory comment. They
are mostly best-effort Telegram calls where failing loudly would be worse than
carrying on — a cosmetic keyboard refresh, or an error notice that itself fails.
Not silent swallowing, but the count only ever grows.

**Direction:** not a sweep. When touching one of these files, narrow the handler
to the exception actually expected.

### 4. `config.example.yaml` / `.env.example` drift

Still not checked; worth a periodic diff against `config.py`'s validated fields.
Cheap to automate now that CI exists — the same shape as the API drift check.

### 5. Scattered timing constants

`draft_interval`, `asyncio.sleep(0.05)`, `httpx.Timeout(connect=10, …)`,
`wait_for_ready(timeout=30, interval=0.5)`, `_FOREIGN_RESULT_GRACE_S`, and the
ADR-0015 background-work cap. Not centralized; harmless, but would be easier to
tune from one place.

---

## Tier 3 — minor / keep an eye on

- **`# type: ignore` at seams (2).** `server.py:227` casts `None` to a `Router`
  for a test-only construction; `config.py:196` `call-arg` for env-sourced
  settings. Both are load-bearing and commented.
- **Two deferred imports in `server.py`** (`contexts`, `store`, inside a
  function). These serve a test-only app construction rather than breaking a
  cycle. Worth confirming next time that file is touched.
- **Frontend test coverage is one module deep.** `resolveLaunch` is covered; the
  diff viewer and the noVNC client are not. A deliberate starting point, not a
  claim of coverage.

---

## What the refactor changed

All five structural items from the previous snapshot are resolved.

### `bot.py` split: 2,271 → 323 lines

It was the largest and most-changed file in the repo, holding the message
handler, all 12 slash commands, all 12 inline-keyboard callbacks, topic naming
and creation, turn running, and `build_application`. It is now the plain-message
path plus the registrar. Ten modules came out, one commit each:

| Module | What it owns |
| --- | --- |
| `message_text.py` | Turning a Telegram message into the text the agent sees |
| `topics.py` | Naming, opening and linking forum topics |
| `turns.py` | Running a turn (joined the existing turn data structures) |
| `auth.py` | The ADR-0008 trust boundary |
| `pickers.py` | The paged multi-select shared by `/delete` and `/schedule` |
| `callbacks.py` | Approval and question replies |
| `commands/session.py` | `/context` `/new` `/status` `/model` `/effort` `/rename` `/cancel` |
| `commands/views.py` | `/diff` `/browser` `/artifacts` |
| `commands/delete.py` | `/delete` and its callbacks |
| `commands/schedule.py` | `/schedule` and its callbacks |

The dependency arrow now points one way: `bot.py` imports these; none of them
imports `bot.py`.

### An import cycle is gone

`schedules.py` could not import `bot.py` at module scope — `bot.py` imports
`schedules` for the `/schedule` handlers — so it reached inside a function for
`TopicOpenError`, `open_topic_in_context` and `start_prompt`. All three now live
in `topics.py` and `turns.py`, neither of which imports `schedules`.
`schedules.py` has no deferred imports left.

### The tool registry (item 4 of the previous snapshot)

`tools.py` holds one `REGISTRY` of `ToolSpec` entries — wire name, display
label, permission category, SDK spellings. `streamer.py`, `claude_sdk_backend.py`
and `permissions.py` derive their lookups from it instead of keeping four partial
copies. The derived maps were diffed against the deleted ones and are identical,
except that `websearch` had been missing from the display map and so rendered
lowercase; completing the vocabulary fixed that.

### Tests are hermetic (item 5 of the previous snapshot)

An autouse fixture deletes any environment variable matching a `Config` field and
neutralizes the repo-root `.env` for every test. The suite now passes in the
environment Balam itself runs in.

Worth recording, because the previous snapshot's proposed fix was wrong:
`test_config.py` already passed `_env_file=None` with a comment claiming that
made the tests hermetic, and still failed. That only disables the file; real
environment variables bind at higher precedence.

### `claude_sdk_backend.py`: 1,067 → 820 lines

`agent/sdk_tasks.py` (the CLI task-list mirror and `LiveTasks`) and
`agent/sdk_translate.py` (the SDK↔OpenCode vocabulary boundary) came out. What
stays is the query loop, foreign-result detection, and the ADR-0015
background-hold policy.

### CI now exists, and covers more

`.github/workflows/ci.yml`: backend (`ruff check`, `ruff format --check`,
`pytest`), frontend (`typecheck`, `lint`, `test`), and the generated-API-types
drift check. The drift job was verified in both directions — it passes on a clean
tree and fails when a route is added to `server.py` without regenerating.

### The frontend has its first tests

`resolveLaunch` — how the Mini App decides what to show and which workspace to
act on — is covered by 10 `bun test` cases, wired into `bun run test` and CI.

---

## What is notably clean (not debt)

- **23 test modules, 594 backend tests plus 10 frontend tests**, all passing in a
  clean environment _and_ in the deployment environment.
- `ruff check`, `ruff format --check`, `bun run typecheck`, `bun run lint` and
  both test suites are green, with no suppressions beyond the two noted above.
- No import cycles anywhere in the backend; verified by importing every module.
- ADRs are thorough and referenced from code comments — the _why_ is captured.
- `api.ts` is a hand-written typed client over generated types, not a parallel
  type definition, and CI now proves the generated half is in sync.

Backend line count rose (11,068 → 11,509) despite no behaviour change. That is
the cost of the split: 13 new modules, each with a docstring explaining what it
owns and why it is separate. The trade is deliberate — total lines up about 4%,
while the largest file dropped by 86%.

---

## Recommended order of attack

1. **Pin the streaming invariants in tests (#2)** — the highest-value work left,
   and what makes any further change to `streamer.py` safe.
2. **Add the config-example drift check (#4)** — small, and CI is now there to
   run it.
3. **Leave `streamer.py` alone (#1)** unless it grows. The remaining split cuts
   into the state machine, so #2 should come first.
4. Narrow broad excepts opportunistically (#3), when already in the file.
