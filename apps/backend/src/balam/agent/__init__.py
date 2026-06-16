"""Pluggable agent backends (ADR-0013).

Balam can drive either the OpenCode server or the Claude Agent SDK, selected by
config (``AGENT_BACKEND``). Both implement the :class:`AgentBackend` protocol and
emit the same normalized :mod:`balam.agent.events` vocabulary, so the streamer,
router, and approval layers never touch a backend's native wire shapes.
"""

from __future__ import annotations

from balam.agent.backend import AgentBackend, TurnRequest
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
from balam.agent.opencode_backend import OpenCodeBackend

__all__ = [
    "AgentBackend",
    "AgentEvent",
    "OpenCodeBackend",
    "PermissionRequested",
    "QuestionAsked",
    "ReasoningUpdated",
    "RetryNotice",
    "SessionStarted",
    "TextUpdated",
    "ToolUpdated",
    "TurnFailed",
    "TurnFinished",
    "TurnRequest",
]
