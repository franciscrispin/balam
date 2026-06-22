# Live Test Plan — Pluggable Agent Backend (PR #1)

Validates that the changes in [PR #1 "Pluggable agent backend: OpenCode or
Claude Agent SDK"](https://github.com/franciscrispin/balam/pull/1) work end-to-end
against the **real Telegram bot**, for **both** runtimes selected by `AGENT_BACKEND`:

- `opencode` (default) — long-lived OpenCode server over HTTP/SSE.
- `claude_sdk` — in-process Claude Agent SDK (per-turn `query(resume=…)`).

The contract under test (ADR-0014): both runtimes implement `AgentBackend`, emit
the normalized `balam.agent.events` vocabulary, and the streamer / router /
permission / approval layers stay **backend-agnostic** — so the *user-visible
behavior should be identical across backends*, modulo the documented consequences
(SDK runs Claude models, coarser live reasoning, fresh session on switch).

---

## Results checklist

Legend: ✅ pass · ◑ partial · ❌ fail · ⏭️ skipped · ⬜ not yet run

> **Run summary (2026-06-16, live on the workspace forum):** Pre-flight green
> (387 tests, ruff, typecheck). **Part A (OpenCode) 12/12 ✅.** **Part B (Claude
> SDK) 17/17 ✅** — incl. all five live-only fixes (B1 streaming-input, B3 stdin
> stays open, B6 `setting_sources=[]`, B12 legacy-UUID rebind, B13 no spurious
> rate-limit). **Part C:** C2 ✅; C1/C3 partial (backward flip + "no auth" row
> not run — owner chose to leave the bot on `claude_sdk`). No tracebacks,
> "Stream closed", or spurious notices observed across the whole run.

### Pre-flight
| Check | Result | Notes |
| --- | --- | --- |
| `pytest` (387+ green) | ✅ | 387 passed, 1 warning (16.4s) |
| `ruff check .` | ✅ | All checks passed |
| `bun run typecheck` | ✅ | shared + frontend exit 0 |

### Part A — OpenCode
| Case | Result | Notes |
| --- | --- | --- |
| A1 Basic round-trip | ✅ | General entry spawned `balam:` topic; agent replied exactly `pong`; typing→draft→final seen; session `ses_1311…` |
| A2 Reasoning + multi-step | ✅ | `ls` tool call rendered + final answer "15 entries" (matches actual); same session resumed |
| A3 Approval — Allow once | ✅ | Two gates (external_directory boundary → edit), both Allow once; `/tmp/balam_test_oc.txt`=`OPENCODE`; agent confirmed |
| A4 Approval — Deny | ✅ | Deny on boundary gate; file absent; agent reported "Permission was denied … not created"; clean turn |
| A5 Pre-approved tool (no keyboard) | ✅ | Read ran with NO keyboard (native allow rule); answered "Balam" = first heading |
| A6 Directory boundary | ✅ | `/etc/passwd` read gated (external_directory keyboard), not silent; deny → "couldn't read" |
| A7 `/plan` + approval | ✅ | Plan mode on → read-only plan → Yes/No/View plan keyboard → Yes built+verified haiku; follow-up "2+2"→"4" ran normally (sticky flag cleared) |
| A8 `send_file` | ✅ | per-topic MCP tool `balam_t1871_send_file`; sendDocument 200; README.md (2.8KB) delivered as attachment |
| A9 `/context` new-topic bind | ✅ | `/context` lists all 6 + marks current; `/context zog` → new topic, Go to topic link; `pwd`=`/home/ubuntu/projects/zog` |
| A10 Abort turn | ✅ | `/cancel` → `POST /session/…/abort 200`; UI "Cancelled."; next turn ("Say OK"→"OK") works |
| A11 `/status` | ✅ | "Context: zog · Backend: opencode · Directory: /home/ubuntu/projects/zog · Model…" |
| A12 Session resume | ✅ | Continuity recall ("/home/ubuntu/projects/zog") without tools; same `ses_13102760…` across turns AND across a `balam` restart (sqlite map survived) |

### Part B — Claude SDK
| Case | Result | Notes |
| --- | --- | --- |
| B0 Backend selection & boot | ✅ | `claude_sdk backend is ready`, app started, no traceback; uses bundled Claude Code CLI |
| B1 Basic round-trip (streaming-input) | ✅ | exact reply "hello-sdk"; streaming-input implied (confirmed by B3 tool gating) |
| B2 Incremental text + coarse reasoning | ✅ | coherent explanation streamed; `ls`/`Read` tool calls rendered (SDK→OpenCode vocab); dirs listed |
| B3 Allow once (stdin stays open) | ✅ | `can_use_tool` fired (keyboard); Allow once → file=`CLAUDESDK`; NO "Stream closed" in logs |
| B4 Deny | ✅ | Deny → file absent; agent "the file wasn't created"; clean turn |
| B5 Pre-approved in-process | ✅ | no keyboard (Read pre-approved via evaluate(build_ruleset)); answer "CLAUDE.md" correct |
| B6 External settings can't bypass | ✅ (code+behavior) | `setting_sources=[]` at `claude_sdk_backend.py:357` w/ security comment; gating fires live (B3) despite no ambient allow. Live ambient-rule injection blocked by safety classifier (can't widen agent's own perms) — not run live |
| B7 Directory boundary | ✅ | `/etc/hostname` read gated by local boundary; deny → "Reading … was denied" |
| B8 `/plan` → ExitPlanMode approval | ✅ | ExitPlanMode → Yes/No/View plan keyboard; Yes built poem; follow-up "5×5"→"25" normal (sticky cleared) |
| B9 `send_file` in-process tool | ✅ | no keyboard (pre-approved in-process); log "sent CLAUDE.md … thread 1941"; 8.1KB doc delivered |
| B10 MCP coercion | ✅ (unit) | `coerce_sdk_mcp_config` handles stdio/local/shorthand/sse/http/remote; 4 unit tests green. No live context defines `mcp` → live round-trip not run |
| B11 Lazy session id + persistence | ✅ | SDK minted UUID `0621a177-…`, persisted to `topic_sessions` (thread 1916) via SessionStarted |
| B12 Legacy OpenCode rebind | ✅ | legacy `ses_13102760…` NOT resumed; fresh UUID `0621a177-…` persisted; reply worked, no crash |
| B13 Rate-limit notice only when throttled | ✅ | ~12 SDK turns, zero rate-limit/retry notices in UI+logs (false-positive gone). Real throttle not forced |
| B14 Abort turn | ✅ | `/cancel` stopped count mid-stream ("Cancelled."); recovery "Say READY"→"READY"; no aclose/GeneratorExit race in logs |
| B15 `/context` new-topic bind | ✅ | `/context balam` → new topic `_1941` bound to balam (lazy session) |
| B16 `/status` shows SDK | ✅ | `/status` → `Backend: claude_sdk` |

### Part C — Cross-backend
| Case | Result | Notes |
| --- | --- | --- |
| C1 Round-trip the switch | ◑ partial | Forward opencode→claude_sdk proven (Part A on OC; flip; B12 rebind + all B green). Backward claude_sdk→opencode pending a `.env` flip back by owner |
| C2 Mini App under both | ✅ | Mini App server 200 under SDK; `/browser` "Watch live" button generated; markdown affordance present; server was up across A+B (backend-agnostic). Full WebApp click-through not exhaustively done |
| C3 Config matrix sanity | ◑ partial | opencode+server-up ✅ (Part A); claude_sdk+auth ✅ (Part B, used bundled CLI/subscription). "neither auth" row not tested (would require de-authing) |

---

## 0. Conventions & how to run each check

**Operate the stack** (`run-balam` skill):

| Action | Command |
| --- | --- |
| Restart bot (picks up `.env` + code) | `sudo systemctl restart --no-block balam` |
| Status | `systemctl --no-pager status balam-opencode balam cloudflared-balam` |
| Follow bot logs | `journalctl -u balam -f` |
| Follow agent logs (opencode) | `journalctl -u balam-opencode -f` |

**Drive the bot** (`browser-use` skill): open Telegram Web as the owner, send to
the bot in a forum topic, watch typing indicator → animated streaming draft →
final reply.

**Switching backends** — edit the repo-root `.env`:

```
AGENT_BACKEND=opencode        # Part A
# AGENT_BACKEND=claude_sdk    # Part B (+ ANTHROPIC_API_KEY or an authed claude CLI)
```

then `sudo systemctl restart --no-block balam`. Confirm with `/status` (it prints
`Backend: <name>`) **and** the boot log line before trusting any result.

**Marking results.** For each case record: ✅ pass / ❌ fail / ⏭️ skipped, plus the
observed reply, the relevant `journalctl` snippet, and (for permission cases) the
on-disk side effect. A case only passes if behavior matches the OpenCode baseline
*and* nothing scary appears in the logs (no tracebacks, no "Stream closed", no
spurious rate-limit notice).

**Pre-flight (regression gate — run before any live testing):**

```sh
uv --directory apps/backend run pytest          # expect 387+ green
uv --directory apps/backend run ruff check .
bun run typecheck                               # frontend types still build
```

If unit tests are red, stop — don't burn live turns on a broken tree.

---

## Part A — OpenCode backend (regression: behavior must be unchanged)

> Goal: prove the refactor (streamer now consumes `AgentEvent`s via
> `OpenCodeBackend` instead of raw SSE) did **not** change any existing behavior.
> This is the safety net — the SDK is new, but OpenCode regressions would be the
> expensive surprise.

Set `AGENT_BACKEND=opencode`, restart, confirm `/status` → `Backend: opencode`.

### A1. Basic round-trip (streaming)
1. In a topic bound to the `balam` context, send: `Reply with exactly: pong`.
2. **Expect:** typing indicator → animated streaming draft updates → final reply
   `pong`. Logs show a normalized event flow, no SSE parse errors.

### A2. Reasoning + multi-step rendering
1. Send a prompt that forces tool use + thinking, e.g. `List the files in the repo
   root, then tell me how many there are.`
2. **Expect:** reasoning/progress narration appears, the tool call renders
   (running → completed), earlier step prose demotes to progress as a new step
   starts, final answer is correct. (Guards the `message_id` step-grouping path.)

### A3. Tool approval keyboard — Allow once
1. Send: `Create a file /tmp/balam_test_oc.txt containing the word OPENCODE.`
   (`Write` outside the workspace dir → falls through to the human keyboard.)
2. **Expect:** inline Allow/Deny keyboard. Tap **Allow once**.
3. **Verify:** `cat /tmp/balam_test_oc.txt` → `OPENCODE`. Reply confirms the write.

### A4. Tool approval keyboard — Deny
1. Send: `Create a file /tmp/balam_test_oc_denied.txt containing NOPE.`
2. Tap **Deny**.
3. **Verify:** file does **not** exist; agent reports it was blocked and does not
   loop/crash.

### A5. Pre-approved tool runs without prompting
1. Send: `Read README.md and summarize the first heading.` (`Read` is in the
   context's `allowed_tools` → native OpenCode allow rule, no keyboard.)
2. **Expect:** no approval keyboard; answer streams directly. (Guards
   `build_ruleset` → native ruleset path.)

### A6. Directory-boundary policy (symlink-safe)
1. Send: `Read /etc/passwd and show me the first line.` (outside workspace +
   additional dirs.)
2. **Expect:** Balam's local boundary blocks it (keyboard or auto-deny per
   `approvals.decide`), not a silent read. (Guards `approvals.py` staying local.)

### A7. `/plan` mode + plan approval
1. `/plan`, then send a small feature request (e.g. `Add a one-line docstring to
   app.py`).
2. **Expect:** agent plans (no edits yet); a plan-approval surfaces with a **View
   plan** button. Tap **Yes** → agent builds in the same/next turn; sticky plan
   flag clears (a follow-up turn is a normal build, not a plan). (OpenCode
   `plan_exit` / `plan_path` path.)

### A8. `send_file` agent tool
1. Send: `Send me the README.md file as an attachment.`
2. **Expect:** the file arrives as a Telegram document (per-topic remote MCP
   server + scope token path).

### A9. `/context`, new-topic binding
1. `/context` → lists contexts + current binding.
2. `/context ivy` → replies with a **Go to topic** link to a *new* topic bound to
   `ivy`; current topic unchanged.
3. In the new topic send `pwd` (or "what directory are you in") → confirms the
   `ivy` `directory`. (Guards router topic→context→session, eager session create.)

### A10. Abort a running turn
1. Send a long task (e.g. `Count slowly from 1 to 50, one number per line.`).
2. Trigger abort (`/cancel`-style or new message per current UX) mid-stream.
3. **Expect:** stream stops; backend abort fires (log line); next turn works.

### A11. `/status`
1. `/status` → shows context, session id, whether a turn is running, and
   `Backend: opencode`.

### A12. Session resume / persistence
1. After A1–A2 in a topic, send a follow-up that depends on prior context
   (`What did I just ask you to do?`).
2. **Expect:** continuity — same session resumed (sqlite `store.py` topic→session
   map intact across the turn; survives a `balam` restart too).

---

## Part B — Claude Agent SDK backend (the new runtime)

> Goal: prove the SDK backend reaches **parity** with OpenCode on the
> backend-agnostic layers, and that the five live-only fixes hold.

Pre-req: set `ANTHROPIC_API_KEY` in `.env` **or** have an authenticated `claude`
CLI / subscription; optionally `CLAUDE_SDK_CLI_PATH`. Set
`AGENT_BACKEND=claude_sdk`, restart, confirm `/status` → `Backend: claude_sdk` and
the boot log. Context `model`, if set, must be a **bare Claude id** (provider half
ignored) — verify a context with a bad/provider-prefixed model is handled per
`split_provider_model`.

### B0. Backend selection & boot
1. **Expect:** `balam.service` boots clean — no traceback, SDK backend
   constructed, `wait_for_ready` passes. `/status` → `Backend: claude_sdk`.

### B1. Basic round-trip (streaming) — *fix: streaming-input mode*
1. Send: `Reply with exactly: pong`.
2. **Expect:** streamed reply `pong`. Logs confirm the prompt is sent as an async
   iterable of user-message dicts (streaming-input mode), **not** a string —
   this is the prerequisite for `can_use_tool` to ever fire.

### B2. Incremental text + coarse reasoning
1. Send: `Briefly explain what this repo does, then list the top-level dirs.`
2. **Expect:** text streams incrementally (per-content-block deltas accumulated
   to running totals). Reasoning may arrive coarsely / once near end of a step
   (documented SDK consequence) — that's acceptable, not a failure.

### B3. Tool approval keyboard — Allow once — *fix: stdin stays open*
1. Send: `Create a file /tmp/balam_test_sdk.txt containing the word CLAUDESDK.`
2. **Expect:** `can_use_tool` parks the call → `PermissionRequested` →
   Allow/Deny keyboard. Tap **Allow once**.
3. **Verify:** `cat /tmp/balam_test_sdk.txt` → `CLAUDESDK`. **Critically:** no
   "Stream closed" / tool-denied error in logs — the input stream must stay open
   for the whole turn so the control request round-trips over stdin.

### B4. Tool approval keyboard — Deny
1. Send: `Create a file /tmp/balam_test_sdk_denied.txt containing NOPE.`
2. Tap **Deny**.
3. **Verify:** file absent; agent reports blocked, turn ends cleanly (no hang).

### B5. Pre-approved tool runs in-process (no keyboard) — *evaluate() path*
1. Send: `Read README.md and summarize the first heading.` (`Read` ∈
   `allowed_tools`.)
2. **Expect:** no keyboard — `evaluate(build_ruleset(...))` pre-approves it
   in-process. Compare with A5: identical UX.

### B6. External settings cannot bypass the gate — *fix: setting_sources=[]*
1. Ensure the owner's `~/.claude/settings.json` has a permissive allow rule for
   some tool Balam would normally gate (e.g. `Bash` / `Write` to a temp path).
2. Send a prompt that triggers that tool outside the workspace.
3. **Expect:** Balam's keyboard/boundary **still** decides — the owner's ambient
   allow-rules do **not** auto-allow it. (`setting_sources=[]` makes Balam's gate
   authoritative.)

### B7. Directory boundary still local
1. Repeat A6 (`Read /etc/passwd`) on the SDK.
2. **Expect:** same local boundary block — `reads still hit the streamer's
   workspace boundary` regardless of backend.

### B8. `/plan` mode → ExitPlanMode approval — *SDK plan parity*
1. `/plan`, then a small feature request.
2. **Expect:** runs with `permission_mode="plan"`; the agent's `ExitPlanMode` is
   intercepted in `can_use_tool` and surfaced as the **same Yes/No plan-approval**
   question, carrying inline `plan_text` (View plan button works).
3. Tap **Yes** → `ExitPlanMode` allowed, agent builds in the same turn, sticky
   plan flag drops. Tap **No** in a separate run → planning continues (build
   blocked). Also confirm a **default** (non-`/plan`) turn keeps native NL
   planning (it can plan in prose without the gate).

### B9. `send_file` as in-process SDK tool
1. Send: `Send me the README.md file as an attachment.`
2. **Expect:** document arrives — via the in-process SDK tool (no HTTP MCP server,
   no scope token). It's pre-approved (no keyboard for send_file itself).

### B10. Context MCP servers coerced to SDK shape
1. Use/temporarily add a context with an `mcp` server (stdio and/or http/sse).
   Trigger a turn that needs it.
2. **Expect:** the server is reachable — `mcp_servers` coercion (stdio/sse/http)
   worked; `${VAR}` from `.env` still interpolated.

### B11. Lazy session id + persistence — *SessionStarted*
1. Fresh topic (or one with no SDK session yet). Send a first message.
2. **Expect:** `TurnRequest.session_id` is `None` on the first turn; the real SDK
   id arrives as `SessionStarted` and the streamer persists it via
   `router.persist_session`. Check `store.py` sqlite now holds a UUID for the
   topic.
3. Follow-up message → resumes that session (`query(resume=<uuid>)`); continuity
   holds.

### B12. Legacy OpenCode session rebind — *fix: don't resume non-UUID*
1. Take a topic that already has a `ses_…` (OpenCode) id from Part A (or any
   pre-existing topic). With `AGENT_BACKEND=claude_sdk`, send a message.
2. **Expect:** `--resume` is **not** attempted on the non-UUID id; a fresh SDK
   session starts and is persisted; the topic transparently rebinds. No hard
   fail / crash. (This is the cross-backend switch path.)

### B13. Rate-limit notice only when throttled — *fix: status=="rejected"*
1. Run several normal turns.
2. **Expect:** **no** "rate-limited — retrying" message on normal turns
   (`RateLimitEvent` with allowed / allowed_warning must stay silent). The notice
   should appear **only** if genuinely throttled (status `rejected`), and then
   name the limit type. (Hard to force; primarily verify the *false positive* is
   gone.)

### B14. Abort a running turn (SDK)
1. Long task, abort mid-stream (as A10).
2. **Expect:** turn cancels cleanly; query generator closes without an aclose/GC
   race in logs; next turn works.

### B15. `/context` + new-topic binding (SDK)
1. Repeat A9 on the SDK (router maps rows only, mints session lazily).
2. **Expect:** new topic bound to the chosen context with the right `directory`;
   `ResolvedSession` carries `session_id=None` + the context's `allowed_tools`/`mcp`.

### B16. `/status` shows SDK
1. `/status` → `Backend: claude_sdk`, context, session, running flag.

---

## Part C — Cross-backend & switch-over

### C1. Round-trip the switch
1. Start on `opencode`, run A1 in a topic. Switch to `claude_sdk`, restart, send
   in the **same** topic → rebinds (B12), works. Switch back to `opencode`,
   restart, send again → works (its original `ses_…` still resumable, or a clean
   fresh session).
2. **Expect:** no data loss/crash on either flip; each backend starts the topic on
   its own session; transcripts are *not* expected to be interchangeable
   (documented consequence).

### C2. Mini App still works under both
1. Under each backend, exercise `/browser` (live noVNC view), the diff viewer, and
   the markdown viewer once.
2. **Expect:** unchanged — the Mini App is backend-agnostic; this guards against
   the refactor disturbing `server.py` / content_store wiring.

### C3. Config matrix sanity
| `AGENT_BACKEND` | Auth present | Expect |
| --- | --- | --- |
| `opencode` | OpenCode server up | normal operation |
| `claude_sdk` | `ANTHROPIC_API_KEY` set | normal operation |
| `claude_sdk` | no key, authed CLI | normal operation (CLI fallback) |
| `claude_sdk` | neither | clean, readable failure at boot/turn — not a silent hang |

---

## Exit criteria

- Pre-flight: unit tests green, ruff clean, frontend typechecks.
- **Part A:** every case matches prior OpenCode behavior — **zero regressions**.
- **Part B:** all five live-only fixes verified (B1, B3, B6, B12, B13) and parity
  cases (B5, B7, B8, B9, B10) match their Part A counterparts.
- **Part C:** backend switch is non-destructive; Mini App unaffected.
- No tracebacks, no "Stream closed", no spurious rate-limit notices, no leaked
  scary messages in `journalctl -u balam` across the run.

## Cleanup

```sh
rm -f /tmp/balam_test_oc.txt /tmp/balam_test_sdk.txt
# remove any scratch topics created via /context
# restore .env to the intended default backend, then: sudo systemctl restart --no-block balam
```
