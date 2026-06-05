# Balam — Core Feature Recommendations

Prioritized tiers for what to build next, drawn from the
[zog vs. open-shrimp feature comparison](./zog-vs-open-shrimp-features.md).
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
  (one mechanism in OpenCode); the heavier `allowed_tools` hard-enforcement stays
  deferred (ADR-0012).
- **Inbound file attachments** — accept images/PDFs/text, saved under
  `/tmp/balam_uploads/<thread_id>/`, referenced in the prompt.

→ See [the Tier 1 implementation plan](./balam-tier1-implementation-plan.md).

## Tier 2 — Next (first Mini App slice)

- **Mini App foundation + `initData` auth** — FastAPI server, serve the React build,
  validate Telegram HMAC (ADR-0003/0008).
- **Git diff viewer** — hunk-level diff of the working dir (read-only first); the
  flagship Mini App view in both apps.
- **`/resume` + session titles** — list recent sessions and reopen one.

## Tier 3 — Defer (costly or blocked)

- **noVNC live Chrome** (ADR-0006) — blocked on VM X11/VNC infra, not code.
- **Markdown/document viewer** — complements the diff viewer.
- **Scheduled tasks** — whole workflow; not core to interactive use yet.
- **`allowed_tools` hard-enforcement** — the heavy half of ADR-0012; human approval
  (Tier 1) is the backstop until then.

Out of scope per ADRs (open-shrimp breadth, not Balam's single-user local design):
sandboxing, computer-use GUI tools, voice/STT, multi-instance packaging, macOS app.

## Sequence

```
Tier 1:  /new,/status,/cancel  →  tool-call display  →  approvals  →  attachments
Tier 2:  FastAPI + initData     →  git diff viewer    →  /resume + titles
Tier 3:  noVNC  ·  markdown viewer  ·  scheduling  ·  allowed_tools enforcement
```
