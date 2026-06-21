# Feedback log

A running log of feedback used to improve the bot. Each entry is a discrete,
actionable item: what's wrong or missing, why it matters, and enough pointers to
pick it up later without re-deriving the context. Newest entries on top.

Status legend: 🔴 open · 🟡 in progress · 🟢 done

---

## 2026-06-21 — `/delete` topic picker caps at 90 with no pagination

**Status:** 🟢 done — fixed via real pagination (no cap). `list_topics` now orders
newest-first (`store.py`, `ORDER BY created_at DESC`); `PendingDeletions`
(`approvals.py`) snapshots the **full** topic list and pages it `PAGE_SIZE=8` at a
time (`page_info`/`set_page`), tracking selection by `thread_id` across the whole
snapshot. The picker grew a `◀ Prev / Page k/n / Next ▶` nav row
(`delp:<token>:<page>`, handled by `_handle_delete_page_callback`) and a selected
count on the confirm button; selections persist across pages and all delete
together on confirm. The misleading "run /delete again for the rest" message is
gone. `_DELETE_PICKER_LIMIT` removed.

**Area:** Bot commands · `/delete` topic picker (`apps/backend/src/balam/bot.py`)

**Summary.** The `/delete` picker caps at `_DELETE_PICKER_LIMIT = 90` topics and,
when there are more, tells the user *"Showing the first N of M topics — run
/delete again for the rest."* But that promise can't be kept: `_handle_delete`
always slices `topics[:_DELETE_PICKER_LIMIT]` with **no offset/cursor**, and
`SessionStore.list_topics` orders by `created_at` **ascending**. So re-running
`/delete` returns the *identical* first 90 (oldest) topics, and everything beyond
the cap is permanently unreachable from the picker.

**Impact.** With >90 topics the **newest** topics can never be selected for
deletion — including a topic you just made. Verified live 2026-06-20 in the
workspace supergroup (98–99 topics): a freshly created `/new` topic (`#2137`)
never appeared in the picker and there was no way to reach it. The cap message
actively misleads.

**Why it matters.** The whole point of `/delete` is letting the owner clean up
topics; the topics most likely to need cleanup (recent test/throwaway ones) are
exactly the ones the cap+ordering hide. Everything else about `/delete` works
end-to-end (picker render, General excluded, toggle, cancel, confirm →
`deleteForumTopic` + `purge()` across all four per-topic tables).

**Possible direction (not yet decided).**
- Order `list_topics` **newest-first** so the cap at least surfaces the topics
  most likely to be deleted (simplest fix; drops the "rest" promise).
- Or add real pagination: a stored offset/cursor per picker token so "run
  /delete again" (or a Next/Prev row) advances through batches.
- Either way, only claim "run /delete again for the rest" if a subsequent run
  actually shows different topics.

**Pointers.**
- `apps/backend/src/balam/bot.py` — `_handle_delete` (~L1118), `_DELETE_PICKER_LIMIT`
  (L1067), the `topics[:_DELETE_PICKER_LIMIT]` slice (~L1128) and the
  "run /delete again for the rest" message (~L1138).
- `apps/backend/src/balam/store.py` — `SessionStore.list_topics` (`ORDER BY created_at`).
- Picker state lives in `PendingDeletions` (`apps/backend/src/balam/approvals.py`) —
  a cursor/offset would attach here per token.

---

## 2026-06-20 — `AskUserQuestion` rich-content fields lost in the question abstraction

**Status:** 🔴 open

**Area:** Agent backends · structured questions (`QuestionAsked` event, streamer rendering, answer channel)

**Summary.** Balam's shared question abstraction is a *labels-in / labels-out*
model. It cleanly carries `question`, `header`, `options[].label`,
`options[].description`, and `multiSelect`, but drops the two *rich-content*
dimensions of Claude Code's `AskUserQuestion` tool schema. This surfaced while
wiring `AskUserQuestion` into the Claude Agent SDK backend
(`apps/backend/src/balam/agent/claude_sdk_backend.py`, `ask_user_question`).

**The gaps:**

1. **`options[].preview` (outbound, higher impact).** Per-option preview content
   — code snippets, mockups, HTML fragments the model generates so the user can
   visually compare options. The abstraction's option dict only models
   `label` + `description`:
   - `_format_question` (`streamer.py`) renders only label + description.
   - `_question_keyboard` (`streamer.py`) renders label only.
   - `ask_user_question` (SDK backend) explicitly drops `preview` when mapping
     options.
   Also structurally hard on the current surface: Telegram inline keyboards have
   no per-option "focus" state to render a preview against. Would need a
   different UX (e.g. a preview expands on selection, or a separate message).
   Claude/Fable models actively generate `preview`, so today it vanishes
   silently.

2. **`annotations` (inbound, lower impact).** `record(questionText → {preview?,
   notes?})` — structured extras the user attaches to a selection that are fed
   *back to the model*. The return channel is
   `reply_question(request_id, answers: list[list[str]])` — selected labels only,
   one list per question. There is no slot to carry the user's free-text `notes`
   or which `preview` they picked. Consequence: the "Type your own answer"
   (`custom=True`) path produces free text, but it rides back as the *answer
   label string*, not as a structured `notes` annotation — the model can't tell
   "chose X" from "chose X and added a note."

3. **`metadata.source` (negligible).** Analytics-only tag (e.g. `"remember"`).
   Never user-facing; dropped silently. Listed for completeness.

**Why it matters.** `AskUserQuestion` is how the agent runs interactive
multiple-choice decisions with the user. Losing `preview` removes the
visual-comparison affordance the model expects to offer; losing `annotations`
flattens nuanced answers into bare labels. Neither OpenCode's `question` tool nor
the Telegram keyboard exercises these fields, which is why the shared shape never
modeled them — but the SDK backend now routes a tool that does.

**Possible direction (not yet decided).**
- Extend the option dict in `QuestionAsked` to carry `preview`, and design a
  Telegram-appropriate way to surface it (expand-on-select, or a follow-up
  message per focused option).
- Widen the answer channel beyond `list[list[str]]` to an optional structured
  form so `notes`/selected `preview` can flow back as `annotations`. Touches
  `reply_question` across both backends and `pending_questions` in the streamer.
- Keep `metadata.source` as a pass-through only if/when analytics need it.

**Pointers.**
- Schema source of truth: `references/free-code/src/tools/AskUserQuestionTool/AskUserQuestionTool.tsx`
  (input/output Zod schemas; `answers` injected via `updated_input`).
- Abstraction: `apps/backend/src/balam/agent/events.py` (`QuestionAsked`),
  `apps/backend/src/balam/agent/backend.py` (`reply_question` signature),
  `apps/backend/src/balam/streamer.py` (`request_questions`, `_format_question`,
  `_question_keyboard`).
- SDK mapping: `apps/backend/src/balam/agent/claude_sdk_backend.py`
  (`ask_user_question`).
