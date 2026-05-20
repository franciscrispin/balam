# Balam — Architecture Decision Records (ADRs)

Last updated: 2026-05-20

These records capture the key architecture decisions for Balam, a Telegram bot
backed by the [OpenCode](https://opencode.ai) coding agent, running locally on
an Ubuntu VM. Each record states one decision: its context, the decision, and
its consequences. This is not a feature list — it records the choices that shape
the system and why we made them.

Shared background: OpenCode is an open-source AI coding agent. It must run
locally on the VM so the model has direct access to local files and tools
(including a browser-use skill). Balam also has a Telegram Mini App — a web app
inside Telegram — for richer views such as git diffs and markdown.

---

## ADR-0001: OpenCode runs as a headless server; Balam is a client

Status: Accepted
Date: 2026-05-20

### Context
OpenCode's programmatic model is client/server. A headless server
(`opencode serve`) is the same OpenCode engine without the terminal UI: it
listens on an HTTP port and answers requests. Running it as a long-lived process
keeps sessions in memory and tool/MCP connections warm, and lets the bot restart
without losing the agent.

### Decision
Run OpenCode as a long-lived headless server on the VM. Balam talks to it as a
client. We do not embed OpenCode as an in-process library.

### Consequences
- The server runs from the working directory we want the agent to act on.
- We manage it as a service under **systemd**: it starts on boot, restarts on
  failure, and logs to journald. The backend health-checks the server (poll
  `/doc` or connect to the event stream) and waits for it before serving
  requests. Balam runs as its own systemd unit alongside it (see ADR-0004).
- The bot stays small. Its job is to move messages between Telegram and the
  server.

---

## ADR-0002: The HTTP API is the source of truth; the SDK is downstream

Status: Accepted
Date: 2026-05-20

### Context
OpenCode generates its OpenAPI spec from the server code, then generates the
TypeScript SDK from that spec (server code → OpenAPI spec → SDK). The SDK is a
generated convenience layer and can never do more than the HTTP API.

### Decision
Treat the OpenCode HTTP API as the contract and source of truth. Because we
build the backend in TypeScript (ADR-0004), we use the **official TypeScript
SDK** as our client — it is generated from this same contract — and drop to raw
HTTP calls only where the SDK lags the API.

### Consequences
- Any language has full access to OpenCode through the HTTP API, so the language
  choice never limits capability (see ADR-0004).
- The OpenAPI spec at `http://<host>:<port>/doc` stays the reference. If the SDK
  is missing an endpoint, we call the HTTP API directly against that spec.
- The SDK's real value is maintenance, not capability: it tracks OpenCode's
  frequent breaking changes for us and gives type-safe access, including the SSE
  event stream. We re-install it on OpenCode upgrades instead of re-generating
  and re-testing a hand-written client.

---

## ADR-0003: Three layers, with a fixed frontend language

Status: Accepted
Date: 2026-05-20

### Context
A Telegram Mini App is a web app, so its frontend must be TypeScript. OpenCode is
a separate process reached over HTTP. Only the middle layer is a free choice.
Naming the layers keeps responsibilities clear and stops agent logic leaking into
the UI, or UI logic into the agent.

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
│  TypeScript (Bun)    │   talks to OpenCode. See ADR-0004.
└──────────┬───────────┘
           │ HTTP + SSE
┌──────────┴───────────┐   The agent: model reasoning + local tools/files,
│  OpenCode server     │   runs the browser-use skill.
│  (separate process)  │
└──────────────────────┘
```

### Consequences
- The frontend stack (TypeScript + a JS build tool) is required regardless of
  backend language. Since the backend is also TypeScript (ADR-0004), frontend
  and backend share one language, one toolchain, and one set of shared types.
- Some features (git diffs, markdown viewing) are mostly backend + frontend work
  and do not need OpenCode at all.

---

## ADR-0004: Backend language is TypeScript, run on Bun

Status: Accepted
Date: 2026-05-20

### Context
Capability is equal across languages (ADR-0002), so we choose on operational fit.
Two facts decide it. First, we already need a TypeScript toolchain and codebase
for the Mini App frontend (ADR-0003), so TypeScript is unavoidable. Second,
OpenCode's only official SDK is TypeScript, generated from the same OpenAPI spec,
and it already implements the SSE event-stream client. Picking a different
backend language would mean rebuilding that client by hand and running two
toolchains with duplicate type definitions — work the frontend choice does not
require.

### Decision
Write the backend in TypeScript and run it on **Bun**. Node is a drop-in fallback
if a dependency is incompatible with Bun, since the same code runs on both.

### Consequences
- We use the official OpenCode SDK (`@opencode-ai/sdk`) as our client. No
  hand-written HTTP client and no hand-written SSE consumer.
- One language across backend and frontend: one toolchain, shared types (diff
  hunk, file model) defined once, shared validation. No hand-synced duplicate
  type definitions.
- Single-file deployment is preserved: `bun build --compile` produces a
  standalone executable, so we keep the single-binary operational benefit that
  first made Go attractive, without giving up TypeScript.
- Bun's fast built-in HTTP/WebSocket server suits reverse-proxying the noVNC
  WebSocket and serving the Mini App.
- Risk: Bun is younger than Node. If a library misbehaves, we fall back to Node
  with the same code.

Well supported in TypeScript (not extra work): the Telegram bot (for example
grammY), Mini App `initData` validation (HMAC-SHA256 via the crypto API),
running `git diff`, and serving markdown.

---

## ADR-0005: Browser automation is an OpenCode skill, not backend code

Status: Accepted
Date: 2026-05-20

### Context
OpenCode loads Anthropic-compatible skills and runs them as part of the agent
loop. Keeping browser control inside OpenCode makes the backend language (Go)
irrelevant to it, and reuses the existing browser-use skill and its persistent
Chrome profile on the VM.

### Decision
The model uses the existing browser-use skill through OpenCode. The backend does
not drive the browser itself.

### Consequences
- OpenCode discovers skills from both user scope (`~/.config/opencode/skills`,
  `~/.claude/skills`, `~/.agents/skills`) and project scope (`.opencode/skills`,
  `.claude/skills`, `.agents/skills`, walking up from the working directory to
  the git worktree root). So `.claude/skills` *is* a discovered path — the
  earlier worry that OpenCode ignores `.claude` folders was wrong.
- The reason we still copy the skill is location, not the folder name: the source
  at `/home/ubuntu/projects/computer-use/.claude/skills/browser-use` lives in a
  different project's tree, which is neither the agent's working directory nor a
  user-scope folder, so OpenCode would not find it from where Balam runs the
  agent. We copy it into the user scope, where it is discovered globally:
  ```
  cp -r /home/ubuntu/projects/computer-use/.claude/skills/browser-use \
        ~/.config/opencode/skills/browser-use
  ```
- We copy rather than symlink on purpose: that source is itself a reference copy,
  not the canonical source, so we want an independent snapshot. If we ever update
  the reference, copy it again.

---

## ADR-0006: The live Chrome view is an embedded noVNC iframe, not a screenshot relay

Status: Accepted
Date: 2026-05-20

### Context
OpenCode has no "show the browser" feature, so this view is ours to build. noVNC
gives a smooth, real-time picture of the actual desktop over a standard,
well-tested stack (VNC server + websockify + noVNC), instead of a custom pipeline
that captures screenshots and pushes them over WebSocket. An iframe is the least
code and can be interactive later (we run it view-only for now).

### Decision
Show the running Chrome in the Mini App by embedding a noVNC viewer as an
`<iframe>`. Chrome runs on an X display on the VM, a VNC server exposes that
display, and noVNC (a JavaScript VNC client) renders it live in the browser. The
Mini App points an iframe at the noVNC page; it does not draw frames itself.

### Consequences
- The VM must run Chrome under an X display (for example Xvfb), a VNC server (for
  example x11vnc or TigerVNC), and websockify (or a WebSocket-capable VNC server)
  so noVNC can connect over WebSocket.
- **The browser-use skill's headed Chrome must run on the same X display the VNC
  server exposes.** The skill (ADR-0005) and the VNC server must agree on
  `DISPLAY`, or the iframe shows an empty desktop. This is the explicit link
  between ADR-0005 and this view.
- The backend serves the Mini App and reverse-proxies the noVNC WebSocket, so the
  viewer is same-origin and sits behind our auth.
- The Mini App's content security policy must allow the iframe (`frame-src`), and
  the page must load inside Telegram's webview.
- No screenshot frame format and no custom "browser-frame message" type to define
  and maintain.
- **Lock the endpoint.** Bind the VNC server and websockify to `127.0.0.1`, never
  expose those ports, reach them only through the backend's authenticated,
  token-checked reverse proxy, and keep the viewer view-only (see ADR-0007 and
  ADR-0008).

---

## ADR-0007: Local, single-user deployment on the VM

Status: Accepted
Date: 2026-05-20

### Context
The goal is to give the agent full local file and tool access. With one trusted
user on one machine, we do not need per-user sandboxing.

### Decision
Run the whole system locally on the Ubuntu VM for a single user.

### Consequences
- Bind the OpenCode server to `127.0.0.1` and set `OPENCODE_SERVER_PASSWORD`.
- Do not expose the OpenCode port to the internet. Only Balam reaches it.
- **Telegram reaches in from the internet.** Binding ports to `127.0.0.1` stops
  other machines from connecting to the OpenCode or VNC ports. But commands do not
  arrive over those ports — they arrive through Telegram: anyone who knows the
  bot's name can open a chat and message it, and Telegram's servers pass that
  message to the bot on the VM. Closing local ports does not block this. Deciding
  who is allowed to message the bot is its own decision — see ADR-0008.
- If this becomes multi-user or public, revisit this decision (sandbox per user,
  isolation), because the agent can edit files and run shell commands.

---

## ADR-0008: The Telegram entry point is the real trust boundary

Status: Accepted
Date: 2026-05-20

### Context
ADR-0007 keeps every port on `127.0.0.1`, but the bot is driven through Telegram,
which is internet-facing by nature. Anyone who can message the bot — or anyone
holding a leaked bot token — can reach the backend, and the agent can edit files
and run shell commands on the VM. "Local single-user" describes the deployment,
not this entry point. Without an authorization check, the system is effectively
open remote code execution.

### Decision
Treat the Telegram entry point as the trust boundary and lock it to one user:
- **Allowlist by Telegram user ID.** Accept updates only from the single owner's
  numeric user ID; silently ignore everyone else. Do not rely on username or chat
  title, which can change.
- **Validate Mini App `initData`** on every Mini App request (HMAC-SHA256 with
  the bot token, per Telegram's spec), and check that the embedded user ID is the
  allowed user.
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

Status: Accepted
Date: 2026-05-20

### Context
A coding agent needs more than one line of conversation: the user runs several
tasks and wants each to keep its own context. OpenCode models this as sessions.
Telegram forum topics give a native, built-in way to keep parallel threads in one
chat, without inventing a custom switching UI in the bot.

### Decision
Map **one Telegram forum topic to one OpenCode session**. Creating a topic starts
a new session; posting in an existing topic continues its session. The backend
keeps the topic-to-session mapping and routes each message to the right session.

### Consequences
- The owner's chat must be a forum (topics enabled). Each topic is an isolated
  task with its own history.
- The backend persists the topic→session map so threads survive a restart
  (sessions stay warm in the long-lived server, ADR-0001).
- A new topic with no session yet triggers session creation; a topic whose
  session is gone is recreated or reported, not silently dropped.
- Telegram's "General" topic can default to a single catch-all session.

---

## Summary

| ADR | Decision | Core reason |
|-----|----------|-------------|
| 0001 | OpenCode as headless server (systemd), Balam as client | Keeps sessions/tools warm; bot stays small |
| 0002 | HTTP API is source of truth; use the official TS SDK as client | Contract-first, but reuse the maintained SDK |
| 0003 | Three layers; frontend is fixed TypeScript | Clear responsibilities; Mini App must be web |
| 0004 | Backend in TypeScript on Bun | One language with the frontend; official SDK; single executable |
| 0005 | Browser-use as an OpenCode skill | Reuse skill; backend language irrelevant to it |
| 0006 | Live Chrome via embedded noVNC iframe | Real-time view from a standard stack; least UI code |
| 0007 | Local single-user on the VM | Full local access; minimal security surface |
| 0008 | Telegram entry point is the trust boundary; allowlist one user ID | The bot is internet-facing even when ports are local |
| 0009 | One Telegram forum topic = one OpenCode session | Native parallel task threads, no custom UI |
