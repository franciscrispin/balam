# Feature Comparison: Telegram Coding-Agent Bots

A side-by-side catalog of features across three Telegram-driven coding-agent
bots, marking which features overlap and which are distinct to each project:

- **zog** (`/home/ubuntu/projects/zog`)
- **open-shrimp** (`/home/ubuntu/projects/open-shrimp`)
- **opencode-telegram-bot** (`/home/ubuntu/references/opencode-telegram-bot`),
  abbreviated **OC-TG-Bot** in tables below.

## At a glance

|                  | **Zog**                                   | **Open-Shrimp**                                       | **OC-TG-Bot**                                          |
| ---------------- | ----------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------ |
| One-liner        | Personal Telegram bot powered by the **Claude Agent SDK** | Self-hosted Telegram bot powered by an **OpenCode** coding agent | Telegram client for a **local OpenCode server** — run/monitor coding tasks with no open ports |
| Agent backend    | Claude Agent SDK (in-process)             | OpenCode server over HTTP/SSE                          | OpenCode server over HTTP (local, health-monitored)    |
| Language         | Python 3.13+                              | Python 3.11+                                           | TypeScript / Node 20+                                  |
| Package manager  | uv                                        | uv                                                     | npm (published: `@grinev/opencode-telegram-bot`)       |
| Telegram client  | python-telegram-bot (long polling)        | python-telegram-bot (long polling)                    | grammY (long polling)                                  |
| Persistence      | SQLite (aiosqlite)                         | SQLite (aiosqlite)                                     | SQLite (better-sqlite3) + `settings.json`              |
| Frontend         | React/TS Mini App (single app, 5 views)   | React/TS Mini Apps (5 separate apps)                  | None — native Telegram UI (inline + reply keyboards)   |
| Scope            | Single-user, home-server self-host        | Single-user, self-host (Linux + macOS)                | Single-user, single private chat                       |
| Distribution     | uv run / systemd user service             | Pre-built binary, systemd, launchd, macOS menu-bar app | `npm install -g` / npx, setup wizard, systemd guide    |

All three are, fundamentally, the same idea — *a coding agent driven from
Telegram* — which is why a large core overlaps. They diverge most in the agent
backend (Claude SDK vs. OpenCode), in surface (zog and open-shrimp invest in
Mini Apps; OC-TG-Bot deliberately stays native-Telegram-only), and in how far
open-shrimp pushes into sandboxing, computer-use, and packaging.

---

## Feature matrix

Legend: ✅ yes · ➖ partial / different shape · ❌ no.

### Core agent & messaging

| Feature                                   | Zog | Open-Shrimp | OC-TG-Bot |
| ----------------------------------------- | --- | ----------- | --------- |
| Telegram-driven coding agent              | ✅  | ✅          | ✅        |
| Multi-turn sessions persisted in SQLite   | ✅  | ✅          | ✅        |
| Session resume / browse                   | ✅ `/resume` | ✅ `/resume` | ✅ `/sessions` |
| New / clear session                       | ✅ `/clear` | ✅ `/clear` | ✅ `/new` |
| Revert / fork from message history        | ❌  | ❌          | ✅ `/messages` |
| Streaming via `sendMessageDraft`          | ✅  | ✅          | ➖ opt-in `draft` mode (default: throttled message edits) |
| Message auto-splitting (4096-char limit)  | ✅  | ✅          | ✅        |
| GFM → Telegram MarkdownV2                 | ✅ mistune | ✅ mistune | ✅ remark/unified, plain-text fallback |
| File / photo attachments (inbound)        | ✅  | ✅          | ✅ (albums batched into one prompt) |
| Forum topic support (topic = session)     | ✅  | ✅          | ❌ explicit non-goal |
| Group-chat etiquette (@mention / reply)   | ✅  | ✅          | ❌ private chat only |
| Tool-output truncation                    | ✅  | ✅          | ✅ + optional hiding of tool/thinking messages |

### Contexts & configuration

| Feature                       | Zog | Open-Shrimp | OC-TG-Bot |
| ----------------------------- | --- | ----------- | --------- |
| Multi-project contexts        | ✅  | ✅          | ✅ `/projects` (OpenCode projects; `/open` adds one by browsing dirs) |
| Default context               | ✅  | ✅          | ✅ persisted in settings |
| Additional directories        | ✅  | ✅          | ➖ allowlisted browser roots for `/open` & `/ls` only |
| Per-context model override    | ✅  | ✅          | ➖ model persisted per session, not per project |
| Config format                 | YAML | YAML       | `.env` env vars |
| Telegram user allowlist       | ✅  | ✅          | ✅ single required user ID |

### Model & agent control

| Feature                          | Zog | Open-Shrimp | OC-TG-Bot |
| -------------------------------- | --- | ----------- | --------- |
| Model command / picker           | ✅ `/model` | ✅ `/model` (+ `reset`) | ✅ reply-keyboard picker (OpenCode favorites + recents) |
| Reasoning-effort control         | ❌  | ✅ `/effort` | ➖ model "variant" selection |
| Agent-mode switching (plan/build/…) | ❌ | ❌         | ✅ reply keyboard |

### Tool approval

| Feature                                | Zog | Open-Shrimp | OC-TG-Bot |
| -------------------------------------- | --- | ----------- | --------- |
| Inline approval buttons                | ✅  | ✅ Allow once / Accept all / Deny | ✅ Allow once / Always / Reject |
| Path-scoped auto-approval              | ✅  | ✅          | ❌ delegated to OpenCode config |
| Pattern-based tool rules (`Bash(git *)`) | ✅ | ✅         | ❌        |
| Out-of-directory protection            | ✅  | ✅          | ❌        |
| Interactive agent questions            | ✅ Question Form Mini App | ❌ | ✅ inline buttons + free-text answers |

### Status & control

| Feature                              | Zog | Open-Shrimp | OC-TG-Bot |
| ------------------------------------ | --- | ----------- | --------- |
| `/status`                            | ✅  | ✅          | ✅ health, project, model, context usage, changed files |
| Pinned auto-refreshing status message | ❌ | ✅          | ✅        |
| Cancel a running turn                | ✅ `/cancel` | ✅ `/cancel` | ✅ `/abort` |
| Detach from session w/o stopping it  | ❌  | ❌          | ✅ `/detach` |
| Agent-server lifecycle management    | n/a (in-process) | ➖ bundled runtime | ✅ `/opencode_start` / `/opencode_stop` + optional auto-restart monitor |
| Git worktree switching               | ❌  | ❌          | ✅ `/worktree` |
| Interactive file browser + download  | ❌  | ❌          | ✅ `/ls`  |
| Run agent commands / skills catalogs | ❌  | ❌          | ✅ `/commands`, `/skills` |
| Rename session                       | ❌  | ❌          | ✅ `/rename` |

### Scheduled tasks

| Feature                              | Zog | Open-Shrimp | OC-TG-Bot |
| ------------------------------------ | --- | ----------- | --------- |
| Recurring scheduled prompts          | ✅  | ✅          | ✅ `/task` (cron-like, 5-min minimum) |
| Safer / isolated execution           | ✅ restricted tool sets | ✅ restricted tool sets | ➖ runs `build` agent off-session; auto-rejects permission prompts |
| Listing / management                 | ✅ `/schedule(s)` | ✅ `/schedule(s)` | ✅ `/tasklist` |
| Persisted across restarts            | ✅  | ✅          | ✅        |
| Natural-language scheduling via agent tool | ❌ | ✅      | ❌        |

### Mini App (web UI)

| Feature                                  | Zog | Open-Shrimp | OC-TG-Bot |
| ---------------------------------------- | --- | ----------- | --------- |
| Companion Mini App (cloudflared, InitData auth) | ✅ | ✅     | ❌ deliberately native-UI-only |
| Hunk-level diff viewer w/ stage–unstage  | ✅  | ✅          | ❌        |
| Multi-directory review                   | ✅  | ✅          | ❌        |

### Voice

| Feature             | Zog | Open-Shrimp | OC-TG-Bot |
| ------------------- | --- | ----------- | --------- |
| Voice input (STT)   | ❌  | ✅ local Moonshine (no cloud) | ✅ any Whisper-compatible API |
| Voice replies (TTS) | ❌  | ❌          | ✅ `/tts` (OpenAI-compatible or Google Cloud) |

### MCP & background work

| Feature                          | Zog | Open-Shrimp | OC-TG-Bot |
| -------------------------------- | --- | ----------- | --------- |
| MCP server management            | ❌  | ✅ `/mcp` (status/tools/reset/enable/disable) | ✅ `/mcps` (browse / toggle) |
| Per-context MCP servers          | ❌  | ✅          | ❌ (OpenCode-level config) |
| Built-in agent-facing MCP tools  | ❌  | ✅ `send_file`, schedules, `edit_topic`, subagent launch | ❌ |
| Background subagents / tasks     | ❌  | ✅ `run_in_background` + `/tasks` | ➖ background-session notifications + live subagent activity display |

### Sandboxing & computer use

| Feature                    | Zog | Open-Shrimp | OC-TG-Bot |
| -------------------------- | --- | ----------- | --------- |
| Sandboxed agent execution  | ❌  | ✅ Docker / libvirt / Lima | ❌ |
| Computer use (GUI tools)   | ❌  | ✅          | ❌        |
| Live VNC viewer            | ❌  | ✅ `/vnc`   | ❌        |

### Ops & packaging

| Feature                    | Zog | Open-Shrimp | OC-TG-Bot |
| -------------------------- | --- | ----------- | --------- |
| systemd service            | ✅  | ✅ (+ launchd) | ✅ guide + optional `--daemon` |
| Structured logging         | ✅  | ✅          | ✅ levels + log-file retention |
| Single-user trust model    | ✅  | ✅          | ✅        |
| Interactive setup wizard   | ❌  | ✅          | ✅ first-launch config |
| Auto-update                | ❌  | ✅          | ❌        |
| Config hot-reload          | ❌  | ✅          | ❌        |
| UI localization            | ❌  | ❌          | ✅ 7 languages |
| Telegram network flexibility | ❌ | ❌         | ✅ forward proxy (SOCKS/HTTP), reverse proxy + shared secret, IPv4 forcing |

---

## Distinct to **Zog**

- **Claude Agent SDK backend** — runs the agent in-process via the Claude SDK
  (no separate agent server), rather than OpenCode.
- **AskUserQuestion → Question Form Mini App** — multi-question forms with tabs,
  multi-select, "Other" free-text, and a review-before-submit step.
- **Approval Queue Mini App** — batch review of pending tool approvals
  (approve/deny all), beyond inline buttons.
- **Notification Inbox Mini App** — scheduled-task results delivered as silent
  notifications into a Mini App inbox (instead of chat spam), with unread state,
  relative timestamps, and **"Continue in chat"** to convert a notification into a
  live session.
- **Document Viewer Mini App** — scrollable markdown reader with Prism syntax
  highlighting, auto table-of-contents, collapsible sections, and full-text search;
  long agent output (>8000 chars) auto-redirects here.
- **Notes / knowledge directory per context** — a dedicated `notes_directory`
  exposed to the agent for natural-language note-taking.
- **Model alias allowlist** — friendly aliases (`sonnet`/`opus`/`haiku`) validated
  against a hardcoded model list.
- **Per-rule approval config object** — `approval.default: auto` + path-glob rules
  declaring which tool categories require approval.
- **Single-instance lock** — lock file prevents duplicate pollers (Telegram 409).
- **`--schedules` separate file** — schedules live in their own
  `schedules.yaml`.

> Note: zog ships **one** React Mini App with five internal views (Diff, Document,
> Question Form, Approval Queue, Notification Inbox).

---

## Distinct to **Open-Shrimp**

### Agent backend & inference

- **Bundled OpenCode runtime** — binary ships OpenCode so users needn't install it.
- **`/connect` provider auth** — opens a Terminal Mini App with OpenCode's provider
  authentication UI.

### Sandboxing & isolation (major differentiator)

- **Sandbox execution backends** — **Docker** (Linux), **libvirt/QEMU** (Linux),
  **Lima** (macOS Virtualization.framework); agent runs isolated with only the
  project directory mounted.
- **Docker-in-Docker**, custom Dockerfiles, persistent disk overlays.
- **Sudo / host-escape mode** — `allow_host_escape: true` exposes a `host_bash`
  MCP tool with per-command approval (10s auto-deny) and an audit log (`sudo.log`).
- **Instance namespacing** — `instance_name` isolates DB / sandbox / images / VMs
  for multiple bot instances.

### Computer use (GUI automation)

- **Headless desktop in sandbox** — 1280×720 Wayland (labwc), Chromium + terminal.
- **GUI MCP tools** — `screenshot`, `click`, `type`, `key`, `scroll`, `toplevel`.
- **Screenshot streaming** — every screenshot auto-posted to the chat.
- **Live VNC viewer** — `/vnc` opens a noVNC Mini App to watch the agent's desktop
  in real time.

### MCP & async

- **Built-in OpenShrimp MCP tools** — `send_file`, `create_schedule`,
  `list_schedules`, `delete_schedule`, `edit_topic` (agent auto-titles topics),
  and **subagent launch** (`openshrimp_agent`).
- **Background subagents** — agent can launch subagents with `run_in_background`
  and notify on completion; `/tasks` lists them with elapsed time.
- **Natural-language scheduling** — describe a schedule in chat; agent calls the
  schedule MCP tool. Supports interval / cron / one-shot, concurrency caps,
  per-task instance limits, and failure notifications.

### Runtime directory management

- **`/add_dir`** — add a working directory at runtime, choosing "this session"
  (DB-only) or "remember" (writes YAML + hot-reload).

### More Mini Apps

- **Terminal Mini App** — SSE task-output viewer + provider-auth PTY (xterm.js).
- **Markdown Preview Mini App** — ephemeral GFM→HTML rendering.
- **VNC Viewer Mini App** — live desktop (noVNC).
- **Config Editor Mini App** — edit bot config YAML from the web UI.

### UX helpers & state

- **Prompt suggestions** — optional inline button predicting the next prompt,
  superseded when a new message arrives.
- **Accept-all-edits mode** — session-level toggle to auto-approve future
  Edit/Write until `/clear` or context switch.
- **Chat locking & defaults** — `locked_for_chats` and `default_for_chats` bind
  contexts to specific chats.

### Packaging & lifecycle

- **Pre-built binary distribution** — download-and-run, isolated venv self-setup.
- **Auto-update** — optional background binary updates (`auto_update`).
- **Config hot-reload** — YAML changes applied without restart (ruamel.yaml
  round-trip).
- **`openshrimp install` / `uninstall`** — one-command service setup (systemd /
  launchd), systemd lingering.
- **macOS menu-bar app** — native `.dmg`, start/stop, logs, config editor,
  "Start at Login", native setup dialog.
- **`openshrimp doctor`** — health check for OpenCode / Docker / libvirt / binaries.
- **`/restart`** and **`/config`** commands.
- **Auto-download of cloudflared / STT binaries** on demand.

---

## Distinct to **OpenCode-Telegram-Bot**

### Session control depth

- **Revert & fork from message history** — `/messages` browses the session's user
  messages and can revert the session to a prior state or fork a new session from
  any old message. Unique among the three.
- **`/detach`** — stop tracking a session without terminating it; with
  `TRACK_BACKGROUND_SESSIONS` the bot still sends short notifications when a
  detached session (same project/worktree) replies, asks a question, or requests
  a permission.
- **Git worktree switching** — `/worktree` detects and switches worktrees;
  `/status` shows branch + worktree.
- **`/rename`** — rename the current session.

### Native-Telegram UX (instead of Mini Apps)

- **Persistent reply keyboard** — agent / model / variant / context controls are
  always visible as keyboard buttons, not just slash commands.
- **Single-interaction-at-a-time flows** — only one interactive flow (Q&A,
  permission, confirmation) can be active; unrelated input is blocked with hints,
  preventing race conditions.
- **Agent-mode switching** — pick OpenCode agents (📋 plan, 🛠️ build, explore, …)
  from the reply keyboard; persisted.
- **Live subagent activity** — streams the running subagent's task, agent name,
  model, and current tool step into the chat.
- **Interactive file browser** — `/ls` navigates directories (within allowlisted
  roots) and downloads files straight to Telegram.

### OpenCode surface coverage

- **`/commands` and `/skills`** — browse and run OpenCode custom commands,
  built-ins (init, review, …) and the skills catalog from chat.
- **Model picker fed by OpenCode state** — favorites (starred in the OpenCode TUI)
  first, then recents; current model marked.
- **Server lifecycle from chat** — `/opencode_start` / `/opencode_stop`, plus an
  optional health monitor that auto-restarts a crashed local server
  (`OPENCODE_AUTO_RESTART_ENABLED`).

### Voice & i18n

- **TTS voice replies** — `/tts` toggles audio responses (OpenAI-compatible or
  Google Cloud TTS); STT voice input via any Whisper-compatible API.
- **Localization** — full UI translation in 7 languages (en, ar, de, es, fr, ru,
  zh) via `BOT_LOCALE`.

### Networking & distribution

- **Telegram connectivity options** — forward proxy (SOCKS4/5, HTTP/S), reverse
  proxy via `TELEGRAM_API_ROOT` with an `X-Proxy-Secret` shared secret, and IPv4
  forcing — built for corporate / restricted networks with **no open ports**.
- **npm distribution** — published package, `npm install -g` or npx, first-launch
  setup wizard, systemd guide.

---

## Summary

- **Shared core:** all three are single-user, long-polling Telegram bots with
  SQLite-persisted resumable sessions, streaming output, GFM→MarkdownV2
  rendering, attachments, multi-project switching, inline tool-approval buttons,
  `/status` + cancel, and persisted scheduled tasks.

- **Zog leans toward** a richer **single Mini App** experience around the Claude
  SDK: interactive question forms, an approval queue, and a notification-inbox
  workflow that turns scheduled results into resumable chats — plus per-context
  notes.

- **Open-Shrimp leans toward** **isolation, breadth, and packaging** on top of
  OpenCode: real sandboxes (Docker/libvirt/Lima), computer-use with live VNC,
  deep MCP integration, background subagents, runtime `/add_dir`, and polished
  cross-platform distribution (binary, doctor, menu-bar app, hot-reload,
  auto-update).

- **OpenCode-Telegram-Bot leans toward** being the **deepest pure-Telegram
  client for OpenCode**: no Mini App by design, but the widest command surface
  over OpenCode itself (sessions with revert/fork, worktrees, commands/skills
  catalogs, MCP toggling, server lifecycle), a persistent reply-keyboard UX,
  voice in *and* out, localization, and proxy-friendly networking. It
  deliberately skips forum topics, group chats, sandboxing, and path-scoped
  approvals (delegating permissions to OpenCode).

Balam (this repo) is architecturally closest to **open-shrimp** — it is also
OpenCode-backed (ADR-0011) and adapts open-shrimp's workspace-context model
(ADR-0012) — while sharing the Mini App ambitions seen in zog and open-shrimp.
OC-TG-Bot is the closest reference for *breadth of OpenCode API coverage* from
chat (sessions, agents, commands, skills, MCP toggling, server lifecycle), even
though its single-chat, no-topics model differs from Balam's forum-topic design.
