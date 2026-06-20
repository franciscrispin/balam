# Feedback log

A running log of feedback used to improve the bot. Each entry is a discrete,
actionable item: what's wrong or missing, why it matters, and enough pointers to
pick it up later without re-deriving the context. Newest entries on top.

Status legend: 🔴 open · 🟡 in progress · 🟢 done

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
