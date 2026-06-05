# Feature Comparison: Zog vs. Open-Shrimp

A side-by-side catalog of features in **zog** (`/home/ubuntu/projects/zog`) and
**open-shrimp** (`/home/ubuntu/projects/open-shrimp`), marking which features
overlap and which are distinct to each project.

## At a glance

|                  | **Zog**                                   | **Open-Shrimp**                                       |
| ---------------- | ----------------------------------------- | ----------------------------------------------------- |
| One-liner        | Personal Telegram bot powered by the **Claude Agent SDK** | Self-hosted Telegram bot powered by an **OpenCode** coding agent |
| Agent backend    | Claude Agent SDK (in-process)             | OpenCode server over HTTP/SSE                          |
| Language         | Python 3.13+                              | Python 3.11+                                           |
| Package manager  | uv                                        | uv                                                     |
| Telegram client  | python-telegram-bot (long polling)        | python-telegram-bot (long polling)                    |
| Persistence      | SQLite (aiosqlite)                         | SQLite (aiosqlite)                                     |
| Frontend         | React/TS Mini App (single app, 5 views)   | React/TS Mini Apps (5 separate apps)                  |
| Scope            | Single-user, home-server self-host        | Single-user, self-host (Linux + macOS)                |
| Distribution     | uv run / systemd user service             | Pre-built binary, systemd, launchd, macOS menu-bar app |

Both are, fundamentally, the same idea — *a coding agent driven from Telegram* —
which is why a large core overlaps. They diverge most in the agent backend
(Claude SDK vs. OpenCode) and in how far open-shrimp pushes into sandboxing,
computer-use, and packaging.

---

## Overlapping features (present in both)

### Core agent & messaging

- **Telegram coding agent** — drive a coding agent entirely from Telegram chat.
- **Multi-turn sessions, persisted in SQLite** — conversations survive restarts;
  session state keyed per chat/topic.
- **Session resume** — `/resume` lists recent sessions with metadata and reopens one.
- **New / clear session** — start a fresh conversation (`/clear`).
- **Streaming draft messages** — live `sendMessageDraft` (Bot API 9.5) updates as
  the agent thinks/acts.
- **Message auto-splitting** — respect Telegram's 4096-char limit at paragraph /
  code-block boundaries.
- **GFM → Telegram MarkdownV2 rendering** — both use a custom **mistune** renderer.
- **File attachments** — accept images / PDFs / text files saved to a temp path,
  referenced in the prompt.
- **Forum topic support** — each forum topic gets its own context + session;
  independent parallel conversations.
- **Group-chat etiquette** — bot only responds to @mentions and direct replies in
  group chats.
- **Tool-output handling** — truncate long tool/bash output inline with a
  "show full output" affordance.

### Contexts & configuration

- **Multi-project contexts** — named workspaces bundling a directory (+ model,
  tools, etc.); switch via `/context`.
- **Default context** — auto-applied context for new sessions / unbound topics.
- **Additional directories** — extend the agent's allowed paths beyond the primary
  context directory.
- **Per-context model override** — each context can pin its own model.
- **YAML configuration** — config file under `~/.config/<app>/config.yaml`.
- **User allowlist** — only listed Telegram user IDs may interact.

### Model control

- **Model override command** — `/model` shows / sets / (open-shrimp) resets the
  model for the chat.

### Tool approval

- **Path-scoped auto-approval** — read-style tools (read/glob/grep) auto-approved
  within allowed directories.
- **Inline approval buttons** — Write/Edit/Bash operations gated behind Telegram
  approve/deny keyboards.
- **Pattern-based tool rules** — allowlist patterns like `Bash(git *)`.
- **Out-of-directory protection** — files outside allowed dirs always require approval.

### Status & control

- **`/status`** — show current context, model, session info.
- **`/cancel`** — abort a running agent turn (per topic / chat).

### Scheduled tasks

- **Recurring scheduled prompts** — cron-style tasks run automatically in the
  background.
- **Read-only-ish task isolation** — scheduled runs are restricted to safer tool sets.
- **Schedule listing / management** — `/schedule(s)` to view and control tasks.
- **SQLite-persisted schedules** — reloaded on startup.

### Mini App (web UI)

- **Companion Mini App over HTTPS** — served via **cloudflared** tunnel, authed
  with Telegram InitData.
- **Diff / code-review viewer** — hunk-level git diff review with stage / unstage
  (à la `git add -p`).
- **Multi-directory review** — review per directory when a context has extras.

### Ops

- **systemd user service** — packaged for background self-hosted running.
- **Structured logging** — Python `logging`, debug levels, exception-safe handlers.
- **Single-user trust model** — locked to one operator by design.

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

- **OpenCode backend** — drives `opencode serve` over HTTP/SSE; uses any
  OpenCode-supported provider/model (`openai/…`, `anthropic/…`, etc.).
- **Bundled OpenCode runtime** — binary ships OpenCode so users needn't install it.
- **Thinking-effort levels** — `/effort {low,medium,high,xhigh,max}` to tune
  reasoning depth.
- **`/connect` provider auth** — opens a Terminal Mini App with OpenCode's provider
  authentication UI.
- **`/model reset`** — explicit revert of a chat-scoped model override.

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

### Voice & input

- **Voice transcription** — local **Moonshine STT** auto-transcribes OGG/Opus voice
  notes (no cloud), binary auto-downloaded on first use.

### MCP & extensibility

- **Per-context MCP servers** — command / HTTP / embedded MCP servers per context.
- **Rich `/mcp` management** — list status/tools/version, `reset`/`enable`/`disable`,
  reconnect failed servers.
- **Built-in OpenShrimp MCP tools** — `send_file`, `create_schedule`,
  `list_schedules`, `delete_schedule`, `edit_topic` (agent auto-titles topics),
  and **subagent launch** (`openshrimp_agent`).

### Tasks & async

- **Background subagents** — agent can launch subagents with `run_in_background`
  and notify on completion.
- **`/tasks`** — list active background tasks with elapsed time.
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
- **Pinned status message** — pinned chat message showing context / session /
  running status, auto-refreshed.
- **Accept-all-edits mode** — session-level toggle to auto-approve future
  Edit/Write until `/clear` or context switch.
- **Three-button approval UX** — Allow once / Accept all [tool] / Deny.
- **Chat locking & defaults** — `locked_for_chats` and `default_for_chats` bind
  contexts to specific chats.

### Packaging & lifecycle

- **Interactive first-run setup wizard** — collects token / user ID / directory,
  generates config.
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

## Summary

- **Shared core (~25 features):** Telegram-driven coding agent, multi-context
  workspaces, persisted resumable sessions, streaming drafts, MarkdownV2 rendering,
  attachments, forum topics, path-scoped tool approval, scheduled tasks, and a
  cloudflared-tunneled Mini App with hunk-level diff review.

- **Zog leans toward** a richer **single Mini App** experience around the Claude
  SDK: interactive question forms, an approval queue, and a notification-inbox
  workflow that turns scheduled results into resumable chats — plus per-context
  notes.

- **Open-Shrimp leans toward** **isolation, breadth, and packaging** on top of
  OpenCode: real sandboxes (Docker/libvirt/Lima), computer-use with live VNC,
  voice input, deep MCP integration, background subagents, runtime `/add_dir`,
  prompt suggestions, and polished cross-platform distribution (binary, doctor,
  menu-bar app, hot-reload, auto-update).

Balam (this repo) is architecturally closest to **open-shrimp** — it is also
OpenCode-backed (ADR-0011) and adapts open-shrimp's workspace-context model
(ADR-0012) — while sharing the Mini App ambitions seen in both.
