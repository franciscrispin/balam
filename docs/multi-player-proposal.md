# Balam multi-player — proposal

**Date:** 2026-08-10 · **Status:** proposal for discussion · **Scope:** code + deploy changes to support two users on one Balam instance

**Target use cases**

1. **Priority:** Balam deployed on a *new* VM with its *own* Telegram bot, shared by **2 users, each in their own forum supergroup**.
2. **Non-priority:** same deployment, but the 2 users share **one** supergroup.

This proposal is grounded in a full audit of the backend (three parallel code sweeps: Telegram auth/routing, agent-backend identity, shared resources). ADR-0007 and ADR-0008 both say "revisit together if this ever goes multi-user" — this is that revisit.

---

## TL;DR

- **The data layer is already multi-player-clean.** Every persistent table and every in-process registry is keyed by `(chat_id, thread_id)` — two supergroups with colliding thread ids just work. Model/effort overrides and schedules are already per-chat. The single-user coupling lives almost entirely in **auth config, identity, and ownership**, not in the data model.
- **Recommended architecture: users become first-class, and for the priority case the tenant boundary is the chat.** A `users:` section in `config.yaml` maps each supergroup to one user profile; everything already keyed by `chat_id` inherits per-user scoping for free.
- **Identity is currently ambient** — nothing in the code configures it; it is the systemd `User=ubuntu` plus that account's home (`~/.claude` login, `~/.config/gh`, `~/.gitconfig`, `~/.ssh`, OpenCode's auth store). The fix: **one Unix account per user, with shared project files** (shared group + ACLs). Agent turns run *as* the owning account, so every tool authenticates as the right person — Claude login (and its claude.ai connectors), `gh`, git/ssh, and even flag/config-file CLIs like `lark-cli` — while both users keep working on the same workspaces. A lighter same-account env-injection variant remains as the zero-ops fallback.
- **Backend optionality**: make `claude-agent-sdk` an optional dependency (extra), import backends lazily behind the `AGENT_BACKEND` branch, and make `deploy/install.sh` backend-aware. Today an OpenCode-only VM still downloads the SDK's ~251 MB bundled CLI, and `app.py` imports both backends unconditionally.
- **One bot, two humans**: every Telegram update — message, callback tap, Mini App `initData` — carries the sender's user id, and the harness resolves the profile from exactly that. The nuance: the per-message id decides *authorization and attribution*, but a topic's *turns* always run as one identity (the topic owner's), because a session's transcript and credentials belong to one account. **Two bots on one VM** is also workable (two processes, two ports/tunnels) but only covers the two-supergroup case — see Alternatives.
- **Honest residual limit**: separate accounts give real credential isolation (user A's agent cannot read user B's home), but inside a *shared* workspace the two agents can still edit each other's files — that is the point of sharing, and the residual trust the two users accept.

---

## 1. What the audit found

### 1.1 Already clean (keep as is)

| Area | Evidence |
| --- | --- |
| Topic→session store | `topic_sessions`, `topic_auto_names`, `topic_overrides` all have `(chat_id, thread_id)` composite PKs (`store.py:59-99`); every query passes `chat_id` |
| Schedules | `schedules.chat_id NOT NULL` (`store.py:103-119`); boot registration walks **all** chats (`store.py:419-426`); fires post into their own chat |
| Turn concurrency | `TurnRegistry` locks/queues per `(chat_id, thread_key)` (`turns.py:94-155`) — different topics and different chats already run concurrently |
| Model/effort | Overrides stored on each chat's General row (`router.py:30-34, 149-173`) — per-chat, so per-user under chat-as-tenant |
| send_file | Bound per topic in both backends (`agent_tools.py:190-235`, `app.py:122-135`) |
| PTB filters | `filters.User` / `filters.Chat` accept id lists — the mechanism is multi-ready; only the config type is not |

### 1.2 The four real gaps

**Gap 1 — auth is two scalars.** `allowed_telegram_user_id: int` and `allowed_telegram_chat_id: int | None` (`config.py:50,54`), read by the message filters (`bot.py:320-322`), `auth.py`, four *hand-inlined* copies of the callback check in `callbacks.py` (62-68, 103-108, 153-158, 197-203), the Mini App auth (`webapp_auth.py:134-136`), and the VNC WebSocket (`vnc.py:83`). A single chat id cannot express two supergroups; a single user id cannot express two humans.

**Gap 2 — identity is ambient.** No code path configures who the agent *is*. The `claude` subprocess inherits the balam process environment wholesale (`claude_agent_sdk` transport builds env as `os.environ` + two keys, `claude_sdk_backend.py:277-289`); `CLAUDE_CONFIG_DIR` is never set anywhere in the repo; git/gh/ssh come from the one home directory. The claude.ai account-managed connectors (Calendar/Gmail/Drive/Notion) are bound to the single logged-in Claude account and ride into every turn via `strict_mcp_config=False` (`claude_sdk_backend.py:333-340`). OpenCode has one server, one auth store per VM user (`deploy/balam-opencode.service`).

**Gap 3 — nothing has an owner.** Approval/question/picker keyboards are tappable by *any* allowed user (`approvals.py:218-224, 292-319, 510-520`); "Approve all edits" escalates by session id, not user; schedules belong to a chat, not a person; markdown snapshots live in one flat namespace any authenticated user can fetch (`content_store.py`, `server.py:132-140`); `/api/diff?context=<name>` serves **any** context to any authenticated user (`server.py:114-130`); the agent is never told who is speaking (no handler reads `message.from_user` for the prompt).

**Gap 4 — both backends are assumed installed.** `app.py:32-33` imports `ClaudeSdkBackend` and `OpenCodeBackend` at module top; `claude-agent-sdk>=0.2.102` is a hard dependency (`pyproject.toml:22`) shipping a ~251 MB bundled CLI; `deploy/install.sh` and the systemd units assume the OpenCode binary and hardcode `/home/ubuntu/...` paths. A VM owner who wants only one backend can't cleanly have that.

---

## 2. Recommended design: first-class users, tenant = chat

### 2.1 User profiles in `config.yaml`

```yaml
default_context: balam          # legacy key, still honored

users:
  francis:
    telegram_user_id: 111111111
    chat_id: -1001111111111     # this user's workspace supergroup
    timezone: Asia/Singapore
    default_context: balam
    identity:
      account: francis          # Unix account agent turns run as (§2.3)
      # state_dir: ...          # alternative: same-account env-bundle fallback
  bob:
    telegram_user_id: 222222222
    chat_id: -1002222222222
    timezone: Asia/Jakarta
    default_context: bob-project
    identity:
      account: bob

contexts:
  balam:
    directory: /home/ubuntu/projects/balam
    users: [francis]            # NEW, optional: who may bind this context (default: all)
  bob-project:
    directory: /home/ubuntu/bob/project
    users: [bob]
```

- **Backward compatible:** if `users:` is absent, synthesize one profile from `ALLOWED_TELEGRAM_USER_ID` / `ALLOWED_TELEGRAM_CHAT_ID`. The current deployment changes nothing.
- **Tenant = chat (priority case):** each supergroup maps to exactly one profile. Routing derives the user from `chat_id`, so schedules, model/effort overrides, topics, and turn queues — all already chat-keyed — become per-user with no schema change.
- `timezone` and `default_context` move from process-global to per-user (both are per-human concepts; `BALAM_TIMEZONE` stays as the fallback).

### 2.2 Auth changes

- `bot.py`: build the filters from lists (`filters.User(user_id=[...]) & filters.Chat(chat_id=[...])`), **plus an explicit pair check** — the AND of two lists would admit user A posting in user B's supergroup. A tiny custom filter (or first line of each handler) resolves `(from_user.id, chat.id)` to a profile and drops mismatches.
- `auth.py`: `is_owner(...)` → `resolve_user(from_id, chat_id) -> UserProfile | None`. **Prep refactor first:** replace the four hand-inlined checks in `callbacks.py` with calls to the one shared function, so the auth shape changes in one place, not five.
- `webapp_auth.py`: `RequireOwner` → `RequireUser`, validating the HMAC as today but resolving the embedded user id against the profile set and **returning the profile** (today it returns the single allowed id and every route discards it, `webapp_auth.py:136`).
- `bot.py:252-266` `register_commands`: register the per-chat command scope for every profile's chat, not one.

**What the sender's Telegram user id can and cannot decide.** Every update type carries it — `message.from_user.id`, `callback_query.from_user.id`, and the `user.id` inside Mini App `initData` — so one bot can always tell the two humans apart at the harness level; profile resolution keys on it, with the chat pair check as defense in depth. The per-message id settles **authorization** (may this person tap this keyboard, cancel this turn, delete this topic) and **attribution** (who said this). What it cannot do is give one *topic* two identities: a session is one continuous transcript living under one account's `~/.claude/projects/`, resumed by one login — so **identity attaches to the topic** (owner = creator; under chat-as-tenant, the chat's user), and a turn always runs as the topic owner regardless of who typed the message. One Telegram caveat: a user posting as an **anonymous admin** (or as a channel) has no visible user id — `from_user` becomes the group's anonymous bot — so such updates are dropped, and both users must post as themselves.

### 2.3 Identity — one Unix account per user, shared project files

Each human gets a real account on the VM (`user1@server`, `user2@server`). Identity stops being something Balam has to inject and becomes what it already is on any shared Linux box: **the account the agent process runs as**. Each home carries that user's whole identity surface — `~/.claude` (Claude login, claude.ai connectors, artifacts, memory, skills settings, session transcripts), `~/.config/gh`, `~/.gitconfig` + `~/.ssh`, and every config-file/flag-based CLI (`lark-cli` profiles) that an env-var scheme could never separate. No per-user API keys need to live in `.env` at all.

**Shared files.** Both accounts (plus the balam service account) join a shared group, and shared workspaces get setgid directories plus **default ACLs** (`setfacl -d -m g:balam:rwX`) so files created by either user stay group-read/writable regardless of umask. Two git-specific gotchas to handle in setup: `core.sharedRepository=group` on shared repos, and `safe.directory` entries per account — git refuses to operate on a repo owned by a different user without them (this includes the balam service account running `git_diff.py`).

**Running turns as the right account.**

- **SDK mode:** the profile's `cli_path` (already a knob, today process-global at `app.py:80-83` — becomes per-profile) points at a tiny wrapper: `exec sudo -u user1 -H <claude> "$@"`, with a sudoers rule scoped to exactly that command (`balam ALL=(user1) NOPASSWD: <claude>`) and a minimal `env_keep`. stdin/stdout pipe straight through sudo, and sudo relays SIGTERM to its child — so the ADR-0015 lifecycle (holding a turn open, stdin-close wind-down, `/cancel`) is unchanged. Avoid SIGKILL-the-wrapper paths (SIGKILL cannot be relayed and would orphan the CLI).
- **OpenCode mode:** per-user `opencode serve` units with `User=user1` / `User=user2`, own port and own data dir (the existing unit already overrides `OPENCODE_DB`); the profile carries `opencode_base_url` and the router picks the client per user. No sudo anywhere — in this model OpenCode mode is as clean as SDK mode.
- **The Balam service** runs as a small service account in the shared group. The bot, `git_diff.py`, attachments, and `send_file` only need group access to the workspaces.

**What this buys, and the residual limit.** Real credential isolation: user1's agent cannot read user2's home (mode `700` homes), so "different authentication for tools" is enforced by the kernel, not by convention. What it deliberately does *not* prevent: inside a shared workspace, either agent can edit (or sabotage) the other's files — that is what sharing files means, and it is the residual trust the two users accept. Per-user connectors also fall out for free: each account's claude.ai login brings that user's own Google Calendar/Gmail; `config.yaml` MCP servers keep working, with per-user secrets under distinct `.env` names (`${FRANCIS_...}`, `${BOB_...}`).

**Claude account: per-user or shared — a setup-time choice (decided: support both).** Balam supports both modes by construction, because Claude auth is ambient per home: the deployment picks one approach at setup, puts the matching credential into each home, and sticks with it. The code never branches on the mode.

- *Per-user accounts:* each home logs into its own claude.ai account (or carries its own `ANTHROPIC_API_KEY`). Each user gets their own account-managed connectors (Google Calendar/Gmail), own usage limits, own artifacts. This is the mode that makes account-bound MCPs "just work" per user — connectors are tied to the logged-in account by construction, and one account carries exactly one set of Google links.
- *Shared account:* both homes log into the same claude.ai account (or share one API key). Consequences the setup accepts knowingly, recorded in the runbook: the account's connectors are injected into **both** users' turns — right when the connected Google account is genuinely shared, and fenced per user otherwise (next point); usage limits are one pool (a heavy turn from one user eats the other's window); `/artifacts` lists the one account for both.
- *Connector fencing (the one new code piece, small):* contexts — or profiles — gain an optional `denied_tools` list, translated into deny rules evaluated after allows (`evaluate()` is last-match-wins on both backends, so the expressiveness already exists; only the config surface is new). A shared-account deployment uses it to cut `mcp__claude_ai_*` out of a user's contexts entirely, instead of leaving those tools reachable through the ask keyboard.
- *Self-managed alternative:* connectors can also be replaced by `config.yaml` MCP servers with per-user credentials (own Google OAuth app, refresh tokens in `.env`) — works in either mode, but is strictly more plumbing than a second account, and is the exact setup this deployment once ran and deleted.

**Migration note (for the existing deployment):** session transcripts live under each account's `~/.claude/projects/`, so moving yourself from `ubuntu` to a personal account means live topics won't `resume` across the cutover unless the directory (login + `projects/`) is copied over.

**Fallback variant — same account, env bundles.** Where creating accounts is unwanted, a profile can instead carry `state_dir` + an env map (`CLAUDE_CONFIG_DIR`, `GH_CONFIG_DIR`, `GIT_CONFIG_GLOBAL`, …) injected through the SDK backend's per-turn `env` seam (`claude_sdk_backend.py:277-289` — the one identity seam that exists today). That yields the *correct* identity but not a *protected* one (same account, mutual read access), and flag-based CLIs stay shared. Keep it as the zero-ops option; the profile schema covers both (`identity.account` vs `identity.state_dir`).

### 2.4 Ownership and scoping

- `TurnRequest` gains `user` (profile name). Approvals, questions, and pickers record the initiating user at `register(...)` time — for attribution and audit, not to restrict tapping. **Policy decision (agreed): any authorized user of the chat may tap keyboards, approve, and confirm `/delete`.** That is what the current check already does once the allowlist becomes a set, so it costs nothing; the recorded initiator keeps a per-action owner-only exception possible later. The deliberate consequence to keep in view: an approval authorizes an action that *executes as the topic owner's account*.
- `schedules` gains a `user_id` column (migration; existing rows backfill to the legacy profile). A fired schedule resolves the owning profile for identity env and timezone.
- `ContentStore` entries gain an owner; `GET /api/content/{id}` checks it. `/api/diff` checks the requested context is in the caller's allowed set. Mini App views become per-user views of per-user data behind the one tunnel hostname — no frontend changes needed beyond what the API returns.
- Contexts gain the optional `users:` allowlist (§2.1); `/context` lists only the caller's contexts.
- `agent_tools.py:224-235`: make `qualify_chat` unconditional so per-topic MCP server names always carry the chat id (today the chat qualifier is *dropped* exactly when chat-scoped — inverted for multi-chat).

---

## 3. Backend optionality (install either, not both)

- **Packaging:** move `claude-agent-sdk` to `[project.optional-dependencies]` as the `claude` extra. `uv sync` installs core (httpx covers OpenCode); `uv sync --extra claude` adds the SDK. CI syncs `--all-extras`.
- **Lazy imports:** move the backend imports inside the `AGENT_BACKEND` branch in `app.py:78-102` (the SDK modules `claude_sdk_backend.py` / `sdk_tasks.py` are the only importers of `claude_agent_sdk`). Boot failure becomes a config error with a fix in the message — "`AGENT_BACKEND=claude_sdk` but claude-agent-sdk is not installed; run `uv sync --extra claude`" — instead of an `ImportError` before config is even read.
- **SDK-mode preflight:** `wait_for_ready()` is a no-op today (`claude_sdk_backend.py:191-193`); nothing validates credentials until the first turn dies. Add a boot check per user profile — under the accounts model, a cheap probe through the profile's wrapper (the balam service cannot read user homes, by design).
- **Deploy:** `install.sh` takes the backend choice; the `balam-opencode.service` unit installs only for OpenCode mode (ADR-0014 already notes it isn't needed in SDK mode). Parameterize the hardcoded `/home/ubuntu/...` paths (install user, repo path, PATH line) — the priority use case is literally "someone else sets this up on a new VM". Fix stale references while in there: the `BALAM_DEV_AUTH` comment (no such flag exists) and `RICH_MESSAGES=true` in `deploy/balam.env` (removed in `ea5aa55`).

---

## 4. Work plan

**Phase 0 — prep, no behavior change (small)**
1. Consolidate the 4 inline callback auth checks into `auth.callback_authorized`.
2. `RequireOwner` returns the authenticated user id (routes keep discarding it for now).
3. Unconditional `qualify_chat` in MCP server naming.
4. **send_file path boundary** (see §6 — worth doing even single-user).
5. Lazy backend imports + `claude` extra + friendly boot errors.
6. `install.sh` parameterization + backend choice + stale-flag cleanup.

**Phase 1 — the priority case (medium)**
1. `users:` in `config.yaml` + legacy synthesis from the two env vars.
2. List-based filters + the (user, chat) pair check; `resolve_user` in `auth.py`; per-chat command registration.
3. Identity: `TurnRequest.user`; per-profile `cli_path` (sudo wrapper) for SDK mode or per-user `opencode_base_url`; per-user timezone + `default_context`. (Env-bundle fallback: state-dir expansion into `_build_options`.)
4. Ownership columns/fields: schedules `user_id`, pending-registry initiator, `ContentStore` owner, `/api/diff` context check, contexts `users:` allowlist.
5. Deploy + runbook for the second user: create the account, join the shared group, set up ACLs/setgid on shared workspaces, `safe.directory`/`core.sharedRepository`, the sudoers rule; **choose the Claude auth mode** (per-user accounts or one shared account, §2.3) and log each home in accordingly; then `gh auth login`, gitconfig/ssh — plus create their supergroup and add the bot as admin.
6. `denied_tools` in contexts/profiles (connector fencing for shared-account mode, §2.3).

**Phase 2 — same-supergroup case (deferred, medium-large)** — see §5.

**Deferred** — per-user browser displays; per-user OpenCode servers only if OpenCode mode must serve both users.

---

## 5. The same-supergroup case (non-priority, design sketch)

Chat-as-tenant stops working; ownership must move down one level, to the **topic**:

- `topic_sessions` gains `owner_user_id` — the topic's creator. Turns in a topic always run under the **owner's** identity, regardless of who typed.
- The other user's messages in someone else's topic: allow (it's collaboration), but prefix sender attribution into the agent-visible text ("**Bob:** …") — today the agent has no idea who is speaking, and two humans' words are indistinguishable in one session.
- Shared controls (agreed policy): approval/question keyboards, `/cancel`, `/delete`, mid-turn follow-up folding (`turns.py:237-249`), and armed custom-question answers (`approvals.py:447-471`) stay open to **any authorized user in the chat** — which is what the mechanism already does once the allowlist is a set. The actor is recorded and attributed; the constraint that holds it together is that execution identity stays the topic owner's (§2.2), so approving/answering means authorizing work that runs as the owner.
- `/model` and `/effort` are chat-global by design (ADR-0015) — under a shared chat they become a shared knob. Either accept and document, or move overrides from the General row to per-user rows.

Everything in Phase 1's ownership work (initiator on keyboards, user on schedules) is deliberately shaped so this phase is additive, not a redesign. Still, it touches every keyboard and the attribution model — that is why it is Phase 2.

---

## 6. Findings worth fixing regardless of multi-player

- **`send_file` has no path boundary** (`agent_tools.py:118-120`): it delivers any absolute path the process can read — from any context, including `.env`. Bound it to the turn's context `directory` + `additional_directories`.
- **Browser/VNC is one shared screen**: `start.sh` kills any existing display stack on re-run, so two concurrent browser sessions clobber each other; `/browser` is explicitly global (`commands/views.py:98-105`), so each user would watch the other's Chrome. V1: keep it, gate `/browser` per profile flag, and say so in docs. Proper fix (deferred): per-user `DISPLAY`/profile dirs.
- **Unbounded in-memory registries**: `ToolScopes` grows per topic and is never pruned; worth a cap/TTL sweep alongside the ContentStore owner change.
- **Capacity note:** each held SDK turn is its own `claude` process (~200–500 MB, held up to 30 min under ADR-0015). Two users double the worst case — size the new VM accordingly.
- **Telegram flood budget:** one `AIORateLimiter` paces everything; two *separate* supergroups get separate per-group budgets (fine), but two users in one group contend on ~20 msg/min (another reason the shared-chat case is Phase 2).

---

## 7. Alternatives considered

**A. Two instances, two bots on the same VM (acceptable, partial).** Each user gets their own bot token, clone, `.env`, `config.yaml`, sqlite, port, and tunnel hostname; with the §2.3 accounts, each instance simply runs as its user and *all* the in-process multi-tenancy work disappears — code changes shrink to Phase 0 (mainly install parameterization). Two pollers on one VM coexist fine (different tokens, different ports). The limits: it only covers the two-supergroup shape (a second bot cannot help two users sharing one bot), and the stated one-bot use case still stands — one bot token can only be long-polled by one process, so serving both users from one bot requires the in-process design in §2.1–2.4. The two are not exclusive: the profile work is what makes either topology clean.

**B. One VM per user.** Today's answer; out of scope by the use case definition.

---

## 8. ADR impact

| ADR | Change |
| --- | --- |
| 0007 / 0008 | Amend together (they self-mandate this): trust boundary becomes the profile set — (user id, chat id) pairs; "local single-user" → "local, N mutually trusting users" |
| 0012 | Contexts gain optional `users:`; `default_context` and timezone become per-user with global fallback |
| 0013 | Mini App auth resolves a *user*, and routes scope data per user |
| 0014 | New: turns run as the profile's Unix account (per-profile `cli_path` wrapper / per-user OpenCode server); backend deps optional |
| 0015 | Unchanged, but capacity note: held turns × users |
| 0016 | Schedules owned by a user; fire with the owner's identity and timezone |
| New 0017 | Users are first-class; the tenant boundary is the chat (priority case) with topic ownership as the extension point (same-chat case) |
