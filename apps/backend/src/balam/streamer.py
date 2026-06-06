"""Stream an OpenCode reply into a Telegram topic using native message drafts.

Telegram's ``sendMessageDraft`` streams partial text without flicker (ADR-0010)
and works inside forum topics when ``message_thread_id`` is passed. This follows
the proven approach in ~/projects/zog (``src/zog/stream.py``):

  1. Accumulate assistant text as it streams; mark the draft dirty.
  2. A background loop flushes a draft every ~0.5s via ``send_message_draft``,
     reusing one ``draft_id`` so Telegram *animates* the updates.
  3. If a draft call fails, disable drafts and degrade gracefully — the final
     real message still goes out.
  4. On turn completion, send the real message(s) via ``send_message``. Both the
     draft and the final message render GFM as Telegram MarkdownV2 (ADR-0010),
     split into ≤4096-char chunks.

The transport-agnostic :class:`DraftSession` is unit-tested with a fake.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Callable
from typing import Any, Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from balam.approvals import (
    Choice,
    PendingApprovals,
    Verdict,
    decide,
    is_edit,
    request_target_paths,
)
from balam.markdown import gfm_to_telegram
from balam.opencode import OpenCode
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
    "bash": "Bash",
    "read": "Read",
    "edit": "Edit",
    "write": "Write",
    "glob": "Glob",
    "grep": "Grep",
    "list": "LS",
    "webfetch": "WebFetch",
    "apply_patch": "ApplyPatch",
    "todowrite": "TodoWrite",
    "task": "Task",
    "agent": "Agent",
}

Renderer = Callable[[str], list[str]]


class DraftTransport(Protocol):
    """Where draft previews and final messages land."""

    async def send_draft(self, draft_id: int, text: str) -> None: ...
    async def send_message(self, text: str) -> None: ...


class DraftSession:
    """Tracks the in-progress draft for one streamed reply: accumulates text,
    flushes it as an animated draft, and finalizes into real message(s).

    Mirrors zog's ``_DraftState`` + ``_flush_draft`` + finalize flow.
    """

    def __init__(
        self,
        transport: DraftTransport,
        *,
        draft_id: int | None = None,
        render: Renderer = gfm_to_telegram,
    ) -> None:
        self._transport = transport
        # draft_id must be non-zero and stable for the segment (animates on change).
        self._draft_id = draft_id if draft_id is not None else random.randint(1, 2**31)
        self._render = render
        self._raw = ""
        self._dirty = False
        self._disabled = False

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
        """Flush the current text as a draft preview, if dirty and not disabled.

        Only the first chunk is previewed (full content is split at finalize). A
        failed draft call disables further previews — it never tears down the
        stream.
        """
        if self._disabled or not self._dirty:
            return
        chunks = self._render(self._raw)
        if not chunks:
            return
        try:
            await self._transport.send_draft(self._draft_id, chunks[0])
            self._dirty = False
        except Exception:
            logger.debug("send_message_draft failed; disabling drafts", exc_info=True)
            self._disabled = True

    async def finalize(
        self, fallback: str = "(the agent finished without producing any text)"
    ) -> None:
        """Send the accumulated text as real message(s), split at the char cap."""
        text = self._raw if self._raw.strip() else fallback
        for chunk in self._render(text):
            await self._transport.send_message(chunk)


def _describe_error(error: Any) -> str:
    """Extract a readable line from an OpenCode session error payload."""
    if isinstance(error, dict):
        name = error.get("name")
        message = (error.get("data") or {}).get("message")
        if name and message:
            return f"{name}: {message}"
        if message:
            return message
        if name:
            return name
    return "The agent reported an error."


#: One streamed fragment: ``(arrival_order, kind, rendered_text)`` where ``kind``
#: is ``"text"`` (assistant prose) or ``"tool"`` (a rendered tool-call line).
StreamPart = tuple[int, str, str]


def _join_stream(parts: dict[str, StreamPart]) -> str:
    """Render the session's text and tool parts as one GFM string, in arrival
    order. Consecutive text fragments concatenate (they are deltas of one
    message); a tool line is set off from its neighbours by a blank line."""
    out = ""
    prev_kind: str | None = None
    for _order, kind, text in sorted(parts.values(), key=lambda p: p[0]):
        if not text:
            continue
        if out:
            if prev_kind != kind:  # text↔tool transition
                out = out.rstrip("\n") + "\n\n"
            elif kind == "tool":  # group consecutive tool lines
                out = out.rstrip("\n") + "\n"
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
    if tool in ("read", "edit", "write"):
        return _relpath(tool_input.get("filePath", ""), directory)
    if tool == "list":
        return _relpath(tool_input.get("path", ""), directory)
    if tool == "glob":
        return tool_input.get("pattern", "")
    if tool == "grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {_relpath(path, directory)}" if path else pattern
    if tool == "apply_patch":
        # The raw patchText envelope is huge and breaks MarkdownV2; show the files
        # it touches instead (parsed from the envelope headers).
        paths = _apply_patch_files(tool_input.get("patchText", ""))
        return ", ".join(_relpath(p, directory) for p in paths)
    if tool == "webfetch":
        return tool_input.get("url", "")
    if tool in ("task", "agent"):
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
    tool: str, tool_input: dict[str, Any], state: dict[str, Any], directory: str | None
) -> str:
    """Render a terminal tool part as a compact GFM line for the stream.

    Bash is special-cased to show the command and its (truncated) output in
    fenced blocks; everything else is a one-liner like ``🔧 Read src/foo.py``.
    """
    status = state.get("status")
    if tool == "bash":
        command = tool_input.get("command", "")
        line = "🔧 Bash"
        if command:
            line += f"\n```\n{command}\n```"
        payload = state.get("error") if status == "error" else state.get("output")
        body = _truncate_output(_coerce_output(payload))
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


def _format_approval_request(tool: str, tool_input: dict[str, Any], directory: str | None) -> str:
    """A GFM prompt asking the user to approve one tool call.

    Bash shows the command; file tools show the (workspace-relative) path; other
    tools fall back to the generic argument summary — the same vocabulary as the
    inline tool lines so a prompt reads like the stream around it.
    """
    display = _TOOL_DISPLAY.get(tool, tool)
    header = f"🔐 Allow **{display}**?"
    if tool == "bash":
        command = tool_input.get("command", "")
        return f"{header}\n```\n{command}\n```" if command else header
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


def _make_transport(bot: Any, chat_id: int, thread_id: int | None) -> DraftTransport:
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

        async def send_message(self, text: str) -> None:
            try:
                await bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="MarkdownV2", **topic_kwargs
                )
            except Exception:
                # Malformed MarkdownV2 → resend without formatting rather than drop.
                logger.debug("MarkdownV2 send failed; falling back to plain text", exc_info=True)
                await bot.send_message(chat_id=chat_id, text=text, **topic_kwargs)

    return _Transport()


async def stream_reply(
    *,
    bot: Any,
    opencode: OpenCode,
    session_id: str,
    chat_id: int,
    thread_id: int | None,
    prompt: str,
    directory: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    pending: PendingApprovals | None = None,
    allowed_dirs: list[str] | None = None,
    draft_interval: float = DRAFT_INTERVAL_S,
) -> None:
    """Prompt the agent and stream its reply into the topic.

    ``directory``/``provider``/``model``/``effort`` come from the topic's
    resolved context (:class:`balam.router.ResolvedSession`) and are forwarded to
    the prompt so the agent runs in the right workspace with the right model.

    Subscribes to the event stream *before* prompting so no early deltas are
    missed, animates a draft as text grows, and finalizes into real message(s)
    on ``session.idle`` (or ``session.error``).

    When ``pending`` is given, ``permission.asked`` events are handled (ADR-0012):
    each is dispatched to a background task that recovers the call's tool/input
    from the tool-part cache, runs :func:`balam.approvals.decide` against
    ``allowed_dirs``, and either auto-replies to OpenCode or sends an inline
    keyboard and awaits the user's choice. Without ``pending`` the events are
    ignored (e.g. unit tests of the text/tool path).
    """
    transport = _make_transport(bot, chat_id, thread_id)
    draft = DraftSession(transport)
    topic_kwargs = thread_kwargs(thread_id)

    streaming = True

    async def flush_loop() -> None:
        while streaming:
            await asyncio.sleep(draft_interval)
            if not streaming:
                break
            await draft.flush_draft()

    flush_task = asyncio.create_task(flush_loop())

    assistant_message_ids: set[str] = set()
    # Interleaved text + tool fragments, keyed by part id / ``tool:<callID>``.
    stream_parts: dict[str, StreamPart] = {}
    # Latest ``(tool, input, status)`` per tool callID. Built here so the
    # interactive-approval step (#3) can recover a call's input by callID.
    tool_parts: dict[str, tuple[str, dict[str, Any], str | None]] = {}
    order = 0
    error_text: str | None = None
    stream_ready = asyncio.Event()
    dirs = allowed_dirs or ([directory] if directory else [])
    # Per-request approval tasks, so the SSE loop isn't blocked while the user
    # decides. Torn down with the consumer.
    permission_tasks: set[asyncio.Task[None]] = set()

    async def await_tool_input(call_id: str) -> tuple[str | None, dict[str, Any]]:
        """Recover a call's ``(tool, input)`` from the tool-part cache, briefly
        waiting for it: ``permission.asked`` can race ahead of the tool part that
        carries the input. Falls back to whatever is cached when the wait lapses.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 1.0
        while True:
            entry = tool_parts.get(call_id)
            if entry and entry[1]:
                return entry[0], entry[1]
            if loop.time() >= deadline:
                return (entry[0], entry[1]) if entry else (None, {})
            await asyncio.sleep(0.05)

    async def request_approval(
        request_id: str, category: str, tool: str, tool_input: dict[str, Any]
    ) -> None:
        """Ask the user via an inline keyboard, then reply to OpenCode. The
        callback handler resolves the future and updates the message; here we
        only translate the choice into a permission reply. ``category`` drives the
        keyboard (whether to offer "accept all edits"); ``tool`` is display-only."""
        assert pending is not None
        token, future = pending.register(session_id)
        gfm = _format_approval_request(tool, tool_input, directory)
        keyboard = _approval_keyboard(token, category)
        chunks = gfm_to_telegram(gfm)
        text = chunks[0] if chunks else f"🔐 Allow {tool}?"
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
                **topic_kwargs,
            )
        except Exception:
            logger.debug("approval keyboard MarkdownV2 send failed; retrying plain", exc_info=True)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🔐 Allow {tool}? (see request)",
                    reply_markup=keyboard,
                    **topic_kwargs,
                )
            except Exception:
                logger.exception("failed to send approval keyboard; denying")
                pending.discard(token)
                await opencode.reply_permission(
                    request_id, "reject", directory=directory, message="Could not prompt the user."
                )
                return
        try:
            choice = await future
        except asyncio.CancelledError:
            # Turn torn down (e.g. /cancel) before the user answered: unblock the
            # server so it isn't left waiting on a permission that will never come.
            await opencode.reply_permission(
                request_id, "reject", directory=directory, message="Cancelled."
            )
            raise
        finally:
            pending.discard(token)
        if choice is Choice.DENY:
            await opencode.reply_permission(
                request_id, "reject", directory=directory, message="Denied by the user."
            )
        else:
            await opencode.reply_permission(request_id, "once", directory=directory)

    async def handle_permission(request: dict[str, Any]) -> None:
        request_id = request.get("id")
        if not request_id:
            return
        category = request.get("permission") or ""
        metadata = request.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        tool_ref = request.get("tool")
        call_id = tool_ref.get("callID", "") if isinstance(tool_ref, dict) else ""
        cached_tool, tool_input = await await_tool_input(call_id) if call_id else (None, {})
        cwd = dirs[0] if dirs else None
        # Classify by OpenCode's permission category; take edit targets from the
        # permission metadata (authoritative for apply_patch) and reads from input.
        paths = request_target_paths(category, metadata, tool_input, cwd)
        verdict = decide(
            category,
            paths,
            allowed_dirs=dirs,
            accept_all_edits=pending.is_accept_all_edits(session_id) if pending else False,
        )
        if verdict is Verdict.ALLOW:
            await opencode.reply_permission(request_id, "once", directory=directory)
            return
        await request_approval(request_id, category, cached_tool or category, tool_input)

    async def consume() -> None:
        nonlocal order, error_text
        async for event in opencode.events(directory=directory, ready=stream_ready):
            etype = event.get("type")
            props = event.get("properties", {})

            if etype == "message.updated":
                info = props.get("info", {})
                if info.get("sessionID") == session_id and info.get("role") == "assistant":
                    assistant_message_ids.add(info.get("id"))

            elif etype == "message.part.updated":
                part = props.get("part", {})
                if part.get("sessionID") != session_id:
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    # Render only assistant text. Subscribing before prompting
                    # guarantees we see the assistant's message.updated before its
                    # parts, so this set is populated by the time they arrive.
                    if part.get("messageID") not in assistant_message_ids:
                        continue
                    part_id = part.get("id")
                    text = part.get("text", "")
                    if part_id in stream_parts:
                        stream_parts[part_id] = (stream_parts[part_id][0], "text", text)
                    else:
                        stream_parts[part_id] = (order, "text", text)
                        order += 1
                    draft.set_text(_join_stream(stream_parts))
                elif ptype == "tool":
                    call_id = part.get("callID")
                    if not call_id:
                        continue
                    state = part.get("state")
                    if not isinstance(state, dict):
                        continue
                    status = state.get("status")
                    tool = part.get("tool") or ""
                    raw_input = state.get("input")
                    tool_input = raw_input if isinstance(raw_input, dict) else {}
                    # Cache every update so #3's approval step can read the input.
                    tool_parts[call_id] = (tool, tool_input, status)
                    # Reserve a slot at the call's arrival position (so the tool
                    # line interleaves before any later text), but only render
                    # once the call finishes.
                    key = f"tool:{call_id}"
                    if key not in stream_parts:
                        stream_parts[key] = (order, "tool", "")
                        order += 1
                    if status in ("completed", "error"):
                        rendered = _render_tool_part(tool, tool_input, state, directory)
                        stream_parts[key] = (stream_parts[key][0], "tool", rendered)
                        draft.set_text(_join_stream(stream_parts))

            elif etype == "permission.asked":
                # ``props`` is the PermissionRequest. Handle in a child task so a
                # slow user decision doesn't stall the SSE loop (the session stays
                # busy — not idle — while a permission is pending).
                if pending is None or props.get("sessionID") != session_id:
                    continue
                ptask = asyncio.create_task(handle_permission(props))
                permission_tasks.add(ptask)
                ptask.add_done_callback(permission_tasks.discard)

            elif etype == "session.error" and props.get("sessionID") == session_id:
                error_text = _describe_error(props.get("error"))
                break

            elif etype == "session.idle" and props.get("sessionID") == session_id:
                break

    # Subscribe *before* prompting so no early deltas are missed (ADR-0010).
    consume_task = asyncio.create_task(consume())
    try:
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing", **topic_kwargs)
        except Exception:
            pass

        try:
            ready_task = asyncio.create_task(stream_ready.wait())
            await asyncio.wait({ready_task, consume_task}, return_when=asyncio.FIRST_COMPLETED)
            if consume_task.done():
                # Stream failed/closed before opening; surface it without prompting.
                ready_task.cancel()
                await consume_task
            else:
                await opencode.prompt(
                    session_id,
                    prompt,
                    directory=directory,
                    provider=provider,
                    model=model,
                    effort=effort,
                )
                await consume_task
        except Exception as exc:
            # A failed prompt or a broken event stream must still finalize a real
            # message (ADR-0010): fold the error into the reply instead of letting
            # it bubble out and skip finalize() below.
            logger.exception("streaming the reply failed")
            error_text = error_text or str(exc) or exc.__class__.__name__

        if error_text:
            base = _join_stream(stream_parts)
            prefix = f"{base}\n\n" if base.strip() else ""
            draft.set_text(f"{prefix}⚠️ {error_text}")
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
        await asyncio.gather(flush_task, consume_task, *permission_tasks, return_exceptions=True)

    # Replace the ephemeral draft with the real, persistent message(s) — always,
    # even on error, so accumulated text and any error notice are delivered.
    await draft.finalize()
