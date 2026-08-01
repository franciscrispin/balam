# Tech debt inventory

_Snapshot taken 2026-08-01, `feature/scheduled-tasks` @ 727d9b8 (open PR #6, so
these numbers include `/schedule`). Backend is 11,068 lines of Python across 31
modules; the frontend is a small React/Vite app, ~1,334 hand-written lines plus a
310-line generated types file. Findings are ordered by leverage (impact × how
often the area changes), not by severity alone._

_Previous snapshot: 2026-07-13, `main` @ c4355c2. Everything below was
re-measured against the current tree; see "Changes since the last snapshot" for
what turned out to be wrong._

## How this was measured

- **Churn** = number of commits touching a file across all 149 commits. High
  churn + high line count = a hotspot where debt compounds fastest.
- Signals gathered: LOC/function counts, `git log` churn, duplicated
  vocabularies, broad `except`, `# type: ignore`, timing constants, type-sync
  mechanism, gitignore hygiene, test hermeticity, CI coverage.
- Claims were checked by running things, not by reading alone. The test finding
  below was reproduced in a clean `git worktree` with the environment varied.

---

## Tier 1 — structural, high-leverage

### 1. `bot.py` is a god module (2,271 lines, 73 functions, **44 commits — #1 churn**)

A _god module_ is one file that holds too many unrelated jobs. This one holds:
the message handler, **every** slash-command handler (12 of them: `/new`,
`/rename`, `/status`, `/model`, `/effort`, `/cancel`, `/context`, `/diff`,
`/browser`, `/artifacts`, `/delete`, `/schedule`), **all 12** inline-keyboard
callback handlers (approvals, questions, and the delete and schedule pickers'
paging/confirm), topic naming, keyboard construction, cleanup scheduling, and
`build_application`. It is both the largest and most-changed file, so almost
every feature edit lands here and the merge/reasoning surface is large.

It grew 616 lines (+37%) and 10 commits since the last snapshot, mostly from
`/schedule`. It is growing faster than the rest of the backend.

**Direction:** split by concern — e.g. `commands/` (one module per command
group), `callbacks.py` (inline-keyboard routing), `topics.py` (naming/rename/
create). `build_application` becomes a thin registrar. This is still the single
highest-value refactor.

The `/schedule` work already moved in the right direction: it kept its store,
parser and fire path out in `schedules.py`, and the message-free seam it needed
(`open_topic_in_context`, `start_prompt`) is the `topics.py` extraction above,
done in place. So the `commands/` split has less to untangle than it would have,
but more lines to move.

### 2. `streamer.py` is the second god module (1,573 lines, 44 functions, 30 commits — #2 churn)

Owns draft transport, live-edit fallback, tool-burst collapsing, reasoning
overflow ordering, tool label rendering, the live todo checklist, and
finalization. The "answer-stays-at-tail" invariant and the collapsed-tool-stream
logic are subtle and concentrated here. Worth extracting the rendering/label
layer from the transport/flush layer.

Grew 237 lines and 7 commits since the last snapshot.

### 3. `agent/claude_sdk_backend.py` is a new hotspot (1,067 lines, 27 functions, 23 commits)

**New in this snapshot** — it was not called out as a hotspot before, and it
should have been. It is now the third-largest and third-most-changed backend
module (23 commits across its whole history), and it grew 431 lines in the 8
commits since c4355c2.

It carries several independent concerns: SDK message translation, the
`_WIRE_TOOL`/`_CATEGORY` name maps, the TaskCreate/TaskUpdate → synthetic
`todowrite` mirror, foreign-result detection (`_FOREIGN_RESULT_GRACE_S`), and the
background-work turn-holding policy of ADR-0015. The last two encode timing
policy that is easy to break by accident and hard to test.

Unlike `bot.py`, this one is not obviously over-large yet — flagging it now so it
gets split before it reaches `bot.py`'s size, not after.

### 4. Tool & permission vocabulary is duplicated across 4+ files

The same conceptual tool set (`read`/`edit`/`bash`/`grep`/…) is redefined, in
slightly different string forms, in:

| File | Shape |
| --- | --- |
| `opencode_tools.py` | `Tool` enum **and** `Permission` enum |
| `permissions.py` | permission list / `parse_allowed_tool` |
| `streamer.py` | `_TOOL_DISPLAY` (tool → label) |
| `agent/claude_sdk_backend.py` | `_WIRE_TOOL` (SDK name → wire) + `_CATEGORY` |

Adding or renaming a tool means editing all of them, and the SDK↔OpenCode name
mapping (`LS`→`list`, `MultiEdit`→`edit`) lives only in `_WIRE_TOOL`. There is no
single source of truth for "what tools exist and how each backend spells them."

Unchanged since the last snapshot.

**Direction:** one canonical tool registry (name, wire form, display label,
permission category) that all four consumers derive from.

---

## Tier 2 — worth doing

### 5. Tests are not hermetic: they read the real process environment

`Config` is a pydantic-settings model. Real environment variables take precedence
over the repo-root `.env`, and the test fixtures do not isolate either one. So
the suite's result depends on what is exported in the shell that runs it.

On a machine with Balam's deployment environment exported, **5 tests fail**:

- `test_miniapp.py` — `test_mini_app_reply_localhost_text_only`,
  `test_markdown_button_none_without_public_url`,
  `test_markdown_button_plain_url_without_shortname` (all from `BALAM_PUBLIC_URL`)
- `test_agent_tools.py::test_send_file_markdown_without_public_url_sends_without_button`
- `test_config.py::test_agent_backend_defaults_to_opencode` (from `AGENT_BACKEND=claude_sdk`)

Reproduced 2026-08-01: 581 passed / 5 failed with the deployment environment
exported; **586 passed** with those variables cleared, in the same clean worktree.

This matters most for Balam itself. The bot runs under systemd with that whole
environment set, so an agent session running inside Balam inherits it — Balam
cannot correctly run its own test suite, while a plain developer shell and CI both
see green. That asymmetry is exactly the kind that wastes debugging time.

**Direction:** make `make_config` (and the direct `Config()` constructions in
tests) independent of the ambient environment — clear the `BALAM_*` /
`AGENT_BACKEND` / `TELEGRAM_*` / `OPENCODE_*` variables in an autouse fixture, and
pass `_env_file=None` as well. Both halves are needed; see the correction note
below for why `_env_file=None` alone is not enough.

### 6. Two agent backends duplicate normalization logic

`opencode_backend.py` and `claude_sdk_backend.py` each translate their runtime's
tool inputs/names into the shared `balam.agent.events` vocabulary. Some helpers
are already shared (`collapse_mcp_name`, `_normalize_input` bridges `file_path`↔
`filePath`), but display/category maps diverge. As backends are the pluggable
seam (ADR-0014), keeping the normalization contract in one place — closer to
`events.py` — would reduce drift as either runtime evolves.

Overlaps with #4; a canonical tool registry would remove much of it.

### 7. The frontend has no tests at all

There is no test runner and no test file anywhere under `apps/frontend` or
`packages/`. `typecheck` and `lint` are the only automated checks. The app is
small (~1,334 hand-written lines) and mostly presentational, so this is a
reasonable trade for now — but the diff viewer and the noVNC client have real
logic, and nothing would catch a regression in either.

**Direction:** if this stays untested, treat it as a deliberate choice and say so
here. If not, `bun test` on the two `lib/` modules with real logic is the
cheapest useful start.

---

## Tier 3 — minor / keep an eye on

- **Broad `except Exception` (48 uses, up from ~40).** 38 of the 48 are in the
  two god modules (`bot.py` 22, `streamer.py` 16). Audited a sample: most log at
  `debug` with `exc_info=True` or carry an explanatory comment (e.g. the
  native-draft fallback). Not silent swallowing today, but the count keeps
  growing, and a new bare handler could hide a real error unnoticed.
- **`# type: ignore` at seams (2).** `server.py:227` casts `None` to a `Router`
  for a test-only construction; `config.py:196` `call-arg` for env-sourced
  settings. Both are load-bearing and commented; fine, just noted.
- **Scattered timing constants** — `draft_interval`, `asyncio.sleep(0.05)`,
  `httpx.Timeout(connect=10, …)`, `wait_for_ready(timeout=30, interval=0.5)`,
  plus `_FOREIGN_RESULT_GRACE_S` and the ADR-0015 background-work cap in
  `claude_sdk_backend.py`. Not centralized; harmless but would be easier to tune
  from one place.
- **`config.example.yaml` / `.env.example` drift** — still not checked; worth a
  periodic diff against `config.py`'s validated fields.

---

## Changes since the last snapshot

Three findings from the 2026-07-13 doc did not survive re-checking. Recorded here
so the same ground is not re-covered.

- **"`balam.db` sits untracked in the repo root and is not gitignored" — wrong,
  now removed.** The database is `balam.sqlite` (`config.py` `db_path` default,
  and `BALAM_DB_PATH` in `.env.example`). `*.sqlite` has been in `.gitignore` all
  along, and no file named `balam.db` exists in the repo. `git status` is clean.
  The one real `balam.db` on this machine belongs to OpenCode and lives at
  `~/.local/share/opencode/balam.db`, outside the repo. Nothing to fix.

- **"Generated file is dated Jun 11; `server.py` has changed since" — no drift
  exists.** Running `bun run gen:api` on this tree regenerates
  `packages/shared/src/api.ts` byte-for-byte identically. Both files last changed
  on Jun 11, in the same noVNC feature. The *risk* was real — nothing enforced
  it — but the claimed drift was not. Now enforced by CI (see below).

- **"Deployed `.env` leaks into 5 tests" — right symptom, wrong cause, and the
  proposed fix would not have worked.** The failures persist in a clean worktree
  that has no `.env` at all, so the file is not the source. The values come from
  real exported environment variables. `_env_file=None` only disables the file,
  and pydantic-settings reads real environment variables at *higher* precedence,
  so that fix alone would leave all 5 tests failing. Rewritten as #5 above with
  the correct cause and fix.

### Resolved

- **No CI existed.** The previous doc proposed wiring the `gen:api` drift check
  into CI, but the repo had no `.github/workflows` directory at all, so there was
  nothing to wire it into. Added in the same change as this snapshot:
  `.github/workflows/ci.yml` runs three jobs on every push to `main` and every
  PR — backend (`ruff check`, `ruff format --check`, `pytest`), frontend
  (`typecheck`, `lint`), and the API type drift check (regenerate, fail if the
  tree changed). The drift job was verified in both directions: it passes on the
  current tree, and it fails when a route is added to `server.py` without
  regenerating.

---

## What is notably clean (not debt)

- Test coverage is broad: **22 test modules**, one per source module including
  the two backends, permissions, approvals, streamer, schedules, and router.
  586 tests pass in a clean environment.
- `ruff check`, `ruff format --check`, `bun run typecheck` and `bun run lint` are
  all green on this tree, with no suppressions beyond the two noted above.
- ADRs are thorough and referenced from code comments — the _why_ is captured.
- `api.ts` (frontend) is a hand-written typed client that imports generated types
  from `@balam/shared`; it is not a parallel type definition.
- Broad excepts largely log rather than swallow (see Tier 3).
- `opencode_tools.py` keeps the `Tool` and `Permission` enums deliberately
  separate, with a comment explaining exactly where they diverge. The duplication
  in #4 is across files, not inside this one.

---

## Recommended order of attack

Ordered by value per unit of risk. The first two are small and make the rest
safer to attempt.

1. **Make the tests hermetic (#5)** — small, self-contained, and it is the only
   item currently costing time on every run. Do this first: until it is done,
   Balam cannot run its own suite, which makes every later refactor harder to
   verify from inside the bot.
2. **Introduce the canonical tool registry (#4)** — medium, well-bounded, and it
   removes most of #6 as a side effect. Unblocks safe work on either backend.
3. **Split `bot.py` by command group (#1)** — the big one. Do it incrementally,
   one command module at a time, leaning on `test_bot.py`. Land each module as
   its own commit so a regression is easy to bisect.
4. **Extract `streamer.py`'s rendering layer (#2)** — after #3, and carefully:
   the answer-at-tail and collapsed-tool-stream invariants live here and are
   under-specified in tests.
5. **Split `claude_sdk_backend.py` (#3)** — lowest urgency of the four
   structural items, but the cheapest to do *now* rather than at 2,000 lines.
6. **Decide on frontend tests (#7)** — either write the first few or record the
   decision not to.
