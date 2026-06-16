"""ClaudeSdkBackend translates SDK messages into AgentEvents (ADR-0013).

Drives turns through an injected fake ``query_fn`` so no real ``claude``
subprocess is spawned.
"""

from types import SimpleNamespace

from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from balam.agent.backend import TurnRequest
from balam.agent.claude_sdk_backend import ClaudeSdkBackend
from balam.agent.events import (
    PermissionRequested,
    SessionStarted,
    TextUpdated,
    ToolUpdated,
    TurnFailed,
    TurnFinished,
)

SID = "ses_sdk"


def _init() -> SystemMessage:
    return SystemMessage(subtype="init", data={"session_id": SID})


def _stream(event: dict) -> StreamEvent:
    return StreamEvent(uuid="u", session_id=SID, event=event, parent_tool_use_id=None)


def _result(*, is_error: bool = False, result: str | None = None) -> ResultMessage:
    return ResultMessage(
        subtype="error" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id=SID,
        result=result,
    )


def _turn(**kwargs) -> TurnRequest:
    return TurnRequest(directory="/ws", prompt="hi", **kwargs)


def _fake_query(messages: list):
    async def gen(*, prompt, options):
        for message in messages:
            yield message

    return gen


async def _collect(backend: ClaudeSdkBackend, turn: TurnRequest) -> list:
    return [event async for event in backend.run_turn(turn)]


async def test_streams_text_and_finishes() -> None:
    messages = [
        _init(),
        _stream({"type": "message_start", "message": {"id": "m1"}}),
        _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello "},
            }
        ),
        _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "world"},
            }
        ),
        _result(),
    ]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())

    assert isinstance(events[0], SessionStarted) and events[0].session_id == SID
    assert isinstance(events[-1], TurnFinished)
    texts = [e for e in events if isinstance(e, TextUpdated)]
    assert [t.text for t in texts] == ["hello ", "hello world"]
    assert all(t.part_id == "m1:0" and t.message_id == "m1" for t in texts)


async def test_tool_call_then_result() -> None:
    messages = [
        _init(),
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
            model="claude",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="a\nb", is_error=False)]),
        _result(),
    ]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())
    tools = [e for e in events if isinstance(e, ToolUpdated)]
    assert tools[0].status == "running" and tools[0].tool == "bash"
    assert tools[-1].status == "completed" and tools[-1].output == "a\nb"
    assert tools[-1].input == {"command": "ls"}


async def test_file_path_is_normalized_to_camelcase() -> None:
    messages = [
        _init(),
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "/ws/a.py"})],
            model="claude",
        ),
        _result(),
    ]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())
    running = next(e for e in events if isinstance(e, ToolUpdated))
    assert running.input["filePath"] == "/ws/a.py"


async def test_result_error_becomes_turn_failed() -> None:
    messages = [_init(), _result(is_error=True, result="boom")]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())
    failed = [e for e in events if isinstance(e, TurnFailed)]
    assert failed and failed[0].message == "boom"


async def test_permission_request_and_deny_reply() -> None:
    captured: list = []

    def query_fn(*, prompt, options):
        async def gen():
            yield _init()
            ctx = SimpleNamespace(tool_use_id="t1")
            captured.append(await options.can_use_tool("Bash", {"command": "rm -rf /"}, ctx))
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(query_fn=query_fn)
    seen: list = []
    async for event in backend.run_turn(_turn()):
        seen.append(event)
        if isinstance(event, PermissionRequested):
            assert event.category == "bash"
            assert event.tool == "bash"
            assert event.input == {"command": "rm -rf /"}
            await backend.reply_permission(event.request_id, allow=False, message="no")

    assert isinstance(captured[0], PermissionResultDeny)
    assert captured[0].message == "no"
    assert any(isinstance(e, TurnFinished) for e in seen)


async def test_permission_allow_reply() -> None:
    captured: list = []

    def query_fn(*, prompt, options):
        async def gen():
            yield _init()
            ctx = SimpleNamespace(tool_use_id="t1")
            captured.append(await options.can_use_tool("Edit", {"file_path": "/ws/a.py"}, ctx))
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(query_fn=query_fn)
    async for event in backend.run_turn(_turn()):
        if isinstance(event, PermissionRequested):
            # Edit maps to the "edit" category and carries the file in metadata.
            assert event.category == "edit"
            assert event.metadata == {"files": [{"filePath": "/ws/a.py"}]}
            await backend.reply_permission(event.request_id, allow=True)

    assert isinstance(captured[0], PermissionResultAllow)


async def test_resume_and_model_effort_passed_to_options() -> None:
    seen_options: list = []

    def query_fn(*, prompt, options):
        seen_options.append(options)

        async def gen():
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(api_key="sk-x", query_fn=query_fn)
    await _collect(
        backend,
        _turn(session_id="ses_prev", model="claude-opus-4-8", effort="high"),
    )
    opts = seen_options[0]
    assert opts.resume == "ses_prev"
    assert opts.model == "claude-opus-4-8"
    assert opts.effort == "high"
    assert opts.cwd == "/ws"
    assert opts.env.get("ANTHROPIC_API_KEY") == "sk-x"
