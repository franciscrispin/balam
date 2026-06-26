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
from balam.agent.claude_sdk_backend import ClaudeSdkBackend, _category, coerce_sdk_mcp_config
from balam.agent.events import (
    PermissionRequested,
    QuestionAsked,
    SessionStarted,
    TextUpdated,
    ToolUpdated,
    TurnFailed,
    TurnFinished,
)
from balam.agent_tools import AgentTool

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


async def test_plan_mode_sets_permission_mode() -> None:
    seen_options: list = []

    def query_fn(*, prompt, options):
        seen_options.append(options)

        async def gen():
            yield _result()

        return gen()

    await _collect(ClaudeSdkBackend(query_fn=query_fn), _turn(plan_mode=True))
    assert seen_options[0].permission_mode == "plan"


async def test_exit_plan_mode_becomes_plan_question_yes_allows() -> None:
    captured: list = []

    def query_fn(*, prompt, options):
        async def gen():
            yield _init()
            ctx = SimpleNamespace(tool_use_id="t1")
            captured.append(await options.can_use_tool("ExitPlanMode", {"plan": "# Do X"}, ctx))
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(query_fn=query_fn)
    async for event in backend.run_turn(_turn(plan_mode=True)):
        if isinstance(event, QuestionAsked):
            assert event.plan_text == "# Do X"
            assert event.questions[0]["options"] == [{"label": "Yes"}, {"label": "No"}]
            await backend.reply_question(event.request_id, [["Yes"]])

    assert isinstance(captured[0], PermissionResultAllow)


async def test_exit_plan_mode_no_denies() -> None:
    captured: list = []

    def query_fn(*, prompt, options):
        async def gen():
            yield _init()
            ctx = SimpleNamespace(tool_use_id="t1")
            captured.append(await options.can_use_tool("ExitPlanMode", {"plan": "# Do X"}, ctx))
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(query_fn=query_fn)
    async for event in backend.run_turn(_turn(plan_mode=True)):
        if isinstance(event, QuestionAsked):
            await backend.reply_question(event.request_id, [["No"]])

    assert isinstance(captured[0], PermissionResultDeny)


async def test_ask_user_question_becomes_question_and_injects_answers() -> None:
    # AskUserQuestion must not bug the human with a tool-approval prompt; it is
    # surfaced as a structured question, and the selection is fed back to the tool
    # via updated_input.answers (keyed by question text), not a bare allow.
    captured: list = []

    def query_fn(*, prompt, options):
        async def gen():
            yield _init()
            ctx = SimpleNamespace(tool_use_id="t1")
            captured.append(
                await options.can_use_tool(
                    "AskUserQuestion",
                    {
                        "questions": [
                            {
                                "question": "Which DB?",
                                "header": "DB",
                                "options": [
                                    {"label": "Postgres", "description": "relational"},
                                    {"label": "SQLite", "description": "embedded"},
                                ],
                                "multiSelect": False,
                            }
                        ]
                    },
                    ctx,
                )
            )
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(query_fn=query_fn)
    requests: list = []
    async for event in backend.run_turn(_turn()):
        if isinstance(event, PermissionRequested):
            requests.append(event)
        if isinstance(event, QuestionAsked):
            assert event.questions[0]["question"] == "Which DB?"
            assert event.questions[0]["multiple"] is False
            assert event.questions[0]["options"][0]["label"] == "Postgres"
            await backend.reply_question(event.request_id, [["Postgres"]])

    assert requests == []  # never shown as a tool-approval prompt
    result = captured[0]
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input["answers"] == {"Which DB?": "Postgres"}


async def test_ask_user_question_multiselect_comma_joins_and_decline_denies() -> None:
    captured: list = []

    def query_fn(*, prompt, options):
        async def gen():
            yield _init()
            ctx = SimpleNamespace(tool_use_id="t1")
            captured.append(
                await options.can_use_tool(
                    "AskUserQuestion",
                    {
                        "questions": [
                            {
                                "question": "Pick features",
                                "header": "Feat",
                                "options": [
                                    {"label": "A", "description": ""},
                                    {"label": "B", "description": ""},
                                ],
                                "multiSelect": True,
                            }
                        ]
                    },
                    ctx,
                )
            )
            ctx2 = SimpleNamespace(tool_use_id="t2")
            captured.append(
                await options.can_use_tool(
                    "AskUserQuestion",
                    {"questions": [{"question": "Q2", "header": "h", "options": [{"label": "x"}]}]},
                    ctx2,
                )
            )
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(query_fn=query_fn)
    seen = 0
    async for event in backend.run_turn(_turn()):
        if isinstance(event, QuestionAsked):
            seen += 1
            if seen == 1:
                assert event.questions[0]["multiple"] is True
                await backend.reply_question(event.request_id, [["A", "B"]])
            else:
                await backend.reject_question(event.request_id)

    assert isinstance(captured[0], PermissionResultAllow)
    assert captured[0].updated_input["answers"] == {"Pick features": "A, B"}
    assert isinstance(captured[1], PermissionResultDeny)


def test_category_collapses_mcp_name_to_ruleset_form() -> None:
    # An MCP tool must collapse to the OpenCode ``server_tool`` wire form so it
    # matches a ``build_ruleset`` rule (parse_allowed_tool collapses entries the
    # same way); the qualified ``mcp__server__tool`` name would never match.
    assert _category("mcp__google_calendar__list-events") == "google_calendar_list-events"
    assert _category("mcp__github__create_issue") == "github_create_issue"
    # Non-MCP tools keep their existing category mapping.
    assert _category("Read") == "read"
    assert _category("Bash") == "bash"


def test_mcp_wildcard_allow_pre_approves_via_evaluate() -> None:
    from balam.contexts import ContextConfig
    from balam.permissions import build_ruleset, evaluate

    ctx = ContextConfig(
        directory="/tmp/ws", description="x", allowed_tools=["mcp__google_calendar__*"]
    )
    ruleset = build_ruleset(ctx)
    assert evaluate(_category("mcp__google_calendar__list-events"), "*", ruleset) == "allow"
    # An unrelated server stays gated.
    assert evaluate(_category("mcp__notion__search"), "*", ruleset) == "ask"


def test_coerce_mcp_local_to_stdio() -> None:
    out = coerce_sdk_mcp_config("x", {"type": "local", "command": ["uvx", "srv", "--flag"]})
    assert out == {"type": "stdio", "command": "uvx", "args": ["srv", "--flag"]}


def test_coerce_mcp_command_shorthand() -> None:
    out = coerce_sdk_mcp_config("x", {"command": "uvx", "args": ["srv"], "env": {"K": "v"}})
    assert out == {"type": "stdio", "command": "uvx", "args": ["srv"], "env": {"K": "v"}}


def test_coerce_mcp_remote_variants() -> None:
    assert coerce_sdk_mcp_config("x", {"type": "sse", "url": "http://h/sse"})["type"] == "sse"
    assert coerce_sdk_mcp_config("x", {"type": "http", "url": "http://h"})["type"] == "http"
    # OpenCode's collapsed "remote" defaults to http.
    assert coerce_sdk_mcp_config("x", {"type": "remote", "url": "http://h"})["type"] == "http"


async def test_context_mcp_servers_passed_as_sdk_shape() -> None:
    seen: list = []

    def query_fn(*, prompt, options):
        seen.append(options)

        async def gen():
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(query_fn=query_fn)
    await _collect(
        backend,
        _turn(mcp={"github": {"command": "uvx", "args": ["mcp-github"]}}),
    )
    assert seen[0].mcp_servers["github"] == {
        "type": "stdio",
        "command": "uvx",
        "args": ["mcp-github"],
    }


async def test_allowed_tool_is_preapproved_without_human() -> None:
    # A context that pre-approves Bash(git *) must auto-allow `git status` with no
    # PermissionRequested reaching the streamer.
    captured: list = []

    def query_fn(*, prompt, options):
        async def gen():
            yield _init()
            ctx = SimpleNamespace(tool_use_id="t1")
            captured.append(await options.can_use_tool("Bash", {"command": "git status"}, ctx))
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(query_fn=query_fn)
    requests: list = []
    async for event in backend.run_turn(_turn(allowed_tools=["Bash(git *)"])):
        if isinstance(event, PermissionRequested):
            requests.append(event)
    assert isinstance(captured[0], PermissionResultAllow)
    assert requests == []  # never bugged the human


async def test_send_file_registered_as_sdk_tool_and_preapproved() -> None:
    seen: list = []

    def query_fn(*, prompt, options):
        seen.append(options)

        async def gen():
            yield _result()

        return gen()

    async def _handler(args):
        return {"content": [{"type": "text", "text": "ok"}]}

    def factory(chat_id, thread_id):
        assert (chat_id, thread_id) == (42, 7)
        return AgentTool(
            name="send_file",
            description="send a file",
            input_schema={"type": "object"},
            read_only=True,
            handler=_handler,
        )

    backend = ClaudeSdkBackend(send_file_factory=factory, query_fn=query_fn)
    await _collect(backend, _turn(chat_id=42, thread_id=7))
    opts = seen[0]
    assert "balam" in opts.mcp_servers
    assert "mcp__balam__send_file" in opts.allowed_tools


async def test_non_uuid_session_id_is_not_resumed() -> None:
    # A topic carried over from the OpenCode backend has a ses_… id; resuming it
    # would hard-fail the SDK, so resume must be omitted (start fresh).
    seen: list = []

    def query_fn(*, prompt, options):
        seen.append(options)

        async def gen():
            yield _result()

        return gen()

    await _collect(ClaudeSdkBackend(query_fn=query_fn), _turn(session_id="ses_opencode_legacy"))
    assert seen[0].resume is None


async def test_uuid_session_id_is_resumed() -> None:
    seen: list = []

    def query_fn(*, prompt, options):
        seen.append(options)

        async def gen():
            yield _result()

        return gen()

    uid = "6ec73cf3-1da4-4ad8-923f-18da769179f2"
    await _collect(ClaudeSdkBackend(query_fn=query_fn), _turn(session_id=uid))
    assert seen[0].resume == uid


async def test_resume_and_model_effort_passed_to_options() -> None:
    seen_options: list = []

    def query_fn(*, prompt, options):
        seen_options.append(options)

        async def gen():
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(api_key="sk-x", query_fn=query_fn)
    prev = "11111111-2222-3333-4444-555555555555"
    await _collect(
        backend,
        _turn(session_id=prev, model="claude-opus-4-8", effort="high"),
    )
    opts = seen_options[0]
    assert opts.resume == prev
    assert opts.model == "claude-opus-4-8"
    assert opts.effort == "high"
    assert opts.cwd == "/ws"
    assert opts.env.get("ANTHROPIC_API_KEY") == "sk-x"
