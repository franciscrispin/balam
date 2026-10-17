"""Headless runs (ADR-0016 amendment): one turn, no topic, unattended policy."""

import asyncio
from typing import Any

from balam.agent.events import (
    PermissionRequested,
    QuestionAsked,
    SessionStarted,
    TextUpdated,
    TurnFailed,
    TurnFinished,
)
from balam.headless import run_headless
from balam.router import ResolvedSession

WORK = "/work/jambo"


def _resolved(**overrides: Any) -> ResolvedSession:
    fields: dict[str, Any] = {
        "session_id": None,
        "context_name": "jambo",
        "directory": WORK,
        "provider": None,
        "model": "claude-opus-5-5",
        "effort": None,
        "additional_directories": [],
        "allowed_tools": ["Bash"],
        "mcp": {},
    }
    fields.update(overrides)
    return ResolvedSession(**fields)


class _ScriptedBackend:
    """Yields a fixed event list; records every reply the runner sends back."""

    supports_streaming_input = True

    def __init__(self, events: list[Any], *, hang: bool = False) -> None:
        self._events = events
        self._hang = hang
        self.turns: list[Any] = []
        self.permissions: list[tuple[str, bool, str | None]] = []
        self.rejected: list[str] = []
        self.aborted: list[str] = []

    async def run_turn(self, turn: Any):
        self.turns.append(turn)
        for event in self._events:
            yield event
        if self._hang:
            await asyncio.sleep(3600)

    async def reply_permission(
        self, request_id: str, *, allow: bool, message: str | None = None, directory=None
    ) -> None:
        self.permissions.append((request_id, allow, message))

    async def reject_question(self, request_id: str, *, directory=None) -> None:
        self.rejected.append(request_id)

    async def abort(self, session_id: str, *, directory: str) -> None:
        self.aborted.append(session_id)


async def test_a_clean_run_is_ok_and_keeps_only_the_last_message_as_the_answer() -> None:
    backend = _ScriptedBackend(
        [
            SessionStarted(session_id="ses_1"),
            TextUpdated(part_id="p1", text="I'll check the sheets.", message_id="m1"),
            TextUpdated(part_id="p2", text="Filled 4 cells on BT3.", message_id="m2"),
            TurnFinished(),
        ]
    )
    result = await run_headless(backend, _resolved(), "sync")

    assert result.ok
    assert result.answer == "Filled 4 cells on BT3."
    # The turn ran in the context, with its tools and no topic identity.
    turn = backend.turns[0]
    assert (turn.directory, turn.prompt, turn.allowed_tools) == (WORK, "sync", ["Bash"])
    assert turn.chat_id is None and turn.thread_id is None


async def test_an_in_workspace_read_is_allowed() -> None:
    backend = _ScriptedBackend(
        [
            PermissionRequested(
                request_id="r1", category="read", tool="read", input={"filePath": f"{WORK}/a"}
            ),
            TurnFinished(),
        ]
    )
    result = await run_headless(backend, _resolved(), "p")
    assert result.ok
    assert backend.permissions == [("r1", True, None)]


async def test_anything_else_is_refused_and_makes_the_run_fail() -> None:
    # Nobody can tap a keyboard, so a request is refused rather than awaited, and
    # the run is reported: it did not do its whole job.
    backend = _ScriptedBackend(
        [
            PermissionRequested(
                request_id="r1", category="edit", tool="write", input={"filePath": f"{WORK}/x"}
            ),
            QuestionAsked(request_id="q1", questions=[]),
            TurnFinished(),
        ]
    )
    result = await run_headless(backend, _resolved(), "p")

    assert not result.ok
    assert result.blocked == ["denied write", "skipped a question"]
    assert backend.permissions[0][:2] == ("r1", False)
    assert "unattended" in (backend.permissions[0][2] or "")
    assert backend.rejected == ["q1"]


async def test_a_failed_turn_carries_its_error() -> None:
    backend = _ScriptedBackend([TurnFailed(message="rate limited")])
    result = await run_headless(backend, _resolved(), "p")
    assert not result.ok
    assert result.error == "rate limited"


async def test_an_exception_is_a_failure_not_a_crash() -> None:
    class _Exploding(_ScriptedBackend):
        async def run_turn(self, turn: Any):
            raise RuntimeError("cli missing")
            yield  # pragma: no cover

    result = await run_headless(_Exploding([]), _resolved(), "p")
    assert result.error == "cli missing"


async def test_a_hung_run_times_out_and_is_aborted() -> None:
    backend = _ScriptedBackend([SessionStarted(session_id="ses_9")], hang=True)
    result = await run_headless(backend, _resolved(), "p", timeout=0.05)
    assert not result.ok
    assert "timed out" in (result.error or "")
    assert backend.aborted == ["ses_9"]
