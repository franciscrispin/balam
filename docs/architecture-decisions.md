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

Status: Accepted, amended 2026-06-11 Date: 2026-05-20

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

### Amendment (2026-06-11)

Built with the same substance — the standard noVNC/VNC stack, no screenshot
relay, a backend-authenticated proxy, view-only — but two mechanics changed now
that the Mini App shell (React + auth + design system) exists, following the
open-shrimp reference (ADR-0011):

- **The noVNC RFB client is imported in-page (`@novnc/novnc`), not an iframe.**
  The browser view renders the RFB canvas straight into its content area, inside
  the same React shell, theme, and `initData` auth context. The iframe was the
  least-code option before the shell existed; stock `vnc.html` would now bring a
  second UI chrome and a machine-level noVNC checkout into the serving path. The
  `frame-src` CSP consequence above is void.
- **The backend bridges the WebSocket straight to x11vnc** (`/api/vnc/ws` ↔ TCP
  `127.0.0.1:5900`, `balam.vnc`), so websockify and the noVNC web checkout on
  `:6081` are a dev convenience of the browser-use skill, not part of serving.
  Auth is the client's `initData` sent as the **first text frame** — a browser
  cannot set an `Authorization` header on a WebSocket, and a `?token=` query
  param would land verbatim in uvicorn's accept log (verified live). The
  ordering is safe because in RFB the server speaks first; the bridge stays
  silent until the frame passes the same owner allowlist (ADR-0008). The
  backend never starts the VNC stack: `GET /api/browser/status` probes it and
  the view shows an offline state when it is down.

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
- **Optional chat scoping (`ALLOWED_TELEGRAM_CHAT_ID`).** Balam now targets the
  "workspace" forum **supergroup** (ADR-0010) rather than the owner's DM. When this
  `-100…` chat id is set, both handlers require it **in addition to** the owner id
  (`filters.User & filters.Chat`), so the bot acts only inside that group and
  ignores the owner everywhere else (including the old DM). It is a defense-in-depth
  narrowing of the entry point, not a replacement for the user-ID gate, which always
  applies; unset, the bot keeps the legacy owner-anywhere behavior.
- This is the control that makes ADR-0007's "minimal security surface" true. If
  the system ever goes multi-user, this ADR and ADR-0007 are revisited together.

### Amendment (2026-09-01): the allowlist may name more than one person

The boundary is unchanged in kind — it is still an allowlist of numeric Telegram
user IDs — but it is now a **list**, not a single id. `ADDITIONAL_TELEGRAM_USER_IDS`
(comma-separated) adds people next to `ALLOWED_TELEGRAM_USER_ID`, and
`Config.allowed_user_ids` is the one list the message filter (`bot.py`), the
callback checks (`auth.callback_authorized`), the Mini App dependency
(`webapp_auth.RequireUser`) and the noVNC WebSocket (`vnc.py`) all read.

What this is, and what it is deliberately not:

- **Everyone on the list is inside the *same* trust boundary, not a lesser one.**
  There is no second tenant. Turns run as the same OS user, in the same topics,
  on the same files, with the owner's Claude login, `gh`, git and ssh. Adding
  someone gives them the owner's agent, so the list is for people trusted with
  this machine.
- **Chat scoping is what keeps them in the workspace.** The user-ID gate always
  applies, but with the chat id unset a listed person could also open a private
  chat with the bot. A deployment with more than one user should set
  `ALLOWED_TELEGRAM_CHAT_ID`.
- **The owner stays distinguishable** (`allowed_telegram_user_id`), because some
  things still mean *the owner* specifically: the identity the agent runs as, and
  who needs no sender attribution.
- **Any allowed user may act.** Approval keyboards, question answers, `/cancel`,
  `/delete` and `/schedule` are open to everyone on the list, in either
  direction. Approving means authorizing work that runs with the owner's
  credentials; that is understood, not hidden.
- **A topic is still one session,** so the agent is told who is speaking:
  `message_text.sender_prefix` prepends `[From Bob Lee (@bob)]` to a non-owner's
  prompt (and to a mid-turn follow-up). The owner's prompts are unchanged.
- **A person posting as an anonymous group admin has no real `from_user`** and is
  dropped by the filter, so allowed users must post as themselves.
- **Not per-user tenancy.** The Mini App shows every allowed user the same diffs,
  markdown snapshots and live browser view; `/model` and `/effort` stay per-chat.
  Per-user identity, credentials and data scoping are a separate, much larger
  design (see `docs/multi-player-proposal.md`), which this amendment does not
  implement and does not preclude.

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
- **A topic's workspace context is fixed for its lifetime** (ADR-0012). Switching
  context never rebinds an existing topic; `/context <name>` (from any topic or General)
  **creates a new topic** bound to `<name>` and replies with a one-tap link to
  it, delivered as an inline **"Go to topic"** URL button. This keeps the
  one-context-per-topic invariant, so a topic's session always remembers its own
  history and no sessions are orphaned by rebinding. Duplicate topic names are
  allowed (many topics may share a context).
- **Balam runs in a forum _supergroup_, but topics also work in a private chat.**
  The live deployment is the "workspace" supergroup (ADR-0010), so the chat id is
  a `-100…` supergroup. Telegram's topics-in-private-chats (Bot API 9.3, Dec 2025;
  `createForumTopic` in private chats, 9.4, Feb 2026) — enabled via BotFather
  "Threaded Mode" — also makes `/context` topic creation work in the owner's DM,
  where the chat id is instead the owner's **positive** user id; that path is kept
  as a supported fallback.
- **The one-tap link is environment-dependent** because the Bot API cannot focus
  the client on a chat and has **no documented deep link to a topic in a private
  chat** (thread-targeting `t.me`/`tg://` links are supergroup/channel only). So
  `topics.topic_link` emits: for a `-100…` supergroup (the live deployment), the official
  `t.me/c/<internal>/<thread>` (all clients); for the private-chat fallback, the
  Telegram **Web** address `web.telegram.org/a/#<bot_id>_<thread>` — how the Web
  client itself routes to the topic (verified to open it cold). Web-only, but the
  owner drives Balam over Web, so it is a real one-tap link; on native apps the
  fallback is "pick it from the topic list." Either way the URL is wrapped in the
  inline "Go to topic" button.

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
   single-level _within one supergroup_, but the directory level is carried by a
   **workspace context** (ADR-0012) instead of by the chat hierarchy — Balam
   tags each topic with a context that names its working directory, so a single
   supergroup holds topics across several projects, and **one forum topic per
   session** supplies the second level.

Other factors: the rich Mini App views (git diffs, markdown, live noVNC Chrome,
ADR-0006) have no Discord equivalent (Activities cannot embed an arbitrary
iframe); Telegram topics never auto-archive, whereas Discord threads do (max 7
days) and must be programmatically un-archived; and Telegram is investing
first-party effort in AI-agent primitives (streaming, managed bots, bot-to-bot),
which is precisely this project's domain. Discord's one surviving edge is fully
programmatic provisioning of the directory level.

### Decision

Build Balam on **Telegram**. Realize the directory→session tree as:

- **The directory dimension is a workspace context** (ADR-0012), not a separate
  supergroup per directory. Balam is scoped to a single forum supergroup
  ("workspace", `ALLOWED_TELEGRAM_CHAT_ID`); each topic binds to a context whose
  `directory` is the agent's working dir.
- **One forum topic per OpenCode session** within that supergroup (ADR-0009
  unchanged); `/context <name>` opens a topic for a context.

Stream agent output with `sendMessageDraft`, falling back to throttled
`editMessageText` only where the native method does not fit.

### Consequences

- A Telegram **bot cannot create supergroups** via the Bot API, so the owner
  creates the "workspace" supergroup by hand once and adds the bot as an admin
  with "Manage Topics"; the bot then creates session topics itself
  (`createForumTopic`, via `/context`). This one-time manual setup is acceptable.
- The backend's persisted topic→session row (ADR-0009) also stores each topic's
  **context binding** (ADR-0012), so a session's OpenCode prompts run against the
  right context `directory` (ADR-0001) without a per-supergroup directory map.
- The trust boundary (ADR-0008) now spans multiple supergroups: the user-ID
  allowlist still gates every update, and only owner-created groups are honored.
  `ALLOWED_TELEGRAM_CHAT_ID` scopes the bot to a single such supergroup (ADR-0008).
- **Slash commands must be registered (`setMyCommands`).** In a group, clients
  offer and route slash commands by the bot's registered command list (and send
  the disambiguated `/cmd@<bot>` form), so an unregistered `/context` is never
  surfaced. Balam publishes its commands on startup (`post_init`) for the default,
  all-group-chats, and the specific group scopes; PTB's `CommandHandler` matches
  both `/context` and the `/context@<bot>` form.
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
  `~/projects/zog` and `~/projects/open-shrimp` — as worked examples for the
  hardest parts: animated draft streaming into forum topics (`send_message_draft`),
  GFM→Telegram-MarkdownV2 rendering (`mistune`), and the live noVNC Mini App
  (ADR-0006). In Python these are direct references; in any other language each
  would be a _translation_ (effort + divergence risk).
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

## ADR-0012: Workspace contexts live in a required `config.yaml`

Status: Accepted Date: 2026-06-05

### Context

One Balam bot drives several projects, each with its own working directory and,
optionally, its own model, thinking effort, and tool-permission profile. That is
structured, multi-field, per-workspace configuration: it reads naturally as a
small mapping but awkwardly as flat environment variables. Secrets and infra
connection already live in `.env` (ADR-0008) and should not be mixed with this
workspace map. The shape is adapted from the existing open-shrimp codebase
(ADR-0011 reference reuse).

### Decision

Define named **workspace contexts** in a `config.yaml` at the repo root (path
overridable with `BALAM_CONFIG_PATH`). Each context bundles:

- `directory` — the working dir the agent acts in (the session's root).
- `description` — a human label shown by `/context`.
- `model` (optional) — `provider/model`, split to `{providerID, modelID}` on the
  OpenCode prompt.
- `effort` (optional) — one of low/medium/high/xhigh/max, sent as the prompt
  `variant`.
- `allowed_tools` / `additional_directories` (optional) — the tool-permission
  profile.

A top-level `default_context` names the context an unbound topic (e.g. General)
uses. The file is **required**: Balam fails fast with one clear message if it is
missing or malformed, and will not boot without at least one context. Each topic
binds to exactly one context for its lifetime (ADR-0009); the binding is
persisted in the topic→session row, and `/context <name>` opens a new topic for a
context. Secrets never go in `config.yaml`.

### Consequences

- Two config surfaces, one rule: secrets and infra in `.env`, the workspace map
  in `config.yaml`. Neither leaks into the other.
- The router resolves a topic's directory/model/effort from its bound context and
  passes them to the OpenCode prompt (ADR-0002). An unbound topic, or a binding to
  a context since removed from the file, falls back to `default_context`.
- `allowed_tools` and `additional_directories` are enforced via a **hybrid**
  model (`balam.permissions` + `balam.approvals`). The *opt-in* half is native:
  pre-approved tools are translated into OpenCode `allow` rules (so they run
  without prompting), `additional_directories` become `external_directory` grants,
  and bare `Edit`/`Write` entries are scoped to the workspace dirs. The *boundary*
  half stays local: reads/edits not pre-approved fall through to the approval
  layer, which auto-approves reads inside `directory` and prompts otherwise.
  Enforcement is split deliberately — OpenCode matches permission patterns against
  the *literal* path (verified live against v1.15.13: a symlink inside the
  workspace pointing out is auto-allowed by a native `read <dir>/**` rule), so the
  symlink-safe `os.path.realpath` boundary must live in Balam, not in a native
  rule. Pattern formats are category-specific and also verified live: file-path
  categories (`read`/`edit`) strip the leading slash and glob with `**`;
  `external_directory` keeps the leading slash with a `/*` glob; `bash` patterns
  are command globs.
- Adding a workspace is a config edit, not a code change.

---

## ADR-0013: Expose the Mini App through an authenticated Cloudflare tunnel

Status: Accepted Date: 2026-06-07 Amends: ADR-0007

### Context

ADR-0007 keeps every port on `127.0.0.1` and ADR-0007/0006 deferred any public
URL. But a Telegram Mini App only loads inside Telegram's webview from a **public
HTTPS origin**, and `web_app` inline buttons require an HTTPS URL — so the diff
viewer (ADR-0003) cannot be exercised end-to-end inside Telegram while the backend
is localhost-only. ADR-0008 already anticipated this exact surface: "Validate Mini
App `initData` on every request." The boundary exists; it just needs to start
doing its job.

### Decision

Expose **only** the Balam FastAPI server (the Mini App + its `/api`) to the
internet through a **Cloudflare tunnel**, treating Mini App `initData` (ADR-0008)
as the trust boundary. This narrowly amends ADR-0007's "no public URL" for this
one authenticated surface; everything else stays local.

Conditions (all enforced):

- **One ingress only.** The tunnel maps a single hostname → `http://127.0.0.1:3000`
  (the FastAPI server). OpenCode (`:4096`) and the VNC/noVNC ports (ADR-0006) are
  **never** tunneled.
- **`initData` is always required.** Every `/api` request must carry valid Telegram
  `initData` (ADR-0008); there is no auth bypass. The user-ID allowlist still applies.
- **No Cloudflare Access on the hostname.** Access's interactive login cannot run
  inside Telegram's webview; `initData` is the auth, not a second gate.
- **Reduced surface.** `/docs`, `/redoc`, and the HTTP `/openapi.json` route are not
  served; type generation reads the schema in-process (`scripts/dump_openapi.py`),
  not over HTTP, so nothing is lost.
- **Reachable as the Mini App's `BALAM_PUBLIC_URL`.** The bot builds the `/diff`
  `web_app` button from it; with it unset the bot falls back to the local URL.

### Consequences

- The VM opens **no inbound port**: `cloudflared` dials Cloudflare outbound and
  proxies to the localhost socket, so ADR-0007's "don't expose ports / no firewall
  change" stays literally true — what changes is that one authenticated surface is
  reachable *through* Cloudflare.
- The trust boundary is unchanged in kind (ADR-0008): a leaked bot token still
  cannot forge `initData` past the user-ID check; the hostname being known is fine
  because obscurity is not the control.
- Services run under **systemd** (ADR-0001 already runs OpenCode this way): an
  OpenCode unit, the Balam bot+server unit, and the tunnel unit. A quick
  `trycloudflare` tunnel yields an ephemeral hostname, so a small refresh step
  rewrites `BALAM_PUBLIC_URL` and restarts the bot on each tunnel start; a named
  tunnel (stable hostname) removes that step.
- If this ever broadens beyond the single owner, revisit alongside ADR-0007/0008
  (the deployment stops being "local single-user").

---

## ADR-0014: Pluggable agent backend — OpenCode or the Claude Agent SDK

Status: Accepted Date: 2026-06-16

### Context

ADR-0001/0002/0011 made Balam a thin client of a long-lived OpenCode server over
HTTP. That contract leaked into the codebase: the streamer parsed OpenCode SSE
dicts inline, `permissions.py` emitted OpenCode's ruleset wire format, and the
router called the OpenCode client directly. Running Balam on the **Python Claude
Agent SDK** instead (e.g. to use Claude models directly rather than OpenCode's
configured provider) had no seam to slot into.

### Decision

Introduce an `AgentBackend` protocol and a normalized internal event vocabulary
(`balam.agent.events`, adapted from OpenCode's `LLMEvent`), and select the backend
with `AGENT_BACKEND` (`opencode` default, or `claude_sdk`). Two implementations:

- **`OpenCodeBackend`** wraps the existing HTTP/SSE client and translates its
  stream into `AgentEvent`s; session config (ruleset + MCP) is applied eagerly by
  the router at session creation, as before.
- **`ClaudeSdkBackend`** drives the SDK with a fresh, stateless `query(resume=…)`
  per turn — which is what lets model, reasoning effort, and `permission_mode`
  vary per turn (a persistent `ClaudeSDKClient` cannot change effort mid-session).
  Sessions are minted lazily and resume from the SDK's on-disk transcripts.

The streamer, approval keyboard, and question flow consume only `AgentEvent`s and
answer via the backend's reply methods, so they are backend-agnostic.

### Consequences

- **Claude-only in SDK mode.** The SDK runs Claude models, so `AGENT_BACKEND=claude_sdk`
  necessarily switches the model family to Claude; the `provider` half of a
  context's `model` is ignored (a bare Claude id/alias is accepted).
- **Permissions, one intent, two enforcement points.** `build_ruleset` is shipped
  to the OpenCode server; the same ruleset is evaluated **in process** by the SDK
  backend's `can_use_tool` via the ported `evaluate()` (last-match-wins). Tools the
  user pre-approved auto-allow; everything else falls through to Balam's
  symlink-safe directory boundary (`approvals.decide`) and the human keyboard —
  identical policy on both backends.
- **send_file & MCP.** Under OpenCode, `send_file` is a per-topic remote MCP server
  (scope-token URL); under the SDK it is an in-process SDK tool (closure over the
  topic) and context MCP servers are coerced to the SDK's `mcp_servers` shape — no
  HTTP server or token needed.
- **Plan mode.** *Superseded — `/plan` was removed (ADR-0015).* It used
  OpenCode's plan agent and the SDK's `permission_mode="plan"`, surfacing
  `ExitPlanMode` as a Yes/No plan-approval question. Native natural-language
  planning ("plan feature X" in a normal turn) is unaffected and remains
  available on the SDK.
- **Coarser live reasoning on the SDK.** Text streams as deltas, but extended
  thinking is not streamed token-by-token, so the "thinking…" narration is less
  granular than OpenCode's.
- **Different process model.** No agent daemon: the SDK spawns the bundled `claude`
  CLI per turn (auth via `ANTHROPIC_API_KEY` or an already-authenticated CLI), so
  the OpenCode systemd unit is not needed in SDK mode.

---

## ADR-0015: Hold an SDK turn open while its background work runs

Status: Accepted Date: 2026-07-24

### Context

Under ADR-0014 the SDK backend runs one `claude` process per turn and closes its
stdin at the `ResultMessage`. The CLI reads that as wind-down and kills every
background task it owns.

So an agent that starts background subagents, says "I'll report findings as they
land", and ends its turn has its work killed seconds later. Observed live: four
investigation subagents launched, the reply sent at 11:17:01, all four killed at
11:17:04 mid-tool-call with nothing produced and nothing said in the topic.

Two mitigations already existed and neither worked. The system-prompt note only
described the Bash tool's `run_in_background`, while the Agent tool defaults to
backgrounding and has no `setsid` equivalent. The turn-end "still running" notice
read `background_tasks_changed`, which the CLI publishes but filters out of the
SDK transport — dead code that could never fire.

### Decision

**Keep the turn open while background work is live**, instead of ending it at the
first result.

- The live set is rebuilt from the per-task lifecycle messages (`task_started`,
  `task_updated`, `task_notification`), which do reach an SDK client. A terminal
  state may arrive on either of the latter two, so both clear it.
- At a result with work still live, the backend emits `TurnStepFinished` and
  keeps consuming. The streamer commits the answer so far, so anything that
  follows lands as its own message.
- When a background task finishes, the CLI wakes the model by itself and the
  report arrives as an ordinary assistant turn. Delivery is not built here; it
  falls out of not hanging up.
- `_BACKGROUND_HOLD_S` (30 min) caps the hold. At the deadline the turn ends and
  the work stops with it — reported by the turn-end notice, now its only job.

Measured on this VM against CLI 2.1.218: with the client held open past the
result a background task keeps running and the process stays alive; disconnecting
kills it immediately. That is the whole mechanism.

### Consequences

- **Persistence is demand-driven.** A topic holds a CLI process only while it has
  background work, so the ordinary turn costs exactly what it did before. This is
  why there is no session pool: one process is ~200-500 MB here and the store has
  34 topics, so keeping one alive per topic (open-shrimp's model, a flat 30-minute
  idle timeout) would not fit in 23 GB.
- **Model and effort had to become chat-global.** A held turn cannot absorb a
  mid-flight change — the SDK can swap a model on a live client but effort is
  fixed at connect. `/model` and `/effort` are now set from General and apply
  everywhere.
- **Plan mode was removed** rather than carried through the new lifecycle; the
  CLI's native natural-language planning covers it.
- **`_is_foreign_result` still stands.** Resume still happens on a cold topic, and
  the CLI's orphan scan still injects its own prompt there.
- **A cancelled or failed turn kills background work**, as before: both close the
  stream. That is the intended reading of `/cancel`.

---

## ADR-0016: Scheduled prompts are stored, not configured

Status: Accepted Date: 2026-07-31

### Context

One prompt already runs on a timer: the Chaska daily brief, from a cron script
outside this repo. It reaches into Balam's `.env` for the bot token, hand-writes
a `topic_sessions` row to bind a topic, re-implements `topics.open_context_topic`
badly (no rollback when the bind fails) and `markdown.py` worse (a `sed` turning
`**x**` into `<b>x</b>`). It works, and it pins Balam's schema from a file no
test covers.

The machinery to do this properly now exists. `topics.open_context_topic` creates
the topic, binds it, and rolls the topic back if the bind fails. `turns.start_turn`
runs a
turn from a plain `(chat_id, thread_id, TurnJob)` — it takes no `Message`. So a
schedule is `/new <context> <prompt>` on a timer, and the work is mostly wiring.

Two things are not wiring. A turn that starts at 07:30 has **nobody watching**,
and neither the approval keyboard nor the question keyboard has a timeout. And
PTB's `JobQueue` is **in memory**, so unlike cron it does not survive a restart.

### Decision

**Schedules are user data in SQLite, driven by `/schedule`** — not `config.yaml`
entries. Contexts stay in `config.yaml` (ADR-0012) because a directory and a tool
policy are infrastructure. A schedule is created, listed and cancelled from the
phone and edited far more often; putting it in `config.yaml` would mean an SSH
session to stop a 7am message.

- A schedule is a saved `(when, context, prompt)` triple. When it fires it opens
  a **fresh forum topic** bound to that context and runs the prompt there. One
  topic per fire keeps each run's history separate — ADR-0009's reasoning
  applied to time.
- `when` covers `daily HH:MM`, `weekdays HH:MM`, and `<weekday> HH:MM`. All three
  are exactly what PTB's `run_daily` takes, so v1 has no cron parser and no
  APScheduler surface of its own. Raw cron expressions are a later extension via
  `run_custom`.
- Times resolve against a required-with-default **`BALAM_TIMEZONE`**, validated
  at boot. The VM runs UTC and the owner does not; "07:30" meaning 07:30 UTC
  would be a bug generator.

**An unattended turn denies anything past an in-workspace read.** `Verdict` gains
`DENY`, and `decide()` takes `unattended`. Reads inside the workspace still
auto-allow; everything else is refused with a reason the agent can reason about,
and the refusal is posted in the topic so the owner can read what it wanted and
re-run it by hand. Questions are rejected the same way. `unattended` is a
property of the **turn**, not the topic — the owner's reply in the morning's
topic is attended and gets the normal keyboard.

**Missed runs catch up within a bounded window.** At boot, after re-registering
every timer, each enabled schedule's most recent due time is computed; if it is
past, later than `last_run_at`, and within six hours, it runs now and says in the
topic that it is late. The window is what stops a VM that was off for a week from
producing seven topics at boot. `last_run_at` is stamped when the run **starts**,
not when its turn ends, so a crash mid-turn cannot re-fire the whole thing.

### Consequences

- **A second, timer-driven entry point to the agent now exists.** It is
  *internal* — no new external surface, and ADR-0008's Telegram gate still
  governs everything a human sends — but it is the first path that starts a turn
  with no human in the loop. That is exactly what the unattended policy above
  constrains.
- **cron survived Balam being down; this does not.** Catch-up narrows the gap to
  "Balam was down across the due time *and* stayed down more than six hours".
  That is the accepted cost of deleting the cron script.
- **`bot.py` grew again** — since resolved. The `/schedule` handlers first landed
  in `bot.py`, on top of the file that was then the largest in the repo. The
  store, the parser and the fire path went into a new `schedules.py`; the command
  surface has since moved to `commands/schedule.py` in the `commands/` split, and
  `bot.py` is now the plain-message path plus the registrar.
- **`/delete` and `/schedule cancel` share one picker.** `PendingDeletions`
  became `PendingPicks`, a paged multi-select over `(id, label)` pairs, rather
  than growing a second picker idiom.

---

## ADR-0017: A held turn stays answerable, and holds are bounded by count

Status: Accepted Date: 2026-08-17 Amends: ADR-0015

### Context

ADR-0015 holds a turn open while its background work runs. A live session
(`9999a2fc-…`, 2026-08-16) exposed three defects in how that hold behaved.

**Messages stalled behind a hold.** `FollowUpChannel.take()` ran in exactly one
place: at a `ResultMessage` boundary. While held, the model has already answered
and no `ResultMessage` is coming, so a message offered into the live turn sat in
the channel until a background task happened to finish. That session held for
14m04s with nothing forwarded. Interactive Claude Code has no such gate — a
prompt typed while background work runs is accepted straight away — so this read
as Balam-specific breakage, and it was.

**An accepted message could be lost.** `offer()` returns `True` while the channel
is open, so the bot reacts 👀 and does *not* queue the message. `close()` only
flipped a flag. A message accepted during a hold that the turn never reached was
dropped silently — on the hold timeout, on the idle-guard timeout, and on turn
error.

**The cap measured the wrong thing.** `_BACKGROUND_HOLD_S` was armed at the first
hold and never disarmed until teardown, so it counted foreground work too. In
that session it was armed at 14:37:59 while the model was working and fired at
15:07:59 — killing a CI watcher started at 14:53:40 after 14 of its 30 minutes.
The agent had been told "background work is capped at 30 minutes", which was not
the rule the runtime implemented.

Behind all three: the cap existed to bound **memory** (a CLI process is
~200-500 MB, the VM has 23 GB), but time is a poor proxy for memory. It cut short
work that was still wanted while doing nothing about several topics holding
processes at once.

### Decision

**1. A hold is interruptible.** While held, the backend also waits on the
follow-up channel (`FollowUpChannel.wait()`) and forwards a message the moment it
lands, instead of waiting for a step boundary that is not coming. `take()` →
`put_nowait()` is one synchronous block, so cancelling that pump when the model
wakes can never strand a message it had already taken. The channel keeps its
single-consumer rule: the pump runs only while held, and ends before the
`ResultMessage` branch can run again.

**2. The hold clock measures waiting, not the turn.** It is disarmed wherever the
model proves it is producing (`note_model_active()`, the same two points that
decide `_is_foreign_result`) and at a folded-in follow-up. Each stretch of actual
waiting gets the full budget; foreground work is never charged to it.

**3. Memory is bounded by count, not by time.** `_MAX_HELD_TURNS` (3) caps how
many topics may wait at once; arming one past the cap ends the longest-idle hold
through the same teardown a timeout uses, so its topic gets the usual turn-end
notice naming what stopped. With the real resource bounded directly,
`_BACKGROUND_HOLD_S` becomes 4 hours — long enough for the CI watch or the slow
build that is the whole point of waiting.

**4. Accepted means recoverable.** `drain()` hands back anything still pending
when a turn ends, and `start_turn` re-resolves each into a job at the *head* of
the topic queue, so it runs next and in the order it was sent. Not after
`/cancel`: the owner stopped that topic on purpose. Each is resolved fresh rather
than copied, so a session minted during the finished turn is the one it runs
against.

**5. `/tasks` reports what is running.** The terminal shows a running background
task in the session; a topic had nowhere to put that, so the only report was the
turn-end notice naming what got *stopped*. The backend publishes each topic's
live set — the same data the hold cap reads — and `/tasks` reads it without
interrupting the turn. Empty on OpenCode, which has no background-task concept.

**Rejected: decoupling the CLI process from the turn.** The bigger fix is a
per-topic session that outlives its turn, so the turn can end while tasks keep
running. It would need a new out-of-turn delivery path to open a Telegram message
for an unsolicited task report, and a new meaning for `/cancel`. It buys a topic
that *looks* idle while it waits; it does not buy anything the four changes above
do not already deliver, since a held turn now answers messages as promptly as an
idle one. Not worth the rewrite until something needs the process to survive a
turn ending for another reason.

### Consequences

- **The system prompt now states the rule the runtime implements** — capped
  while idle, a few topics at a time, `setsid` for anything that must outlive the
  conversation. The old text promised a flat 30 minutes that was never what
  happened.
- **A topic can hold a CLI process for hours.** That is the point, and
  `_MAX_HELD_TURNS` is what makes it affordable. Both constants are module-level
  in `claude_sdk_backend.py`; they are the two dials for this trade.
- **Eviction is visible, not silent.** Ending the longest-idle hold reuses the
  turn-end notice, so that topic is told which tasks stopped rather than simply
  going quiet.
- **`ADR-0015`'s "delivery falls out of not hanging up" still holds.** Nothing
  here changes how a finished task's report reaches the topic; it changes how
  long the turn is willing to wait, and whether it can still hear the owner while
  it does.
- **A held turn is still one turn.** `/cancel` still ends it and its background
  work, and the streamer still commits each step's answer as its own message.

---

## Summary

| ADR  | Decision                                                                    | Core reason                                                     |
| ---- | --------------------------------------------------------------------------- | --------------------------------------------------------------- |
| 0001 | OpenCode as headless server (systemd), Balam as client                      | Keeps sessions/tools warm; bot stays small                      |
| 0002 | HTTP API is source of truth; thin `httpx` client, no SDK                    | Contract-first; language never limits capability                |
| 0003 | Three layers; frontend is fixed TypeScript                                  | Clear responsibilities; Mini App must be web                    |
| 0005 | Browser-use as an OpenCode skill                                            | Reuse skill; backend language irrelevant to it                  |
| 0006 | Live Chrome via noVNC (amended: RFB client in-page, backend WS↔TCP bridge)  | Real-time view from a standard stack; least UI code             |
| 0007 | Local single-user on the VM                                                 | Full local access; minimal security surface                     |
| 0008 | Telegram entry point is the trust boundary; allowlist one user ID           | The bot is internet-facing even when ports are local            |
| 0009 | One Telegram forum topic = one OpenCode session                             | Native parallel task threads, no custom UI                      |
| 0010 | Telegram over Discord; one supergroup, context-per-topic, session-per-topic | Native streaming + Mini App + no archiving; two-level tree fits |
| 0011 | Backend in Python (FastAPI + PTB), OpenCode over HTTP                       | Reference reuse (zog/open-shrimp); HTTP is the contract (0002)  |
| 0012 | Workspace contexts in a required `config.yaml`                              | Per-project dir/model/effort/tools; structured config, not env  |
| 0013 | Expose only the Mini App via an authenticated Cloudflare tunnel (amends 0007) | Mini App needs a public HTTPS origin; `initData` is the boundary |
| 0014 | Pluggable agent backend (OpenCode or the Claude Agent SDK) via `AGENT_BACKEND` | One seam, normalized events; SDK = Claude models + per-turn config |
| 0015 | Hold an SDK turn open while its background work runs (amends 0014)          | Closing stdin kills background tasks; holding also delivers their report |
| 0016 | Scheduled prompts are stored in SQLite (`/schedule`), not `config.yaml`     | Schedules are user data; unattended turns deny, missed runs catch up |
| 0017 | A held turn stays answerable; holds bounded by count, not time (amends 0015) | Messages must not stall behind background work; memory is the real bound |
