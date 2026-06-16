"""The :class:`AgentBackend` protocol both agent runtimes implement (ADR-0013).

One forum topic ↔ one agent session. The streamer drives a turn through
:meth:`AgentBackend.run_turn`, consuming the normalized :mod:`balam.agent.events`
stream and answering permission/question prompts via the reply methods. Two
implementations satisfy this contract:

* :class:`balam.agent.opencode_backend.OpenCodeBackend` — wraps the long-lived
  OpenCode server (HTTP/SSE); session config (permissions, MCP) is applied at
  session creation by the router, so ``run_turn`` mostly forwards the prompt.
* :class:`balam.agent.claude_sdk_backend.ClaudeSdkBackend` — drives the Claude
  Agent SDK with a fresh stateless ``query(resume=…)`` per turn, so *every* turn
  re-supplies the context's capabilities; that's why :class:`TurnRequest` carries
  the full context (``allowed_tools`` / ``additional_directories`` / ``mcp``), not
  just the prompt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from balam.agent.events import AgentEvent
from balam.attachments import PromptFile


@dataclass
class TurnRequest:
    """Everything a backend needs to run one turn of a topic's session.

    ``session_id`` is ``None`` for a session's first turn on a backend that mints
    the id lazily (the SDK); the real id arrives as a
    :class:`~balam.agent.events.SessionStarted` event. ``plan_mode`` is the
    normalized intent — the OpenCode backend maps it to ``agent="plan"``, the SDK
    backend to ``permission_mode="plan"``.

    The context-capability fields (``allowed_tools`` / ``additional_directories``
    / ``mcp``) and the per-topic identity (``chat_id`` / ``thread_id``, used to
    bind the ``send_file`` tool) let a stateless backend configure each ``query``;
    the OpenCode backend ignores them here because the router already applied them
    at session creation.
    """

    directory: str
    prompt: str
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    files: list[PromptFile] | None = None
    plan_mode: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    additional_directories: list[str] = field(default_factory=list)
    mcp: dict[str, Any] = field(default_factory=dict)
    chat_id: int | None = None
    thread_id: int | None = None


@runtime_checkable
class AgentBackend(Protocol):
    """A coding-agent runtime Balam can drive (OpenCode or the Claude Agent SDK)."""

    async def wait_for_ready(self) -> None:
        """Block until the backend can serve traffic (or raise on misconfig)."""
        ...

    async def aclose(self) -> None:
        """Release any held resources (HTTP clients, subprocesses)."""
        ...

    async def session_exists(self, session_id: str, *, directory: str) -> bool:
        """Whether a previously mapped session is still resumable. A ``False``
        tells the router to recreate the session rather than drop the message."""
        ...

    def run_turn(self, turn: TurnRequest) -> AsyncIterator[AgentEvent]:
        """Run one turn, yielding the normalized event stream.

        The backend subscribes/streams and prompts internally; consuming the
        iterator to exhaustion (or breaking out of it) ends the turn. Permission
        and question prompts surface as events and are answered out-of-band via
        the reply methods below while iteration continues.
        """
        ...

    async def reply_permission(
        self,
        request_id: str,
        *,
        allow: bool,
        message: str | None = None,
        directory: str | None = None,
    ) -> None:
        """Answer a :class:`~balam.agent.events.PermissionRequested`. ``allow``
        runs the call; otherwise it is rejected with an optional ``message`` for
        the agent. Best-effort — a failure must never tear the turn down."""
        ...

    async def reply_question(
        self, request_id: str, answers: list[list[str]], *, directory: str | None = None
    ) -> None:
        """Answer a :class:`~balam.agent.events.QuestionAsked` (one list of
        selected option labels per question)."""
        ...

    async def reject_question(self, request_id: str, *, directory: str | None = None) -> None:
        """Reject a :class:`~balam.agent.events.QuestionAsked`."""
        ...

    async def abort(self, session_id: str, *, directory: str) -> None:
        """Cancel the turn running in ``session_id`` (best-effort)."""
        ...
