"""OpenCode implementation of :class:`~balam.agent.backend.AgentBackend` (ADR-0013).

Wraps the thin :class:`~balam.opencode.OpenCode` HTTP/SSE client and translates
its raw event stream into the normalized :mod:`balam.agent.events` vocabulary, so
the streamer never sees an OpenCode wire shape again. Session config (the
permission ruleset and MCP servers) is applied by the router at session creation,
so :meth:`run_turn` mostly forwards the prompt and re-shapes the events.

**Internal queue + driver task.** A turn cannot be a plain ``async for event:
yield translate(event)`` loop: OpenCode raises ``permission.asked`` *before* the
tool part that carries the call's input arrives, and recovering that input means
waiting for a later event on the same stream. So a *driver* task consumes the SSE
stream and pushes normalized events onto a queue; ``permission.asked`` spawns a
child task that waits for the tool input (off the hot path) and then enqueues the
:class:`~balam.agent.events.PermissionRequested`. :meth:`run_turn` yields from the
queue. The Claude SDK backend uses the same producer/consumer shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

from balam.agent.backend import TurnRequest
from balam.agent.events import (
    AgentEvent,
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
from balam.opencode import OpenCode
from balam.opencode_tools import Permission

logger = logging.getLogger(__name__)

#: Pushed onto the queue by the driver's ``finally`` to tell ``run_turn`` the
#: stream is exhausted. Events are dataclass instances, never ``None``.
_SENTINEL = None


def _is_answer_text_part(part: dict[str, Any]) -> bool:
    """Whether an OpenCode text part should be shown as the answer.

    OpenCode exposes model reasoning as its own ``type: "reasoning"`` part; this
    also rejects defensive legacy/future shapes where a text part is explicitly
    marked ignored or reasoning-like in metadata.
    """
    if part.get("type") != "text" or part.get("ignored") is True:
        return False
    metadata = part.get("metadata")
    if not isinstance(metadata, dict):
        return True
    return not (
        metadata.get("reasoning") is True
        or metadata.get("thinking") is True
        or metadata.get("kind") in {"reasoning", "thinking"}
        or metadata.get("type") in {"reasoning", "thinking"}
    )


def _is_reasoning_part(part: dict[str, Any]) -> bool:
    """Whether an OpenCode part is model reasoning/thinking text."""
    if part.get("type") == "reasoning":
        return True
    if part.get("type") != "text":
        return False
    metadata = part.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return (
        metadata.get("reasoning") is True
        or metadata.get("thinking") is True
        or metadata.get("kind") in {"reasoning", "thinking"}
        or metadata.get("type") in {"reasoning", "thinking"}
    )


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


def _retry_detail(status: dict[str, Any]) -> str | None:
    """The one-line detail behind a ``session.status`` retry, if any."""
    action = status.get("action") if isinstance(status.get("action"), dict) else None
    detail = ""
    if action and isinstance(action.get("message"), str):
        detail = action["message"]
    elif isinstance(status.get("message"), str):
        detail = status["message"]
    detail = detail.strip()
    return detail.splitlines()[0][:300] if detail else None


#: OpenCode's plan_exit tool asks its approval question with this exact text
#: (tool/plan.ts) — the worktree-relative plan path appears nowhere else.
_PLAN_QUESTION_RE = re.compile(r"Plan at (.+?) is complete")


def _plan_path_from_question(
    request: dict[str, Any],
    tool_parts: dict[str, tuple[str, dict[str, Any], str | None]],
    directory: str | None,
) -> str | None:
    """The plan file behind a ``plan_exit`` approval question, or ``None``.

    OpenCode's native plan agent finishes by calling ``plan_exit``, which asks
    "Plan at <path> is complete. …" through the question service. The request's
    ``tool.callID`` identifies the owning tool via the tool-part cache; the path
    itself only exists in the question text, so the regex extracts it. The path is
    worktree-relative, so it resolves against the context directory.
    """
    questions = request.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    first = questions[0] if isinstance(questions[0], dict) else {}
    match = _PLAN_QUESTION_RE.search(str(first.get("question") or ""))
    if match is None:
        return None
    tool = request.get("tool")
    if isinstance(tool, dict):
        cached = tool_parts.get(str(tool.get("callID") or ""))
        if cached is not None and cached[0] != "plan_exit":
            return None  # some other tool's question merely matched the regex
    rel = match.group(1).strip()
    if os.path.isabs(rel):
        return rel
    if directory is None:
        return None
    return os.path.normpath(os.path.join(directory, rel))


class OpenCodeBackend:
    """Drive the OpenCode server as an :class:`~balam.agent.backend.AgentBackend`."""

    def __init__(self, opencode: OpenCode) -> None:
        self._opencode = opencode

    async def wait_for_ready(self) -> None:
        await self._opencode.wait_for_ready()

    async def aclose(self) -> None:
        await self._opencode.aclose()

    async def session_exists(self, session_id: str, *, directory: str) -> bool:
        return await self._opencode.session_exists(session_id, directory=directory)

    async def abort(self, session_id: str, *, directory: str) -> None:
        await self._opencode.abort_session(session_id, directory=directory)

    async def reply_permission(
        self,
        request_id: str,
        *,
        allow: bool,
        message: str | None = None,
        directory: str | None = None,
    ) -> None:
        if allow:
            await self._opencode.reply_permission(request_id, "once", directory=directory)
        else:
            await self._opencode.reply_permission(
                request_id, "reject", directory=directory, message=message
            )

    async def reply_question(
        self, request_id: str, answers: list[list[str]], *, directory: str | None = None
    ) -> None:
        await self._opencode.reply_question(request_id, answers, directory=directory)

    async def reject_question(self, request_id: str, *, directory: str | None = None) -> None:
        await self._opencode.reject_question(request_id, directory=directory)

    async def run_turn(self, turn: TurnRequest) -> AsyncIterator[AgentEvent]:
        session_id = turn.session_id
        assert session_id is not None, "OpenCode sessions are created before the turn"
        directory = turn.directory

        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        # call_id -> (tool, input, status); lets a permission task recover input.
        tool_parts: dict[str, tuple[str, dict[str, Any], str | None]] = {}
        assistant_ids: set[str] = set()
        stream_ready = asyncio.Event()
        permission_tasks: set[asyncio.Task[None]] = set()

        async def await_tool_input(call_id: str) -> tuple[str | None, dict[str, Any]]:
            """Recover a call's ``(tool, input)`` from the cache, briefly waiting:
            ``permission.asked`` can race ahead of the tool part with the input."""
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 1.0
            while True:
                entry = tool_parts.get(call_id)
                if entry and entry[1]:
                    return entry[0], entry[1]
                if loop.time() >= deadline:
                    return (entry[0], entry[1]) if entry else (None, {})
                await asyncio.sleep(0.05)

        async def handle_permission(props: dict[str, Any]) -> None:
            request_id = props.get("id")
            if not request_id:
                return
            category = props.get("permission") or ""
            if category == Permission.QUESTION:
                # OpenCode raises a permission before its own question flow; it is
                # pre-approved by the ruleset, but answer defensively if it slips
                # through (the real gating happens via question.asked).
                await self._opencode.reply_permission(request_id, "once", directory=directory)
                return
            metadata = props.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            tool_ref = props.get("tool")
            call_id = tool_ref.get("callID", "") if isinstance(tool_ref, dict) else ""
            cached_tool, tool_input = await await_tool_input(call_id) if call_id else (None, {})
            await queue.put(
                PermissionRequested(
                    request_id=request_id,
                    category=category,
                    tool=cached_tool or category,
                    input=tool_input,
                    metadata=metadata,
                    call_id=call_id or None,
                )
            )

        async def driver() -> None:
            try:
                async for event in self._opencode.events(directory=directory, ready=stream_ready):
                    etype = event.get("type")
                    props = event.get("properties", {})

                    if etype == "message.updated":
                        info = props.get("info", {})
                        if info.get("sessionID") == session_id and info.get("role") == "assistant":
                            assistant_ids.add(info.get("id"))

                    elif etype == "message.part.updated":
                        part = props.get("part", {})
                        if part.get("sessionID") != session_id:
                            continue
                        ptype = part.get("type")
                        if _is_answer_text_part(part):
                            if part.get("messageID") not in assistant_ids:
                                continue
                            await queue.put(
                                TextUpdated(
                                    part_id=part.get("id"),
                                    text=part.get("text", ""),
                                    message_id=part.get("messageID"),
                                )
                            )
                        elif _is_reasoning_part(part):
                            if part.get("messageID") not in assistant_ids:
                                continue
                            await queue.put(
                                ReasoningUpdated(
                                    part_id=part.get("id"),
                                    text=part.get("text", ""),
                                    message_id=part.get("messageID"),
                                )
                            )
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
                            tool_parts[call_id] = (tool, tool_input, status)
                            await queue.put(
                                ToolUpdated(
                                    call_id=call_id,
                                    tool=tool,
                                    input=tool_input,
                                    status=status or "",
                                    output=state.get("output"),
                                    error=state.get("error"),
                                )
                            )

                    elif etype == "permission.asked":
                        if props.get("sessionID") != session_id:
                            continue
                        ptask = asyncio.create_task(handle_permission(props))
                        permission_tasks.add(ptask)
                        ptask.add_done_callback(permission_tasks.discard)

                    elif etype == "question.asked":
                        if props.get("sessionID") != session_id:
                            continue
                        request_id = props.get("id")
                        if not request_id:
                            continue
                        tool_ref = props.get("tool")
                        call_id = tool_ref.get("callID") if isinstance(tool_ref, dict) else None
                        raw_questions = props.get("questions")
                        questions = (
                            [q for q in raw_questions if isinstance(q, dict)]
                            if isinstance(raw_questions, list)
                            else []
                        )
                        await queue.put(
                            QuestionAsked(
                                request_id=request_id,
                                questions=questions,
                                call_id=call_id,
                                plan_path=_plan_path_from_question(props, tool_parts, directory),
                            )
                        )

                    elif etype == "session.status" and props.get("sessionID") == session_id:
                        status = props.get("status")
                        if isinstance(status, dict) and status.get("type") == "retry":
                            await queue.put(RetryNotice(detail=_retry_detail(status)))

                    elif etype == "session.error" and props.get("sessionID") == session_id:
                        await queue.put(TurnFailed(message=_describe_error(props.get("error"))))
                        return

                    elif etype == "session.idle" and props.get("sessionID") == session_id:
                        await queue.put(TurnFinished())
                        return
            except Exception as exc:  # a broken stream still ends the turn visibly
                logger.exception("OpenCode event stream failed")
                await queue.put(TurnFailed(message=str(exc) or exc.__class__.__name__))
            finally:
                await queue.put(_SENTINEL)

        driver_task = asyncio.create_task(driver())
        try:
            # Subscribe before prompting so no early deltas are missed (ADR-0010).
            ready_task = asyncio.create_task(stream_ready.wait())
            await asyncio.wait({ready_task, driver_task}, return_when=asyncio.FIRST_COMPLETED)
            ready_task.cancel()
            # Prompt iff the stream actually opened. (A fast/exhausted stream can
            # leave the driver "done" yet still have opened — gate on readiness,
            # not driver liveness; a stream that died before opening never set it.)
            if stream_ready.is_set():
                yield SessionStarted(session_id)
                try:
                    await self._opencode.prompt(
                        session_id,
                        turn.prompt,
                        directory=directory,
                        provider=turn.provider,
                        model=turn.model,
                        effort=turn.effort,
                        files=turn.files,
                        agent="plan" if turn.plan_mode else None,
                    )
                except Exception as exc:
                    logger.exception("OpenCode prompt failed")
                    yield TurnFailed(message=str(exc) or exc.__class__.__name__)
                    return
            while (event := await queue.get()) is not None:
                yield event
        finally:
            if not driver_task.done():
                driver_task.cancel()
            for ptask in list(permission_tasks):
                if not ptask.done():
                    ptask.cancel()
            await asyncio.gather(driver_task, *permission_tasks, return_exceptions=True)
