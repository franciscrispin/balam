"""Run one agent turn with no topic behind it (ADR-0016 amendment).

A headless scheduled run is for jobs whose result lands somewhere else: a sync
that writes a spreadsheet and a log line another schedule reports. It drives the
same :class:`~balam.agent.backend.AgentBackend` a topic's turn does, but renders
nothing. The event stream is consumed here instead of by the streamer, and the
run comes back as one :class:`HeadlessResult` for the caller to report.

The permission policy is the streamer's unattended one, unchanged: the context's
``allowed_tools`` pre-approve inside the backend, an in-workspace read is
allowed, and everything else is refused rather than awaited, since there is no
keyboard and nobody to tap it. Questions are rejected the same way. Each refusal
is recorded, because a headless run that was refused something did not do its
whole job and must be reported as a failure, not as a quiet success.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from balam.agent.backend import AgentBackend, TurnRequest
from balam.agent.events import (
    PermissionRequested,
    QuestionAsked,
    TextUpdated,
    TurnFailed,
    TurnFinished,
)
from balam.approvals import Verdict, decide, request_target_paths
from balam.router import ResolvedSession

logger = logging.getLogger(__name__)

#: An answer that starts with this is a finding the owner must see, so it is
#: reported like a failure even though the run itself went fine. It is how a
#: check (is the WhatsApp bridge still linked?) breaks the silence on success.
ALERT_PREFIX = "ALERT:"

#: How long a headless run may take before it is aborted and reported. Generous:
#: a sync over a few dozen sheets takes minutes, and a hung run must still end.
HEADLESS_TIMEOUT_S = 60 * 60

#: What the agent is told when an unattended run refuses a tool call.
_DENIED = (
    "Denied: this is a scheduled, unattended run. Only reads inside the workspace "
    "and the context's allowed tools are permitted. Report what you needed instead "
    "of retrying."
)


@dataclass
class HeadlessResult:
    """How a headless run ended. ``ok`` only when it finished, without an error,
    and was refused nothing."""

    #: The final answer's text, for the log and for a manual ``/schedule run``.
    answer: str = ""
    #: Why the turn failed (backend error, exception, timeout), or ``None``.
    error: str | None = None
    #: One line per refused tool call or rejected question.
    blocked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.blocked

    @property
    def alert(self) -> bool:
        """The agent finished and asked to be heard (:data:`ALERT_PREFIX`)."""
        return self.answer.lstrip().startswith(ALERT_PREFIX)


async def run_headless(
    backend: AgentBackend,
    resolved: ResolvedSession,
    prompt: str,
    *,
    timeout: float = HEADLESS_TIMEOUT_S,
) -> HeadlessResult:
    """Run ``prompt`` once in ``resolved``'s context and return how it went.

    Never raises for a turn that goes wrong: a backend error, an exception and a
    timeout all come back as :attr:`HeadlessResult.error`, so the caller has one
    place to report from.
    """
    result = HeadlessResult()
    dirs = [resolved.directory, *resolved.additional_directories]
    turn = TurnRequest(
        directory=resolved.directory,
        prompt=prompt,
        session_id=resolved.session_id,
        provider=resolved.provider,
        model=resolved.model,
        effort=resolved.effort,
        allowed_tools=list(resolved.allowed_tools),
        additional_directories=list(resolved.additional_directories),
        mcp=dict(resolved.mcp),
    )
    # Answer text by part, remembering which assistant message each belongs to:
    # the agent opens a new message per step, and only the last one's text is
    # the answer (its earlier "I'll check…" narration is not).
    parts: dict[str, tuple[str | None, str]] = {}
    last_message: list[str | None] = [None]
    session_id: list[str | None] = [resolved.session_id]

    async def answer_permission(request: PermissionRequested) -> None:
        paths = request_target_paths(request.category, request.metadata, request.input, dirs[0])
        verdict = decide(
            request.category, paths, allowed_dirs=dirs, accept_all_edits=False, unattended=True
        )
        if verdict is Verdict.ALLOW:
            await backend.reply_permission(
                request.request_id, allow=True, directory=resolved.directory
            )
            return
        result.blocked.append(f"denied {request.tool}")
        await backend.reply_permission(
            request.request_id, allow=False, message=_DENIED, directory=resolved.directory
        )

    async def consume() -> None:
        async for event in backend.run_turn(turn):
            if isinstance(event, TextUpdated):
                parts[event.part_id] = (event.message_id, event.text)
                last_message[0] = event.message_id
            elif isinstance(event, PermissionRequested):
                # Answered inline: the backend is blocked on this reply anyway,
                # and with no keyboard there is nothing to wait for.
                await answer_permission(event)
            elif isinstance(event, QuestionAsked):
                result.blocked.append("skipped a question")
                await backend.reject_question(event.request_id, directory=resolved.directory)
            elif isinstance(event, TurnFailed):
                result.error = event.message
                break
            elif isinstance(event, TurnFinished):
                break
            sid = getattr(event, "session_id", None)
            if isinstance(sid, str) and sid:
                session_id[0] = sid

    try:
        await asyncio.wait_for(consume(), timeout=timeout)
    except TimeoutError:
        result.error = f"timed out after {int(timeout // 60)} min"
        if session_id[0]:
            try:
                await backend.abort(session_id[0], directory=resolved.directory)
            except Exception:
                logger.debug("failed to abort a timed-out headless run", exc_info=True)
    except Exception as exc:
        logger.exception("headless run failed")
        result.error = str(exc) or exc.__class__.__name__

    result.answer = "\n\n".join(
        text for message_id, text in parts.values() if message_id == last_message[0]
    ).strip()
    return result
