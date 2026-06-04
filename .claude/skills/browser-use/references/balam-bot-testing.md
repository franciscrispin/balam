# Balam bot test — end-to-end playbook

How to autonomously test the Balam Telegram bot through a real Telegram Web
client. Read this when the user asks to test/verify/drive the bot. It assumes
the three prerequisites in `SKILL.md` are met: the **OpenCode server** is up,
`uv run balam` shows the bot **polling**, and the headed browser is open on the
logged-in **`telegram`** profile as the **owner** account.

The mental model (matches the Python source in `apps/backend/src/balam/`,
ADR-0011):

```
Telegram Web ──message──▶ Telegram ──long poll──▶ bot.py
   │                                                 │ filters.User owner gate (ADR-0008)
   │                                                 ▼ router.resolve() → OpenCode session (ADR-0009)
   │                                                 ▼ stream_reply(): prompt + subscribe to SSE
   ◀── send_message_draft (animated preview, ~0.5s)─┤   accumulate assistant text
   ◀── send_message (MarkdownV2, ≤4096-char chunks)─┘   on session.idle / session.error
```

So a successful test shows, in order: a **typing** action, a **growing animated
draft**, then a **final message**. The backend log mirrors each step.

---

## 1. Confirm the system is up (don't skip this)

Three quick checks — most "the bot is broken" reports are one of these being
down:

```sh
# OpenCode server reachable + auth working (use the .env password; ask the user)
curl -s -o /dev/null -w 'opencode /doc → %{http_code}\n' -u opencode:<password> http://127.0.0.1:4096/doc   # 200

# Bot polling — look back in the `uv run balam` log for, in order:
#   [balam] INFO OpenCode is ready.
#   [telegram.ext.Application] INFO Application started

# Browser is on Telegram Web, logged in
playwright-cli eval '() => location.href' --raw     # should be https://web.telegram.org/a/...
```

If `/doc` is not `200`, start `opencode serve` (see SKILL.md prereqs). If the
bot never logged "online", it is probably still `waiting for OpenCode …` — fix
OpenCode first, the backend waits for it before connecting to Telegram.

## 2. Open the bot's chat

Balam answers in whatever chat the owner messages it from — a **direct chat**
with the bot, or a **supergroup with forum topics** (ADR-0009 maps each topic to
its own OpenCode session). To find it:

- `playwright-cli snapshot` the chat list and look for the bot's name / the
  supergroup Balam is wired to.
- Click its entry. Telegram routes via the SPA; **confirm the URL** afterwards —
  `playwright-cli eval '() => location.hash' --raw` should be
  `#<chatId>` (or `#<chatId>_<msgId>`). Don't `goto` the `#<chatId>` URL
  directly; the SPA often resets to `/a/` (see `telegram-web.md`).
- If it is a forum supergroup, open (or create) a **topic** first and send your
  message inside it — the bot threads its reply with `message_thread_id`. The
  "General" topic is fine for a smoke test.

Screenshot the open, empty-ish chat so you have a "before" frame.

## 3. Send a test prompt

Pick a prompt that forces the agent to actually *do* something in the workdir,
so the answer is verifiable rather than chit-chat:

- `what files are in this repo?` — agent lists the dir; you can eyeball it.
- `run pwd and show the output` — should print `BALAM_WORKDIR` (the repo root).
- `read CLAUDE.md and summarize what Balam is in one sentence.`

Type into the composer and send:

```sh
playwright-cli fill 'div.input-message-input' 'what files are in this repo?'
playwright-cli screenshot --filename sent.png
playwright-cli press Enter
```

(The WebA composer is a `contenteditable` `div.input-message-input`. If the
selector misses, `snapshot` and grab the `ref=eNN` of the "Message" textbox.)

## 4. Watch the round-trip

This is the actual assertion. Look → wait → look, polling for the reply rather
than sleeping a fixed time (agent latency varies a lot with the prompt):

1. **Typing action.** Shortly after sending, Telegram shows the bot "typing"
   (`stream_reply` calls `send_chat_action("typing")` immediately). Screenshot if
   you catch it — it is the first sign the backend received the message.
2. **Animated draft.** Within ~0.5s the bot starts streaming a **draft** that
   grows as the agent generates (`send_message_draft`, reusing one `draft_id` so
   Telegram animates it). Screenshot mid-stream — this proves the SSE streaming
   path works, the riskiest part.
   ```sh
   playwright-cli screenshot --filename streaming.png
   ```
3. **Final message.** On `session.idle` the draft is replaced by a real,
   persistent message (split into multiple bubbles if > 4096 chars). Poll until
   a new assistant bubble exists, then screenshot:
   ```sh
   # crude poll: wait until the last message text stops being empty / the bubble count grows
   for _ in $(seq 1 60); do
     n="$(playwright-cli eval '() => document.querySelectorAll(".message, .bubble").length' --raw 2>/dev/null)"
     # break when you see a new incoming bubble appear (compare to the count before sending)
     sleep 0.5
   done
   playwright-cli screenshot --filename reply.png
   ```
   (Prefer reading the actual last-message text via `eval` / `snapshot` over
   counting nodes — Telegram's class names shift between WebA versions, so
   confirm the selector against a fresh `snapshot` first.)

## 5. Verify

The test **passes** when:

- a final assistant message appeared in the **same topic** you sent from, and
- its content is a plausible agent answer to your prompt (e.g. an actual file
  listing), **not** a `⚠️ …` line. A message starting with `⚠️` is the bot's
  error path (`bot.py` except / `session.error`) — read the text, it carries the
  OpenCode error; then check the `uv run balam` log for the traceback.

The reply is rendered as **MarkdownV2** (GFM via `markdown.py`, ADR-0010), so
expect bold/code/lists to be formatted. If a message arrives as raw,
unformatted text it means the MarkdownV2 send failed and `streamer.py` fell back
to plain text — note it (the formatting path has a bug), but it is not a
round-trip failure.

Cross-check the backend log: each message should show handling activity, and on
failure `[balam.bot] ERROR failed to handle message` prints there (with a
traceback) — detail the UI hides.

## 6. Report

Tell the user: the prompt you sent, the chat/topic, whether the typing →
draft → final sequence happened, the final answer (quoted/screenshot), rough
latency, and anything off (a draft that never finalized, truncation at the 4096
split, a console/network error, slow first token). Link `sent.png`,
`streaming.png`, `reply.png`.

---

## Distinguishing real failures from expected behavior

| Symptom                                   | Most likely cause                                                              |
| ----------------------------------------- | ------------------------------------------------------------------------------ |
| **No reply at all, no typing**            | You are **not the owner** account (ADR-0008 drops non-owner updates silently), or `uv run balam` is not running / not polling. Check the log shows the message arrived. |
| Typing, then a `⚠️` message               | OpenCode errored — server down, bad auth, or the agent hit an error. Read the `⚠️` text + backend log. |
| `waiting for OpenCode …`, bot never polls | OpenCode server not up or wrong password. `curl /doc` should be `200` with auth. |
| Draft animates but never finalizes        | A real bug worth flagging — `session.idle` not received, or `finalize()`/`send_message` failing. Capture the backend log. |
| Reply arrives as raw/unformatted text     | MarkdownV2 send failed → plain-text fallback in `streamer.py`. Worth flagging, but not a round-trip failure. |
| Reply split into several bubbles          | **Expected** for long answers (≤4096-char chunks in `streamer.py`/`markdown.py`), not a bug. |
| Reply in the wrong topic                  | A routing bug (`message_thread_id` / ADR-0009) — flag it with the topic IDs. |

## What this does *not* cover yet

The Mini App (git diff / markdown viewers) and the live-Chrome noVNC iframe
(ADR-0003/0006) are **not implemented yet** per `CLAUDE.md`. This playbook tests
the **bot round-trip** only. When those land, add their checks here (e.g. open
the Mini App from a Telegram button, verify the diff renders) rather than
expanding `SKILL.md`.
