"""Stream an OpenCode reply into a Telegram topic using native message drafts.

Telegram's ``sendMessageDraft`` streams partial text without flicker (ADR-0010).
But it is **private-chat only** — its documented ``chat_id`` is "the target private
chat", and a supergroup/topic is rejected with ``Textdraft_peer_invalid``. So in the
"workspace" forum supergroup (the live deployment) we fall back to **live-edit
streaming**: send one real message and edit it in place as the text grows — the
throttled ``editMessageText`` path ADR-0010 specifies. Approach follows zog
(``src/zog/stream.py``) and open-shrimp (``stream.py``'s ``_send_live_edit``):

  1. Accumulate assistant text as it streams; mark the draft dirty.
  2. A background loop flushes every ~0.5s, reusing one ``draft_id`` so Telegram
     *animates* native drafts.
  3. The streaming approach is picked up front from the chat type: private chats
     (positive ``chat_id`` in the Bot API) use native drafts; groups/supergroups
     (negative ``chat_id``) go straight to live-edit, never burning a doomed
     ``sendMessageDraft`` call per turn. A draft failure in a private chat still
     falls back to live-edit mid-turn instead of going silent.
  4. On turn completion, send the real message(s). A live-edit message is reused
     for the first chunk (no duplicate); overflow goes to new messages. Drafts and
     final messages render GFM as Telegram MarkdownV2 (ADR-0010), ≤4096-char chunks.
  5. The answer ends the turn. If other messages landed below the streamed answer
     bubble while it was open (progress overflow at finalize, approval prompts,
     retry notices — Telegram cannot insert above them), the stale bubble is
     deleted and the answer is re-sent at the bottom.

The transport-agnostic :class:`DraftSession` is unit-tested with a fake.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from balam.agent.backend import AgentBackend, TurnRequest
from balam.agent.events import (
    PermissionRequested,
    QuestionAsked,
    ReasoningUpdated,
    RetryNotice,
    SessionStarted,
    TextUpdated,
    ToolUpdated,
    TurnFailed,
    TurnFinished,
)
from balam.approvals import (
    Choice,
    PendingApprovals,
    PendingQuestions,
    Verdict,
    decide,
    is_edit,
    request_target_paths,
)
from balam.attachments import PromptFile
from balam.markdown import gfm_to_telegram
from balam.opencode_tools import Tool
from balam.telegram_utils import thread_kwargs

logger = logging.getLogger(__name__)

#: How often the background loop pushes a draft update (seconds), matching zog.
DRAFT_INTERVAL_S = 0.5

#: Caps on inline Bash output, matching open-shrimp. Full output goes to the
#: Mini App later (Tier 2/3); for now we inline-truncate, keeping the tail.
BASH_OUTPUT_MAX_LINES = 50
BASH_OUTPUT_MAX_CHARS = 1500

#: OpenCode's lowercase wire tool names → a friendly display label. Unknown
#: names (e.g. MCP tools) fall through unchanged.
_TOOL_DISPLAY: dict[str, str] = {
    Tool.BASH: "Bash",
    Tool.READ: "Read",
    Tool.EDIT: "Edit",
    Tool.WRITE: "Write",
    Tool.GLOB: "Glob",
    Tool.GREP: "Grep",
    Tool.LIST: "LS",
    Tool.WEBFETCH: "WebFetch",
    Tool.APPLY_PATCH: "ApplyPatch",
    Tool.TODOWRITE: "TodoWrite",
    Tool.TASK: "Task",
    Tool.AGENT: "Agent",
}

Renderer = Callable[[str], list[str]]


class DraftTransport(Protocol):
    """Where draft previews and final messages land.

    ``send_message`` returns the new message's id (or ``None``) so the live-edit
    fallback can keep editing it; ``edit_message`` updates a message in place;
    ``delete_message`` removes one (used to drop a stale streamed bubble when
    the answer must be re-sent at the bottom of the topic).
    """

    async def send_draft(self, draft_id: int, text: str) -> None: ...
    async def send_message(self, text: str) -> int | None: ...
    async def edit_message(self, message_id: int, text: str) -> None: ...
    async def delete_message(self, message_id: int) -> None: ...


class DraftSession:
    """Tracks the in-progress draft for one streamed reply: accumulates text,
    flushes it as an animated draft, and finalizes into real message(s).

    Mirrors zog's ``_DraftState`` + ``_flush_draft`` + finalize flow. Native
    ``sendMessageDraft`` only works in **private chats** (Telegram rejects it
    elsewhere with ``Textdraft_peer_invalid``); ``native_drafts=False`` starts a
    group/supergroup session directly in **live-edit streaming** — send one real
    message and keep editing it in place — exactly the throttled ``editMessageText``
    fallback ADR-0010 calls for (ported from open-shrimp's ``_send_live_edit``).
    A failing draft call still flips to live-edit mid-turn as a safety net.
    """

    def __init__(
        self,
        transport: DraftTransport,
        *,
        draft_id: int | None = None,
        render: Renderer = gfm_to_telegram,
        native_drafts: bool = True,
    ) -> None:
        self._transport = transport
        # draft_id must be non-zero and stable for the segment (animates on change).
        self._draft_id = draft_id if draft_id is not None else random.randint(1, 2**31)
        self._render = render
        self._raw = ""
        self._dirty = False
        # Native drafts disabled (unsupported chat type) → use live-edit instead.
        # ``native_drafts=False`` disables them up front when the caller already
        # knows the chat can't take them (groups/supergroups).
        self._disabled = not native_drafts
        # The live-edit message reused across flushes and at finalize, and the
        # last text pushed to it (so an unchanged render is not re-sent).
        self._live_edit_message_id: int | None = None
        self._live_edit_last: str | None = None

    @property
    def text(self) -> str:
        return self._raw

    @property
    def drafts_disabled(self) -> bool:
        return self._disabled

    def set_text(self, text: str) -> None:
        """Replace the accumulated text; marks dirty if it changed."""
        if text != self._raw:
            self._raw = text
            self._dirty = True

    async def flush_draft(self) -> None:
        """Flush the current text as a streaming preview, if dirty.

        Uses native ``sendMessageDraft`` until it fails (e.g. a group chat, which
        Telegram refuses), then switches permanently to live-edit streaming. Only
        the first chunk is previewed; the full content is split at finalize.
        """
        if not self._dirty:
            return
        if self._disabled:
            await self._flush_live_edit()
            return
        chunks = self._render(self._raw)
        if not chunks:
            return
        try:
            await self._transport.send_draft(self._draft_id, chunks[0])
            self._dirty = False
        except Exception:
            # Native drafts aren't available for this chat (a supergroup/topic
            # raises Textdraft_peer_invalid) — switch to live-edit and flush it
            # now so the user doesn't wait for the next tick. Expected in groups,
            # so log without the traceback.
            logger.info("draft streaming unavailable; switching to live-edit streaming")
            self._disabled = True
            await self._flush_live_edit()

    async def _flush_live_edit(self) -> None:
        """Live-edit fallback: send one message, then edit it in place as text
        grows. Defers while the text overflows one chunk (handled at finalize)."""
        if not self._dirty:
            return
        chunks = self._render(self._raw)
        if not chunks or len(chunks) > 1:
            return
        text = chunks[0]
        if text == self._live_edit_last:
            self._dirty = False
            return
        try:
            if self._live_edit_message_id is None:
                self._live_edit_message_id = await self._transport.send_message(text)
            else:
                await self._transport.edit_message(self._live_edit_message_id, text)
            self._live_edit_last = text
            self._dirty = False
        except Exception:
            logger.debug("live-edit flush failed", exc_info=True)

    async def finalize(
        self,
        fallback: str = "(the agent finished without producing any text)",
        *,
        latest_message_id: int | None = None,
    ) -> None:
        """Send the accumulated text as real message(s), split at the char cap.

        If a live-edit message exists, its first chunk is delivered by editing
        that message in place (no duplicate of the streamed bubble); any overflow
        chunks are sent as new messages.

        ``latest_message_id`` is the id of the most recent message the turn sent
        to the topic. When given and it isn't the live-edit message, other
        messages landed *below* the streamed bubble — and since this text must
        end the turn, the stale bubble is deleted and the text re-sent at the
        bottom. If the delete fails the bubble is edited in place instead, so
        the content is never duplicated.
        """
        text = self._raw if self._raw.strip() else fallback
        if (
            self._live_edit_message_id is not None
            and latest_message_id is not None
            and latest_message_id != self._live_edit_message_id
        ):
            try:
                await self._transport.delete_message(self._live_edit_message_id)
            except Exception:
                logger.debug("could not delete stale streamed bubble", exc_info=True)
            else:
                self._live_edit_message_id = None
                self._live_edit_last = None
        for i, chunk in enumerate(self._render(text)):
            if i == 0 and self._live_edit_message_id is not None:
                # Skip the edit when the streamed bubble already shows this text —
                # Telegram would otherwise 400 with "message is not modified".
                if chunk != self._live_edit_last:
                    await self._transport.edit_message(self._live_edit_message_id, chunk)
            else:
                await self._transport.send_message(chunk)


#: One streamed fragment: ``(arrival_order, kind, rendered_text)`` where ``kind``
#: is ``"text"`` (assistant prose), ``"tool"`` (a rendered tool-call line), or
#: ``"narration"`` (an earlier step's interim text, demoted to progress).
StreamPart = tuple[int, str, str]


def _join_stream(parts: dict[str, StreamPart]) -> str:
    """Render the session's text and tool parts as one GFM string, in arrival
    order. Consecutive text fragments concatenate (they are deltas of one
    message); tool lines and demoted narration blocks are set off from their
    neighbours by separators."""
    out = ""
    prev_kind: str | None = None
    for _order, kind, text in sorted(parts.values(), key=lambda p: p[0]):
        if not text:
            continue
        if out:
            if prev_kind != kind:  # kind transition (text↔tool↔narration)
                out = out.rstrip("\n") + "\n\n"
            elif kind == "tool":  # group consecutive tool lines
                out = out.rstrip("\n") + "\n"
            elif kind == "narration":  # blocks from different steps
                out = out.rstrip("\n") + "\n\n"
            # text after text: concatenate the deltas, no separator
        out += text
        prev_kind = kind
    return out


def _relpath(path: str, directory: str | None) -> str:
    """Show *path* relative to the context *directory* when it lives under it;
    otherwise return it unchanged (e.g. an absolute path outside the workspace)."""
    if not directory or not path:
        return path
    try:
        rel = os.path.relpath(path, directory)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


#: apply_patch envelope headers; the path follows the prefix. Used only to render
#: a readable tool line (the boundary check uses the permission metadata instead).
_APPLY_PATCH_HEADERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")


def _apply_patch_files(patch_text: str) -> list[str]:
    """File paths an apply_patch envelope touches, for display."""
    out: list[str] = []
    for line in patch_text.splitlines():
        for prefix in _APPLY_PATCH_HEADERS:
            if line.startswith(prefix):
                path = line[len(prefix) :].strip()
                if path:
                    out.append(path)
                break
    return out


def _tool_summary(tool: str, tool_input: dict[str, Any], directory: str | None) -> str:
    """A one-line argument summary for a tool call (paths shown workspace-relative)."""
    if tool in (Tool.READ, Tool.EDIT, Tool.WRITE):
        return _relpath(tool_input.get("filePath", ""), directory)
    if tool == Tool.LIST:
        return _relpath(tool_input.get("path", ""), directory)
    if tool == Tool.GLOB:
        return tool_input.get("pattern", "")
    if tool == Tool.GREP:
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {_relpath(path, directory)}" if path else pattern
    if tool == Tool.APPLY_PATCH:
        # The raw patchText envelope is huge and breaks MarkdownV2; show the files
        # it touches instead (parsed from the envelope headers).
        paths = _apply_patch_files(tool_input.get("patchText", ""))
        return ", ".join(_relpath(p, directory) for p in paths)
    if tool == Tool.WEBFETCH:
        return tool_input.get("url", "")
    if tool in (Tool.TASK, Tool.AGENT):
        return tool_input.get("description", "") or tool_input.get("subagent_type", "")
    # Generic: first string-valued argument, capped.
    for value in tool_input.values():
        if isinstance(value, str) and value:
            return value[:80]
    return ""


def _truncate_output(text: str) -> str:
    """Truncate tool output to the inline caps, keeping the most recent tail."""
    text = text.strip()
    lines = text.splitlines()
    truncated = False
    if len(lines) > BASH_OUTPUT_MAX_LINES:
        lines = lines[-BASH_OUTPUT_MAX_LINES:]
        truncated = True
    result = "\n".join(lines)
    if len(result) > BASH_OUTPUT_MAX_CHARS:
        result = result[-BASH_OUTPUT_MAX_CHARS:]
        truncated = True
    return f"…(truncated)\n{result}" if truncated else result


def _coerce_output(output: Any) -> str:
    """Flatten an OpenCode tool ``output``/``error`` payload to plain text."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(
            block.get("text", "")
            for block in output
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return "" if output is None else str(output)


def _render_tool_part(
    tool: str,
    tool_input: dict[str, Any],
    status: str,
    output: Any,
    error: Any,
    directory: str | None,
) -> str:
    """Render a terminal tool part as a compact GFM line for the stream.

    Bash is special-cased to show its natural-language ``description`` and the
    command in a fenced block; successful output is omitted (it is the noise the
    stream drowns in, and the full output is available in the Mini App), and only
    a failed call's tail is kept. Everything else is a one-liner like
    ``🔧 Read src/foo.py``.
    """
    if tool == Tool.BASH:
        command = tool_input.get("command", "")
        description = tool_input.get("description", "")
        line = f"🔧 Bash — {description}" if description else "🔧 Bash"
        if command:
            line += f"\n```\n{command}\n```"
        # Only a failed call's output is actionable. ``status == "error"`` is the
        # backends' authoritative signal (SDK: tool_result.is_error; OpenCode:
        # tool-state status), with the payload in ``error`` (``output`` as a
        # fallback, since OpenCode may carry partial output alongside the error).
        if status == "error":
            body = _truncate_output(_coerce_output(error) or _coerce_output(output))
            if body:
                line += f"\n```\n{body}\n```"
        return line

    display = _TOOL_DISPLAY.get(tool, tool)
    summary = _tool_summary(tool, tool_input, directory)
    line = f"🔧 {display}"
    if summary:
        line += f" `{summary}`"
    if status == "error":
        line += " ⚠️"
    return line


def _format_approval_request(
    tool: str, tool_input: dict[str, Any], directory: str | None, category: str | None = None
) -> str:
    """A GFM prompt asking the user to approve one tool call.

    Bash shows the command; file tools show the (workspace-relative) path; other
    tools fall back to the generic argument summary — the same vocabulary as the
    inline tool lines so a prompt reads like the stream around it.
    """
    display = _TOOL_DISPLAY.get(tool, tool)
    header = f"🔐 Allow **{display}**?"
    if category and category != tool:
        header += f"\nPermission: `{category}`"
    if tool == Tool.BASH:
        command = tool_input.get("command", "")
        # The tool's natural-language ``description`` (e.g. "Install acli via apt
        # repository") is the *reason* for the call — surface it so the prompt
        # explains what it's approving, not just the raw command.
        description = tool_input.get("description", "")
        body = header
        if description:
            body += f"\n_{description}_"
        if command:
            body += f"\n```\n{command}\n```"
        return body
    summary = _tool_summary(tool, tool_input, directory)
    return f"{header}\n`{summary}`" if summary else header


def _approval_keyboard(token: str, category: str) -> InlineKeyboardMarkup:
    """The inline keyboard for an approval prompt. Edit requests (category
    ``edit`` — edit/write/apply_patch) also offer "accept all edits" so the user
    can stop being asked for in-workspace edits."""
    rows = [
        [
            InlineKeyboardButton("Allow once", callback_data=f"appr:{Choice.ALLOW.value}:{token}"),
            InlineKeyboardButton("Deny", callback_data=f"appr:{Choice.DENY.value}:{token}"),
        ]
    ]
    if is_edit(category):
        rows.append(
            [
                InlineKeyboardButton(
                    "Accept all edits", callback_data=f"appr:{Choice.ALL.value}:{token}"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _question_keyboard(
    token: str,
    question_index: int,
    options: list[dict[str, Any]],
    *,
    custom: bool = True,
    multiple: bool = False,
    selected_indexes: set[int] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    selected_indexes = selected_indexes or set()
    for option_index, option in enumerate(options):
        label = str(option.get("label") or f"Option {option_index + 1}")
        if multiple:
            label = f"{'☑' if option_index in selected_indexes else '☐'} {label}"
        rows.append(
            [
                InlineKeyboardButton(
                    label[:64], callback_data=f"qst:{token}:{question_index}:{option_index}"
                )
            ]
        )
    if multiple:
        rows.append([InlineKeyboardButton("Done", callback_data=f"qstd:{token}:{question_index}")])
    if custom:
        rows.append(
            [
                InlineKeyboardButton(
                    "Type your own answer", callback_data=f"qstc:{token}:{question_index}"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _format_question(question: dict[str, Any]) -> str:
    header = str(question.get("header") or "Question")
    prompt = str(question.get("question") or "Choose one option.")
    lines = [f"❓ **{header}**", prompt]
    options = question.get("options")
    if isinstance(options, list) and options:
        lines.append("")
        for option in options:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "Option")
            description = str(option.get("description") or "")
            lines.append(f"- **{label}** — {description}" if description else f"- **{label}**")
    return "\n".join(lines)


def _make_transport(
    bot: Any,
    chat_id: int,
    thread_id: int | None,
    on_sent: Callable[[int | None], None] | None = None,
) -> DraftTransport:
    # message_thread_id routes both the draft and the final message to the topic.
    topic_kwargs = thread_kwargs(thread_id)

    class _Transport:
        async def send_draft(self, draft_id: int, text: str) -> None:
            await bot.send_message_draft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=text,
                parse_mode="MarkdownV2",
                **topic_kwargs,
            )

        async def send_message(self, text: str) -> int | None:
            try:
                msg = await bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="MarkdownV2", **topic_kwargs
                )
            except Exception:
                # Malformed MarkdownV2 → resend without formatting rather than drop.
                logger.debug("MarkdownV2 send failed; falling back to plain text", exc_info=True)
                msg = await bot.send_message(chat_id=chat_id, text=text, **topic_kwargs)
            message_id = getattr(msg, "message_id", None)
            if on_sent is not None:
                on_sent(message_id)
            return message_id

        async def delete_message(self, message_id: int) -> None:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)

        async def edit_message(self, message_id: int, text: str) -> None:
            # edit_message_text addresses the message by id within the chat, so no
            # thread kwargs. "message is not modified" is benign (identical render).
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="MarkdownV2",
                )
            except Exception as exc:
                if "not modified" in str(exc).lower():
                    return
                logger.debug("MarkdownV2 edit failed; falling back to plain text", exc_info=True)
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                except Exception as exc2:
                    if "not modified" not in str(exc2).lower():
                        raise

    return _Transport()


async def stream_reply(
    *,
    bot: Any,
    backend: AgentBackend,
    session_id: str | None,
    chat_id: int,
    thread_id: int | None,
    prompt: str,
    directory: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    pending: PendingApprovals | None = None,
    pending_questions: PendingQuestions | None = None,
    allowed_dirs: list[str] | None = None,
    additional_directories: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    mcp: dict[str, Any] | None = None,
    files: list[PromptFile] | None = None,
    plan_mode: bool = False,
    plan_view: Callable[[str, str], InlineKeyboardButton | None] | None = None,
    on_plan_approved: Callable[[], None] | None = None,
    on_session_started: Callable[[str], None] | None = None,
    draft_interval: float = DRAFT_INTERVAL_S,
) -> None:
    """Run one turn through ``backend`` and stream its reply into the topic.

    ``directory``/``provider``/``model``/``effort`` come from the topic's
    resolved context (:class:`balam.router.ResolvedSession`) and ride the
    :class:`~balam.agent.backend.TurnRequest` so the agent runs in the right
    workspace with the right model. The backend owns subscribing/prompting and
    yields the normalized :mod:`balam.agent.events` stream; this function renders
    it, animates a draft as text grows, and finalizes into real message(s) when
    the turn finishes (or fails).

    When ``pending`` is given, a :class:`~balam.agent.events.PermissionRequested`
    is dispatched to a background task that runs :func:`balam.approvals.decide`
    against ``allowed_dirs`` and either auto-allows or shows an inline keyboard and
    awaits the user's choice (ADR-0012). Without ``pending`` the request is left
    unhandled (e.g. unit tests of the text/tool path).

    ``plan_view`` (see :func:`balam.miniapp.make_plan_view_button`) maps a plan
    ``(title, content)`` to a Mini App button; when a question carries a
    ``plan_path`` (a plan-approval), the plan file is snapshotted and the button
    rides the question keyboard as an extra row.

    ``plan_mode`` puts the turn in plan mode (the backend maps it to OpenCode's
    plan agent or the SDK's ``permission_mode="plan"``). ``on_plan_approved``
    fires when a plan-approval question is answered "Yes", so the caller can drop
    its sticky plan-mode flag. ``on_session_started`` receives the backend's real
    session id once known — used to persist a lazily-minted SDK session.
    """
    # Local session id: known up front for OpenCode, learned from the first
    # SessionStarted event for a lazily-minted SDK session.
    sid = session_id
    # The id of the most recent message this turn sent to the topic — live-edit
    # bubbles and finalize chunks (via the transport) as well as approval
    # prompts, question keyboards, and retry notices (noted at their send
    # sites). The answer's finalize compares its streamed bubble against this to
    # know whether other messages landed below it (Telegram ids are monotonic).
    last_sent_id: int | None = None

    def note_sent(message_id: int | None) -> None:
        nonlocal last_sent_id
        if message_id is not None:
            last_sent_id = message_id

    transport = _make_transport(bot, chat_id, thread_id, on_sent=note_sent)
    # sendMessageDraft is private-chat only; in the Bot API private chats have
    # positive ids and groups/supergroups negative ones, so the chat id alone
    # picks the streaming approach — no wasted draft call per group turn.
    native_drafts = chat_id > 0
    reasoning_draft = DraftSession(transport, native_drafts=native_drafts)
    answer_draft = DraftSession(transport, native_drafts=native_drafts)
    topic_kwargs = thread_kwargs(thread_id)

    streaming = True

    async def flush_loop() -> None:
        while streaming:
            await asyncio.sleep(draft_interval)
            if not streaming:
                break
            await reasoning_draft.flush_draft()
            await answer_draft.flush_draft()

    flush_task = asyncio.create_task(flush_loop())

    # Reasoning/progress and answer text are delivered as separate messages.
    # Tool calls are progress, so they live with the reasoning stream.
    reasoning_parts: dict[str, StreamPart] = {}
    answer_parts: dict[str, StreamPart] = {}
    # The assistant message whose text currently fills the answer draft. The
    # agent opens a new assistant message per step, and a step's interim
    # narration ("I'll check…") is a plain text part just like the final
    # answer — only the *last* message's text is the answer.
    answer_message_id: str | None = None
    order = 0
    error_text: str | None = None
    retry_noticed = False
    dirs = allowed_dirs or ([directory] if directory else [])
    # Per-request approval tasks, so the event loop isn't blocked while the user
    # decides. Torn down with the consumer.
    permission_tasks: set[asyncio.Task[None]] = set()
    question_tasks: set[asyncio.Task[None]] = set()

    async def request_approval(
        request_id: str, category: str, tool: str, tool_input: dict[str, Any]
    ) -> None:
        """Ask the user via an inline keyboard, then reply to the backend. The
        callback handler resolves the future and updates the message; here we
        only translate the choice into a permission reply. ``category`` drives the
        keyboard (whether to offer "accept all edits"); ``tool`` is display-only."""
        assert pending is not None
        token, future = pending.register(sid or "")
        gfm = _format_approval_request(tool, tool_input, directory, category)
        keyboard = _approval_keyboard(token, category)
        chunks = gfm_to_telegram(gfm)
        text = chunks[0] if chunks else f"🔐 Allow {tool}?"
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
                **topic_kwargs,
            )
            note_sent(getattr(msg, "message_id", None))
        except Exception:
            logger.debug("approval keyboard MarkdownV2 send failed; retrying plain", exc_info=True)
            try:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=f"🔐 Allow {tool}? (see request)",
                    reply_markup=keyboard,
                    **topic_kwargs,
                )
                note_sent(getattr(msg, "message_id", None))
            except Exception:
                logger.exception("failed to send approval keyboard; denying")
                pending.discard(token)
                await backend.reply_permission(
                    request_id,
                    allow=False,
                    directory=directory,
                    message="Could not prompt the user.",
                )
                return
        try:
            choice = await future
        except asyncio.CancelledError:
            # Turn torn down (e.g. /cancel) before the user answered: unblock the
            # agent so it isn't left waiting on a permission that will never come.
            await backend.reply_permission(
                request_id, allow=False, directory=directory, message="Cancelled."
            )
            raise
        finally:
            pending.discard(token)
        if choice is Choice.DENY:
            await backend.reply_permission(
                request_id, allow=False, directory=directory, message="Denied by the user."
            )
        else:
            await backend.reply_permission(request_id, allow=True, directory=directory)

    async def handle_permission(request: PermissionRequested) -> None:
        cwd = dirs[0] if dirs else None
        # Classify by the permission category; take edit targets from the request
        # metadata (authoritative for apply_patch) and reads from the input.
        paths = request_target_paths(request.category, request.metadata, request.input, cwd)
        verdict = decide(
            request.category,
            paths,
            allowed_dirs=dirs,
            accept_all_edits=pending.is_accept_all_edits(sid or "") if pending else False,
        )
        if verdict is Verdict.ALLOW:
            await backend.reply_permission(request.request_id, allow=True, directory=directory)
            return
        await request_approval(request.request_id, request.category, request.tool, request.input)

    async def request_questions(request: QuestionAsked) -> None:
        if pending_questions is None:
            await backend.reject_question(request.request_id, directory=directory)
            return
        raw_questions = request.questions
        if not raw_questions:
            await backend.reject_question(request.request_id, directory=directory)
            return

        questions = [q for q in raw_questions if isinstance(q, dict)]
        labels: list[list[str]] = []
        multiples: list[bool] = []
        customs: list[bool] = []
        for question in questions:
            options = question.get("options")
            if not isinstance(options, list) or not options:
                await backend.reject_question(request.request_id, directory=directory)
                return
            labels.append([str(o.get("label") or "") for o in options if isinstance(o, dict)])
            multiples.append(question.get("multiple", False) is True)
            customs.append(question.get("custom", True) is not False)
        if any(not question_labels for question_labels in labels):
            await backend.reject_question(request.request_id, directory=directory)
            return

        # A plan-approval carries the freshly written plan: snapshot it and ride a
        # "View plan" button on the question keyboard. The backend supplies either
        # an inline ``plan_text`` (SDK) or a ``plan_path`` to read (OpenCode).
        # Strictly best-effort — any failure (file unreadable, no public URL) must
        # never block the Yes/No flow, which is what actually answers the agent.
        is_plan = request.plan_path is not None or request.plan_text is not None
        plan_button: InlineKeyboardButton | None = None
        if plan_view is not None and is_plan:
            try:
                if request.plan_text is not None:
                    plan_button = plan_view("Plan", request.plan_text)
                elif request.plan_path is not None:
                    text = await asyncio.to_thread(
                        Path(request.plan_path).read_text, "utf-8", "replace"
                    )
                    plan_button = plan_view(os.path.basename(request.plan_path), text)
            except Exception:
                logger.debug("could not snapshot plan", exc_info=True)

        token, futures = pending_questions.register(
            sid or "",
            labels,
            multiples=multiples,
            customs=customs,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        try:
            for index, question in enumerate(questions):
                chunks = gfm_to_telegram(_format_question(question))
                text = chunks[0] if chunks else "❓ Question"
                keyboard = _question_keyboard(
                    token,
                    index,
                    question["options"],
                    custom=customs[index],
                    multiple=multiples[index],
                )
                if index == 0 and plan_button is not None:
                    keyboard = InlineKeyboardMarkup([*keyboard.inline_keyboard, [plan_button]])
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                    **topic_kwargs,
                )
                note_sent(getattr(msg, "message_id", None))
            answers = await asyncio.gather(*futures)
        except asyncio.CancelledError:
            pending_questions.discard(token)
            await backend.reject_question(request.request_id, directory=directory)
            raise
        except Exception:
            logger.exception("failed to ask the agent's question in Telegram")
            pending_questions.discard(token)
            await backend.reject_question(request.request_id, directory=directory)
            return
        await backend.reply_question(request.request_id, answers, directory=directory)
        # "Yes" to a plan-approval makes the agent switch to building, so the
        # caller's sticky plan-mode flag must drop with it — else the next prompt
        # would force it straight back into plan mode. "No" keeps plan mode.
        if is_plan and on_plan_approved is not None:
            if answers and answers[0] == ["Yes"]:
                on_plan_approved()

    async def note_retry(detail: str | None) -> None:
        """Tell the user the turn is being retried (e.g. provider rate limit).

        Some failures (rate limits, transient 5xx) are retried internally without
        ending the turn, so it can stall for minutes with no visible output.
        Surface a single notice per turn — enough to explain the silence and
        point at ``/cancel`` — without spamming one per attempt.
        """
        nonlocal retry_noticed
        if retry_noticed:
            return
        retry_noticed = True
        body = "⏳ The model provider is rate-limited — retrying…"
        if detail:
            body += f"\n{detail}"
        body += "\nThis can take a while; send /cancel to stop waiting."
        try:
            msg = await bot.send_message(chat_id=chat_id, text=body, **topic_kwargs)
            note_sent(getattr(msg, "message_id", None))
        except Exception:
            logger.debug("failed to post retry notice", exc_info=True)

    turn = TurnRequest(
        directory=directory,
        prompt=prompt,
        session_id=session_id,
        provider=provider,
        model=model,
        effort=effort,
        files=files,
        plan_mode=plan_mode,
        allowed_tools=allowed_tools or [],
        additional_directories=additional_directories or [],
        mcp=mcp or {},
        chat_id=chat_id,
        thread_id=thread_id,
    )

    async def consume() -> None:
        nonlocal order, error_text, answer_message_id, sid
        async for event in backend.run_turn(turn):
            if isinstance(event, SessionStarted):
                if sid != event.session_id:
                    sid = event.session_id
                    if on_session_started is not None:
                        on_session_started(sid)

            elif isinstance(event, TextUpdated):
                if event.message_id != answer_message_id:
                    # Text from a new step: what the answer draft holds was an
                    # earlier step's narration, not the answer. Demote it to the
                    # progress stream (it keeps its arrival order, so it
                    # interleaves with the tool lines it narrates) and start the
                    # answer over with the new step's text.
                    if answer_parts:
                        for pid, (pos, _kind, prev) in answer_parts.items():
                            reasoning_parts[pid] = (pos, "narration", prev)
                        answer_parts.clear()
                        reasoning_draft.set_text(_join_stream(reasoning_parts))
                    answer_message_id = event.message_id
                if event.part_id in answer_parts:
                    answer_parts[event.part_id] = (
                        answer_parts[event.part_id][0],
                        "text",
                        event.text,
                    )
                else:
                    answer_parts[event.part_id] = (order, "text", event.text)
                    order += 1
                answer_draft.set_text(_join_stream(answer_parts))

            elif isinstance(event, ReasoningUpdated):
                if event.part_id in reasoning_parts:
                    reasoning_parts[event.part_id] = (
                        reasoning_parts[event.part_id][0],
                        "text",
                        event.text,
                    )
                else:
                    reasoning_parts[event.part_id] = (order, "text", event.text)
                    order += 1
                reasoning_draft.set_text(_join_stream(reasoning_parts))

            elif isinstance(event, ToolUpdated):
                # Reserve a slot at the call's arrival position (so the tool line
                # interleaves before any later text), but only render once the
                # call finishes.
                key = f"tool:{event.call_id}"
                if key not in reasoning_parts:
                    reasoning_parts[key] = (order, "tool", "")
                    order += 1
                if event.status in ("completed", "error"):
                    rendered = _render_tool_part(
                        event.tool, event.input, event.status, event.output, event.error, directory
                    )
                    reasoning_parts[key] = (reasoning_parts[key][0], "tool", rendered)
                    reasoning_draft.set_text(_join_stream(reasoning_parts))

            elif isinstance(event, PermissionRequested):
                # Handle in a child task so a slow user decision doesn't stall the
                # event loop (the turn stays busy while a permission is pending).
                if pending is None:
                    continue
                ptask = asyncio.create_task(handle_permission(event))
                permission_tasks.add(ptask)
                ptask.add_done_callback(permission_tasks.discard)

            elif isinstance(event, QuestionAsked):
                qtask = asyncio.create_task(request_questions(event))
                question_tasks.add(qtask)
                qtask.add_done_callback(question_tasks.discard)

            elif isinstance(event, RetryNotice):
                await note_retry(event.detail)

            elif isinstance(event, TurnFailed):
                error_text = event.message
                break

            elif isinstance(event, TurnFinished):
                break

    consume_task = asyncio.create_task(consume())
    try:
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing", **topic_kwargs)
        except Exception:
            pass

        try:
            await consume_task
        except Exception as exc:
            # A failed turn must still finalize a real message (ADR-0010): fold the
            # error into the reply instead of letting it bubble out and skip
            # finalize() below.
            logger.exception("streaming the reply failed")
            error_text = error_text or str(exc) or exc.__class__.__name__

        if error_text:
            base = _join_stream(answer_parts)
            prefix = f"{base}\n\n" if base.strip() else ""
            answer_draft.set_text(f"{prefix}⚠️ {error_text}")
    finally:
        # Stop the flusher and the consumer before finalizing so neither races the
        # real message, and so a leftover task can't outlive the turn. Pending
        # approval tasks are cancelled too; each rejects its request on the way
        # out so OpenCode isn't left blocked on an answer that will never come.
        streaming = False
        if not consume_task.done():
            consume_task.cancel()
        for ptask in list(permission_tasks):
            if not ptask.done():
                ptask.cancel()
        for qtask in list(question_tasks):
            if not qtask.done():
                qtask.cancel()
        await asyncio.gather(
            flush_task, consume_task, *permission_tasks, *question_tasks, return_exceptions=True
        )

    # Replace ephemeral drafts with real, persistent messages. Reasoning/progress
    # is intentionally separate from the answer; only emit the answer fallback if
    # the turn produced nothing visible at all. The reasoning stream keeps its
    # position; the answer must end the turn, so its finalize gets the last sent
    # id and re-sends at the bottom if anything landed below its bubble.
    if reasoning_draft.text.strip():
        await reasoning_draft.finalize()
    if answer_draft.text.strip() or not reasoning_draft.text.strip():
        await answer_draft.finalize(latest_message_id=last_sent_id)
