# Balam — Architecture Decision Records (ADRs)

Last updated: 2026-06-05

These records capture the key architecture decisions for Balam, a Telegram bot
backed by the [OpenCode](https://opencode.ai) coding agent, running locally on
an Ubuntu VM. Each record states one decision: its context, the decision, and
its consequences.

Shared background: OpenCode is an open-source AI coding agent. It must run
locally on the VM so the model has direct access to local files and tools and
skills. Balam also has a Telegram Mini App — a web app inside Telegram — for
richer views such as git diffs and markdown.

---

## ADR-0001: OpenCode runs as a headless server; Balam is a client

Status: Accepted Date: 2026-05-20

### Context

OpenCode's programmatic model is client/server. A headless server (`opencode
serve`) is the same OpenCode engine without the terminal UI: it listens on an
HTTP port and answers requests. Running it as a long-lived process keeps
sessions in memory and tool/MCP connections warm, and lets the bot restart
without losing the agent.

### Decision

Run OpenCode as a long-lived headless server on the VM. Balam talks to it as a
client. We do not embed OpenCode as an in-process library.

### Consequences

- The server runs from the working directory we want the agent to act on.
- We manage it as a service under **systemd**: it starts on boot, restarts on
  failure, and logs to journald. The backend health-checks the server (poll
  `/doc` or connect to the event stream) and waits for it before serving
  requests. Balam runs as its own systemd unit alongside it.
- The bot stays small. Its job is to move messages between Telegram and the
  server.

---

## ADR-0002: The HTTP API is the source of truth; we call it directly

Status: Accepted Date: 2026-05-20

### Context

OpenCode generates its OpenAPI spec from the server code, then generates a
TypeScript SDK from that spec (server code → OpenAPI spec → SDK). The SDK is a
generated convenience layer and can never do more than the HTTP API.

### Decision

Treat the OpenCode HTTP API as the contract and source of truth. The backend
(Python, ADR-0011) calls it directly with a thin, hand-maintained `httpx`
client over the endpoints it needs (`/doc`, `/session`,
`/session/{id}/prompt_async`, `/event`), rather than through OpenCode's
generated TypeScript SDK.

### Consequences

- Any language has full access to OpenCode through the HTTP API, so the language
  choice never limits capability (see ADR-0011).
- The OpenAPI spec at `http://<host>:<port>/doc` stays the reference. If an
  endpoint is undocumented in the client, we call the HTTP API directly against
  that spec.
- We own the small client and track OpenCode's frequent breaking changes
  ourselves against `/doc`. The generated TypeScript SDK would absorb that
  maintenance for us, but it is a TypeScript client and the backend is Python
  (ADR-0011); consuming the `/event` SSE stream is only a handful of lines over
  `httpx`, so the cost is small and bounded.

---

## ADR-0003: Three layers, with a fixed frontend language

Status: Accepted Date: 2026-05-20

### Context

A Telegram Mini App is a web app, so its frontend must be TypeScript. OpenCode
is a separate process reached over HTTP. Only the middle layer is a free choice.
Naming the layers keeps responsibilities clear and stops agent logic leaking
into the UI, or UI logic into the agent.

### Decision

Split the system into three layers:

```
┌──────────────────────┐   The diff viewer, markdown viewer, live Chrome view.
│  Mini App frontend   │   Runs inside Telegram's webview.
│  (TypeScript — fixed)│   Always TypeScript/JavaScript. No choice here.
└──────────┬───────────┘
           │ HTTP / WebSocket
┌──────────┴───────────┐   Receives Telegram updates, serves the Mini App,
│  Balam backend       │   runs git, reads files, proxies the noVNC stream,
│  (Python: FastAPI +  │   talks to OpenCode. See ADR-0011.
│  python-telegram-bot)│
└──────────┬───────────┘
           │ HTTP + SSE
┌──────────┴───────────┐   The agent: model reasoning + local tools/files,
│  OpenCode server     │   runs the browser-use skill.
│  (separate process)  │
└──────────────────────┘
```

### Consequences

- The frontend stack (TypeScript + a JS build tool) is required regardless of
  backend language.
- The backend is Python (ADR-0011), so frontend and backend do not share a
  language. The Mini App contract is kept in sync by generating the frontend's
  TypeScript types from the backend's **FastAPI-emitted OpenAPI schema** (single
  source of truth, no hand-synced duplicate definitions).
- Some features (git diffs, markdown viewing) are mostly backend + frontend work
  and do not need OpenCode at all.

---

## ADR-0005: Browser automation is an OpenCode skill, not backend code

Status: Accepted Date: 2026-05-20

### Context

OpenCode loads Anthropic-compatible skills and runs them as part of the agent
loop. Keeping browser control inside OpenCode makes the backend language
irrelevant to it, and reuses the existing browser-use skill and its persistent
Chrome profile on the VM.

### Decision

The model uses the existing browser-use skill through OpenCode. The backend does
not drive the browser itself.

### Consequences

- OpenCode discovers skills from both user scope (`~/.config/opencode/skills`,
  `~/.claude/skills`, `~/.agents/skills`) and project scope (`.opencode/skills`,
  `.claude/skills`, `.agents/skills`, walking up from the working directory to
  the git worktree root). So `.claude/skills` _is_ a discovered path.

---

## ADR-0006: The live Chrome view is an embedded noVNC iframe, not a screenshot relay

Status: Accepted Date: 2026-05-20

### Context

OpenCode has no "show the browser" feature, so this view is ours to build. noVNC
gives a smooth, real-time picture of the actual desktop over a standard,
well-tested stack (VNC server + websockify + noVNC), instead of a custom
pipeline that captures screenshots and pushes them over WebSocket. An iframe is
the least code and can be interactive later (we run it view-only for now).

### Decision

Show the running Chrome in the Mini App by embedding a noVNC viewer as an
`<iframe>`. Chrome runs on an X display on the VM, a VNC server exposes that
display, and noVNC (a JavaScript VNC client) renders it live in the browser. The
Mini App points an iframe at the noVNC page; it does not draw frames itself.

### Consequences

- The VM must run Chrome under an X display (for example Xvfb), a VNC server
  (for example x11vnc or TigerVNC), and websockify (or a WebSocket-capable VNC
  server) so noVNC can connect over WebSocket.
- **The browser-use skill's headed Chrome must run on the same X display the VNC
  server exposes.** The skill (ADR-0005) and the VNC server must agree on
  `DISPLAY`, or the iframe shows an empty desktop. This is the explicit link
  between ADR-0005 and this view.
- The backend serves the Mini App and reverse-proxies the noVNC WebSocket, so
  the viewer is same-origin and sits behind our auth.
- The Mini App's content security policy must allow the iframe (`frame-src`),
  and the page must load inside Telegram's webview.
- No screenshot frame format and no custom "browser-frame message" type to
  define and maintain.
- **Lock the endpoint.** Bind the VNC server and websockify to `127.0.0.1`,
  never expose those ports, reach them only through the backend's authenticated,
  token-checked reverse proxy, and keep the viewer view-only (see ADR-0007 and
  ADR-0008).

---

## ADR-0007: Local, single-user deployment on the VM

Status: Accepted Date: 2026-05-20

### Context

The goal is to give the agent full local file and tool access. With one trusted
user on one machine, we do not need per-user sandboxing.

### Decision

Run the whole system locally on the Ubuntu VM for a single user.

### Consequences

- Bind the OpenCode server to `127.0.0.1` and set `OPENCODE_SERVER_PASSWORD`.
- Do not expose the OpenCode port to the internet. Only Balam reaches it.
- **Telegram reaches in from the internet.** Binding ports to `127.0.0.1` stops
  other machines from connecting to the OpenCode or VNC ports. But commands do
  not arrive over those ports — they arrive through Telegram: anyone who knows
  the bot's name can open a chat and message it, and Telegram's servers pass
  that message to the bot on the VM. Closing local ports does not block this.
  Deciding who is allowed to message the bot is its own decision — see ADR-0008.
- If this becomes multi-user or public, revisit this decision (sandbox per user,
  isolation), because the agent can edit files and run shell commands.

---

## ADR-0008: The Telegram entry point is the real trust boundary

Status: Accepted Date: 2026-05-20

### Context

ADR-0007 keeps every port on `127.0.0.1`, but the bot is driven through
Telegram, which is internet-facing by nature. Anyone who can message the bot —
or anyone holding a leaked bot token — can reach the backend, and the agent can
edit files and run shell commands on the VM. "Local single-user" describes the
deployment, not this entry point. Without an authorization check, the system is
effectively open remote code execution.

### Decision

Treat the Telegram entry point as the trust boundary and lock it to one user:

- **Allowlist by Telegram user ID.** Accept updates only from the single owner's
  numeric user ID; silently ignore everyone else. Do not rely on username or
  chat title, which can change.
- **Validate Mini App `initData`** on every Mini App request (HMAC-SHA256 with
  the bot token, per Telegram's spec), and check that the embedded user ID is
  the allowed user.
- **Protect the bot token.** Keep it out of the repo, in an environment file or
  secret read by the systemd unit, with tight file permissions.

### Consequences

- A stranger messaging the bot gets nothing; only the owner can drive the agent.
- If the token leaks, an attacker can still send updates, but the user-ID
  allowlist rejects them. Rotating the token stays the recovery step.
- This is the control that makes ADR-0007's "minimal security surface" true. If
  the system ever goes multi-user, this ADR and ADR-0007 are revisited together.

---

## ADR-0009: One Telegram forum topic maps to one OpenCode session

Status: Accepted Date: 2026-05-20

### Context

A coding agent needs more than one line of conversation: the user runs several
tasks and wants each to keep its own context. OpenCode models this as sessions.
Telegram forum topics give a native, built-in way to keep parallel threads in
one chat, without inventing a custom switching UI in the bot.

### Decision

Map **one Telegram forum topic to one OpenCode session**. Creating a topic
starts a new session; posting in an existing topic continues its session. The
backend keeps the topic-to-session mapping and routes each message to the right
session.

### Consequences

- The owner's chat must be a forum (topics enabled). Each topic is an isolated
  task with its own history.
- The backend persists the topic→session map so threads survive a restart
  (sessions stay warm in the long-lived server, ADR-0001).
- A new topic with no session yet triggers session creation; a topic whose
  session is gone is recreated or reported, not silently dropped.
- Telegram's "General" topic can default to a single catch-all session.

---

## ADR-0010: Telegram over Discord as the messaging platform

Status: Accepted Date: 2026-06-04

### Context

Balam's whole purpose is to be a chat front end for a coding agent, so the
choice of messaging platform is foundational. Two requirements shape it:

- **One channel per directory path** (e.g. `~/otp`, `~/mts`, `~/projects`) — the
  top-level grouping the owner browses by.
- **One thread/topic per OpenCode session** inside that grouping (ADR-0009).

That is a two-level tree (directory → session). Discord models this natively
(server → channel → thread) and a Discord bot can create both channels and
threads programmatically. Telegram was therefore re-evaluated against Discord
rather than assumed. Two earlier beliefs that favored Discord turned out to be
wrong on inspection:

1. **"Telegram has no streaming API."** False as of Bot API 9.3 (Dec 31, 2025),
   which added `sendMessageDraft` for native, flicker-free streaming of partial
   messages; Bot API 9.5 (Mar 1, 2026) opened it to all bots. Discord still has
   **no** first-party streaming — bots fake it by editing a message as tokens
   arrive, which burns per-channel rate limits. For an agent that relays
   incremental output, this is now a clear Telegram advantage.
2. **"Telegram can't express the two-level tree."** It can: forum topics are
   single-level _within one supergroup_, but using **one supergroup per
   directory** (the "channel") with **one forum topic per session** inside it
   yields the required two levels — and each directory becomes a top-level entry
   in the chat list.

Other factors: the rich Mini App views (git diffs, markdown, live noVNC Chrome,
ADR-0006) have no Discord equivalent (Activities cannot embed an arbitrary
iframe); Telegram topics never auto-archive, whereas Discord threads do (max 7
days) and must be programmatically un-archived; and Telegram is investing
first-party effort in AI-agent primitives (streaming, managed bots, bot-to-bot),
which is precisely this project's domain. Discord's one surviving edge is fully
programmatic provisioning of the directory level.

### Decision

Build Balam on **Telegram**. Map the directory→session tree as:

- **One Telegram supergroup (forum) per directory path** — the "channel".
- **One forum topic per OpenCode session** within it (ADR-0009 unchanged).

Stream agent output with `sendMessageDraft`, falling back to throttled
`editMessageText` only where the native method does not fit.

### Consequences

- A Telegram **bot cannot create supergroups** via the Bot API, so the owner
  creates the handful of per-directory supergroups by hand once and adds the
  bot; the bot then auto-creates session topics (`createForumTopic`). For a few
  repositories this one-time manual setup is acceptable.
- The backend's persisted mapping (ADR-0009) extends to **supergroup → directory
  (working dir)** in addition to **topic → session**, so each session's OpenCode
  server runs against the right `BALAM_WORKDIR` (ADR-0001).
- The trust boundary (ADR-0008) now spans multiple supergroups: the user-ID
  allowlist still gates every update, and only owner-created groups are honored.
- Re-evaluate if the project ever goes multi-user or if Discord ships a
  first-party streaming primitive and iframe-capable embeds.

---

## ADR-0011: Backend language is Python

Status: Accepted Date: 2026-06-04

### Context

Capability is equal across languages (ADR-0002): any language has full access to
OpenCode through its HTTP API, so the backend language is chosen on operational
fit, not capability. The frontend is fixed TypeScript (ADR-0003), but the
backend is a free choice. Two factors decide it for Python:

- **Reference reuse.** The build leans heavily on two existing Python codebases —
  `~/projects/zog` and `~/projects/open-udang` — as worked examples for the
  hardest parts: animated draft streaming into forum topics (`send_message_draft`),
  GFM→Telegram-MarkdownV2 rendering (`mistune`), and the live noVNC Mini App
  (ADR-0006). In Python these are direct references; in any other language each
  would be a *translation* (effort + divergence risk).
- **Mature Telegram tooling.** `python-telegram-bot` (22.6+) exposes everything
  this project needs, including `send_message_draft` for native streaming — so
  the streaming advantage that motivated Telegram (ADR-0010) is fully available
  in Python.

The one real cost is that frontend (TypeScript) and backend no longer share a
language, so shared types are not free. It is mitigated: FastAPI emits an OpenAPI
schema from the backend, and the frontend's types are generated from it (ADR-0003)
— arguably a cleaner contract than hand-shared types. Not using OpenCode's
generated TypeScript SDK is a second, bounded cost (ADR-0002): the SSE stream is
a handful of lines over `httpx`.

### Decision

Write the backend in **Python**. Concretely:

- **Runtime/tooling:** Python 3.12+, managed with **uv**; **ruff** for lint +
  format.
- **Telegram:** **python-telegram-bot** (long polling for this local,
  no-public-URL deployment, ADR-0007), using `send_message_draft` for streaming.
- **OpenCode client:** a thin **httpx** wrapper over the HTTP API (ADR-0002), no
  generated SDK.
- **Mini App HTTP/WS:** **FastAPI + uvicorn** (serves the Mini App, exposes the
  API, will reverse-proxy the noVNC WebSocket, ADR-0006), with its OpenAPI schema
  as the source for the frontend's generated types (ADR-0003).
- **Frontend:** TypeScript + Vite (ADR-0003), the only fixed layer.

### Consequences

- The repo is polyglot: a Python backend (`apps/backend`, uv) beside a
  TypeScript frontend (`apps/frontend`, Bun/Vite). They do not share a
  toolchain; the contract between them is the generated OpenAPI client.
- We own a small hand-written OpenCode HTTP/SSE client and track OpenCode's
  changes against the `/doc` spec ourselves (ADR-0002).
- The backend ships as a uv-managed app under systemd (ADR-0001) or a container.
- Revisit only if the Mini App's shared-contract surface grows large enough that
  a single language across both layers would clearly win.

---

## Summary

| ADR  | Decision                                                           | Core reason                                                     |
| ---- | ------------------------------------------------------------------ | --------------------------------------------------------------- |
| 0001 | OpenCode as headless server (systemd), Balam as client             | Keeps sessions/tools warm; bot stays small                      |
| 0002 | HTTP API is source of truth; thin `httpx` client, no SDK           | Contract-first; language never limits capability                |
| 0003 | Three layers; frontend is fixed TypeScript                         | Clear responsibilities; Mini App must be web                    |
| 0005 | Browser-use as an OpenCode skill                                   | Reuse skill; backend language irrelevant to it                  |
| 0006 | Live Chrome via embedded noVNC iframe                              | Real-time view from a standard stack; least UI code             |
| 0007 | Local single-user on the VM                                        | Full local access; minimal security surface                     |
| 0008 | Telegram entry point is the trust boundary; allowlist one user ID  | The bot is internet-facing even when ports are local            |
| 0009 | One Telegram forum topic = one OpenCode session                    | Native parallel task threads, no custom UI                      |
| 0010 | Telegram over Discord; supergroup-per-directory, topic-per-session | Native streaming + Mini App + no archiving; two-level tree fits |
| 0011 | Backend in Python (FastAPI + PTB), OpenCode over HTTP              | Reference reuse (zog/open-udang); HTTP is the contract (0002)    |
