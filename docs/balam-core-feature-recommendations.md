# Balam — Core Feature Recommendations

Prioritized tiers for what to build next, drawn from the
[Telegram coding-bots feature comparison](./telegram-coding-bots-comparison.md).
Principle: finish the daily-driver **chat loop** before the Mini App; prefer
features proven in *both* reference apps; reuse their implementations (ADR-0011).

Already shipped: bot↔agent round-trip over forum topics, workspace contexts +
`/context`, `send_message_draft` streaming, GFM→MarkdownV2, SQLite store, allowlist.

## Tier 1 — Build first (the chat loop)

Small additions to existing modules; all overlap in both reference apps.

- **Session commands `/new`, `/status`, `/cancel`** — start a fresh session, show
  context/model/session, abort the in-flight turn. (Both apps call this `/clear`;
  Balam uses `/new` since the topic history stays.)
- **Tool-call visibility** — surface which tool ran + truncated output in the stream.
- **Interactive tool approval** — gate Write/Edit/Bash behind an inline keyboard;
  auto-approve reads inside the context dir. Ships with the directory-boundary check
  (one mechanism in OpenCode). `allowed_tools` enforcement has since shipped too, as
  the hybrid model in ADR-0012 (`balam.permissions` + `balam.approvals`).
- **Inbound file attachments** — accept images/PDFs/text, saved under
  `/tmp/balam_uploads/<thread_id>/`, referenced in the prompt.

→ See [the Tier 1 implementation plan](./balam-tier1-implementation-plan.md).

## Tier 2 — Next (first Mini App slice)

- **Mini App foundation + `initData` auth** — FastAPI server, serve the React build,
  validate Telegram HMAC (ADR-0003/0008).
- **Git diff viewer** — hunk-level diff of the working dir (read-only first); the
  flagship Mini App view in both apps.

Dropped: `/resume` + session titles — Balam binds one session per forum topic, so
the Telegram topic list *is* the session list; navigating topics replaces reopening
sessions by hand.

## Tier 3 — Defer (costly or blocked)

- **noVNC live Chrome** (ADR-0006) — blocked on VM X11/VNC infra, not code.
- **Scheduled tasks** — whole workflow; not core to interactive use yet.

Shipped since this doc was written: `allowed_tools` enforcement (the hybrid
native-ruleset + local-boundary model; see ADR-0012 and `balam.permissions`);
the **markdown viewer** with open-shrimp's two triggers — an agent-facing
`send_file` MCP tool whose `.md` sends carry a "📖 Preview" button, and a
"📋 View plan" button on OpenCode's native `plan_exit` approval question
(`balam.agent_tools` + `balam.content_store`; review-comments mode in the
viewer remains a deferred follow-up).

Out of scope per ADRs (open-shrimp breadth, not Balam's single-user local design):
sandboxing, computer-use GUI tools, voice/STT, multi-instance packaging, macOS app.

## Sequence

```
Tier 1:  /new,/status,/cancel  →  tool-call display  →  approvals  →  attachments
Tier 2:  FastAPI + initData     →  git diff viewer    →  markdown viewer
Tier 3:  noVNC  ·  scheduling
```
