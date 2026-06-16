"""Normalized turn events both agent backends emit (ADR-0013).

The streamer consumes *these* events, never a backend's native wire shapes. The
vocabulary is adapted from OpenCode's ``LLMEvent`` taxonomy
(``packages/llm/src/schema/events.ts``), trimmed to what Balam's UI actually
renders, so the OpenCode backend's translation is near-identity and the Claude
Agent SDK backend maps onto the same set.

**Text/reasoning use replace semantics, keyed by ``part_id``.** OpenCode reports
the full text of a part on every ``message.part.updated``; the streamer is built
to *replace* a part's text on each update (see ``DraftSession.set_text`` /
``_join_stream``). So :class:`TextUpdated` / :class:`ReasoningUpdated` carry the
**full text of the part so far**, not a delta. The OpenCode backend forwards its
text directly; the SDK backend accumulates per-content-block deltas and emits the
running total. ``message_id`` groups parts into assistant "steps" so the streamer
can demote an earlier step's prose to progress narration when a new step begins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SessionStarted:
    """The backend's real session id for this turn.

    OpenCode creates the session up front (so this echoes the known id); the SDK
    only reveals it once the first turn's stream opens, so the streamer persists
    it via the store when it arrives (see :class:`~balam.router.Router`).
    """

    session_id: str


@dataclass(frozen=True, slots=True)
class TextUpdated:
    """Assistant answer text (the full text of ``part_id`` so far)."""

    part_id: str
    text: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReasoningUpdated:
    """Model reasoning/thinking text (the full text of ``part_id`` so far).

    Coarse on the SDK backend: extended thinking is not streamed token-by-token,
    so this may arrive only once near the end of a step.
    """

    part_id: str
    text: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolUpdated:
    """A tool call's latest state, keyed by ``call_id``.

    ``status`` is normalized to ``"pending"`` | ``"running"`` | ``"completed"`` |
    ``"error"``. ``output``/``error`` carry the backend's raw result payloads
    (string, or OpenCode's list-of-text-blocks); the streamer flattens them for
    display and the input is cached so a concurrent permission prompt can recover
    it by ``call_id``.
    """

    call_id: str
    tool: str
    input: dict[str, Any]
    status: str
    output: Any = None
    error: Any = None


@dataclass(frozen=True, slots=True)
class PermissionRequested:
    """A tool call needs the human's approval.

    Emitted only when the backend cannot pre-approve the call natively (OpenCode:
    no matching native ``allow`` rule; SDK: not in ``allowed_tools``). The
    streamer runs the symlink-safe directory-boundary policy
    (:func:`balam.approvals.decide`) and either auto-replies or shows the inline
    keyboard, then answers via :meth:`AgentBackend.reply_permission`.

    ``category`` is the normalized permission category (``read`` / ``edit`` /
    ``bash`` / …) the policy keys on; ``metadata`` carries extras such as the
    authoritative ``files`` list for a multi-file edit.
    """

    request_id: str
    category: str
    tool: str
    input: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class QuestionAsked:
    """The agent asked the user a structured question.

    OpenCode's ``question`` tool, or the SDK's ``ExitPlanMode`` rendered as a
    Yes/No plan-approval question. ``questions`` follows the OpenCode shape the
    streamer already renders: a list of ``{question, header, options:[{label,
    description}], multiple, custom}``. ``call_id`` links back to the owning tool
    (used to locate a freshly written plan file).
    """

    request_id: str
    questions: list[dict[str, Any]]
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetryNotice:
    """The turn is being retried internally (e.g. a provider rate limit), so the
    long silence is explained to the user once per turn."""

    message: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """The turn ended in an error; ``message`` is a readable one-liner."""

    message: str


@dataclass(frozen=True, slots=True)
class TurnFinished:
    """The turn completed normally (the agent went idle)."""


#: Every event a backend may yield from :meth:`AgentBackend.run_turn`.
AgentEvent = (
    SessionStarted
    | TextUpdated
    | ReasoningUpdated
    | ToolUpdated
    | PermissionRequested
    | QuestionAsked
    | RetryNotice
    | TurnFailed
    | TurnFinished
)
