"""OpenCodeBackend translates the raw OpenCode SSE stream into AgentEvents."""

import asyncio

from balam.agent.backend import TurnRequest
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
from balam.agent.opencode_backend import OpenCodeBackend

SID = "ses_test"
AID = "msg_assistant"


def _ev(etype: str, **props: object) -> dict[str, object]:
    return {"type": etype, "properties": props}


def _msg_updated(mid: str = AID, role: str = "assistant") -> dict[str, object]:
    return _ev("message.updated", info={"sessionID": SID, "role": role, "id": mid})


def _text(text: str, *, mid: str = AID, pid: str = "prt_1") -> dict[str, object]:
    return _ev(
        "message.part.updated",
        part={"type": "text", "sessionID": SID, "messageID": mid, "id": pid, "text": text},
    )


def _reasoning(text: str, *, mid: str = AID, pid: str = "prt_r") -> dict[str, object]:
    return _ev(
        "message.part.updated",
        part={"type": "reasoning", "sessionID": SID, "messageID": mid, "id": pid, "text": text},
    )


def _tool(call_id: str, tool: str, state: dict[str, object]) -> dict[str, object]:
    return _ev(
        "message.part.updated",
        part={
            "type": "tool",
            "sessionID": SID,
            "messageID": AID,
            "id": f"prt_{call_id}",
            "callID": call_id,
            "tool": tool,
            "state": state,
        },
    )


class FakeOpenCode:
    """Yields a scripted event list; records prompt/reply calls. ``"WAIT_REPLY"``
    in the script blocks the stream until a permission is answered, mirroring how
    OpenCode stays busy until then."""

    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.prompt_kwargs: dict[str, object] = {}
        self.replies: list[tuple[str, str, str | None]] = []
        self._replied = asyncio.Event()

    async def prompt(self, session_id: str, text: str, **kwargs: object) -> None:
        self.prompt_kwargs = kwargs

    async def reply_permission(
        self,
        request_id: str,
        reply: str,
        *,
        directory: str | None = None,
        message: str | None = None,
    ) -> None:
        self.replies.append((request_id, reply, message))
        self._replied.set()

    async def events(self, *, directory: str | None = None, ready: asyncio.Event | None = None):
        if ready is not None:
            ready.set()
        for event in self._events:
            if event == "WAIT_REPLY":
                await self._replied.wait()
                continue
            yield event


def _turn(**kwargs: object) -> TurnRequest:
    return TurnRequest(directory="/ws", prompt="hello", session_id=SID, **kwargs)


async def _collect(backend: OpenCodeBackend, turn: TurnRequest) -> list[object]:
    return [event async for event in backend.run_turn(turn)]


async def test_translates_text_reasoning_tool_and_idle() -> None:
    fake = FakeOpenCode(
        [
            _msg_updated(),
            _reasoning("thinking…"),
            _text("hello "),
            _text("hello world"),
            _tool("call_1", "read", {"status": "completed", "input": {"filePath": "/ws/a.py"}}),
            _ev("session.idle", sessionID=SID),
        ]
    )
    events = await _collect(OpenCodeBackend(fake), _turn())

    assert isinstance(events[0], SessionStarted) and events[0].session_id == SID
    assert isinstance(events[-1], TurnFinished)
    texts = [e for e in events if isinstance(e, TextUpdated)]
    assert [t.text for t in texts] == ["hello ", "hello world"]
    assert all(t.message_id == AID for t in texts)
    reasoning = [e for e in events if isinstance(e, ReasoningUpdated)]
    assert [r.text for r in reasoning] == ["thinking…"]
    tools = [e for e in events if isinstance(e, ToolUpdated)]
    assert tools[0].tool == "read" and tools[0].status == "completed"
    assert tools[0].input == {"filePath": "/ws/a.py"}
    # Default turn forwards no plan agent.
    assert fake.prompt_kwargs["agent"] is None


async def test_text_before_assistant_message_is_dropped() -> None:
    # A text part whose messageID is not a known assistant message is ignored.
    fake = FakeOpenCode([_text("orphan", mid="msg_unknown"), _ev("session.idle", sessionID=SID)])
    events = await _collect(OpenCodeBackend(fake), _turn())
    assert not [e for e in events if isinstance(e, TextUpdated)]


async def test_plan_mode_forwards_plan_agent() -> None:
    fake = FakeOpenCode([_ev("session.idle", sessionID=SID)])
    await _collect(OpenCodeBackend(fake), _turn(plan_mode=True))
    assert fake.prompt_kwargs["agent"] == "plan"


async def test_session_error_becomes_turn_failed() -> None:
    fake = FakeOpenCode(
        [_ev("session.error", sessionID=SID, error={"name": "Boom", "data": {"message": "bad"}})]
    )
    events = await _collect(OpenCodeBackend(fake), _turn())
    failed = [e for e in events if isinstance(e, TurnFailed)]
    assert failed and failed[0].message == "Boom: bad"


async def test_retry_status_becomes_retry_notice() -> None:
    fake = FakeOpenCode(
        [
            _ev(
                "session.status",
                sessionID=SID,
                status={"type": "retry", "message": "rate limited"},
            ),
            _ev("session.idle", sessionID=SID),
        ]
    )
    events = await _collect(OpenCodeBackend(fake), _turn())
    notices = [e for e in events if isinstance(e, RetryNotice)]
    assert notices and notices[0].detail == "rate limited"


async def test_question_carries_plan_path() -> None:
    fake = FakeOpenCode(
        [
            _tool("call_plan", "plan_exit", {"status": "running", "input": {}}),
            _ev(
                "question.asked",
                id="q1",
                sessionID=SID,
                tool={"callID": "call_plan"},
                questions=[{"question": "Plan at .opencode/plans/p.md is complete. Build?"}],
            ),
            _ev("session.idle", sessionID=SID),
        ]
    )
    events = await _collect(OpenCodeBackend(fake), _turn())
    questions = [e for e in events if isinstance(e, QuestionAsked)]
    assert questions and questions[0].plan_path == "/ws/.opencode/plans/p.md"


async def test_permission_recovers_tool_input_and_reply_maps() -> None:
    fake = FakeOpenCode(
        [
            _tool("call_x", "bash", {"status": "running", "input": {"command": "ls"}}),
            _ev(
                "permission.asked",
                id="perm1",
                sessionID=SID,
                permission="bash",
                tool={"callID": "call_x"},
                metadata={},
            ),
            "WAIT_REPLY",
            _ev("session.idle", sessionID=SID),
        ]
    )
    backend = OpenCodeBackend(fake)
    seen: list[object] = []
    async for event in backend.run_turn(_turn()):
        seen.append(event)
        if isinstance(event, PermissionRequested):
            assert event.category == "bash"
            assert event.tool == "bash"
            assert event.input == {"command": "ls"}
            await backend.reply_permission(event.request_id, allow=False, message="no")
    assert ("perm1", "reject", "no") in fake.replies
    assert any(isinstance(e, TurnFinished) for e in seen)
