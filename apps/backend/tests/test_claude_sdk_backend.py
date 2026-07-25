"""ClaudeSdkBackend translates SDK messages into AgentEvents (ADR-0013).

Drives turns through an injected fake ``query_fn`` so no real ``claude``
subprocess is spawned.
"""

import asyncio
from types import SimpleNamespace

from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from balam.agent.backend import TurnRequest
from balam.agent.claude_sdk_backend import ClaudeSdkBackend, _category, coerce_sdk_mcp_config
from balam.agent.events import (
    BackgroundTask,
    BackgroundTasksChanged,
    PermissionRequested,
    QuestionAsked,
    SessionStarted,
    TextUpdated,
    ToolUpdated,
    TurnFailed,
    TurnFinished,
    TurnStepFinished,
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


def _tool_call(call_id: str, name: str, tool_input: dict) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id=call_id, name=name, input=tool_input)], model="claude"
    )


def _tool_result(call_id: str, content: str, *, is_error: bool = False) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=call_id, content=content, is_error=is_error)]
    )


async def test_task_tools_mirror_into_todowrite_checklist_events() -> None:
    # The TaskCreate/TaskUpdate family (harnesses that replace TodoWrite) is
    # mirrored into synthetic todowrite events carrying the cumulative list;
    # the raw Task* calls never reach the tool stream.
    messages = [
        _init(),
        _tool_call("t1", "TaskCreate", {"subject": "Write tests", "description": "d"}),
        _tool_result("t1", "Task #1 created successfully: Write tests"),
        _tool_call("t2", "TaskCreate", {"subject": "Run them", "description": "d"}),
        _tool_result("t2", "Task #2 created successfully: Run them"),
        _tool_call("t3", "TaskUpdate", {"taskId": "1", "status": "in_progress"}),
        _tool_result("t3", "Updated task #1 status"),
        _result(),
    ]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())
    tools = [e for e in events if isinstance(e, ToolUpdated)]
    assert all(t.tool == "todowrite" and t.status == "completed" for t in tools)
    assert [t.input["todos"] for t in tools] == [
        [{"content": "Write tests", "status": "pending"}],
        [
            {"content": "Write tests", "status": "pending"},
            {"content": "Run them", "status": "pending"},
        ],
        [
            {"content": "Write tests", "status": "in_progress"},
            {"content": "Run them", "status": "pending"},
        ],
    ]


async def test_task_update_handles_unknown_ids_and_deletion() -> None:
    # A task created in an earlier turn gets a placeholder label; a "deleted"
    # status removes the item from the mirror.
    messages = [
        _init(),
        _tool_call("t1", "TaskUpdate", {"taskId": "7", "status": "in_progress"}),
        _tool_result("t1", "Updated task #7 status"),
        _tool_call("t2", "TaskUpdate", {"taskId": "7", "status": "deleted"}),
        _tool_result("t2", "Deleted task #7"),
        _result(),
    ]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())
    tools = [e for e in events if isinstance(e, ToolUpdated)]
    assert [t.input["todos"] for t in tools] == [
        [{"content": "Task #7", "status": "in_progress"}],
        [],
    ]


async def test_failed_task_tool_call_stays_loud() -> None:
    # An errored Task* call is not swallowed into the mirror: it surfaces as a
    # normal error tool event under its raw name.
    messages = [
        _init(),
        _tool_call("t1", "TaskUpdate", {"taskId": "9", "status": "completed"}),
        _tool_result("t1", "No such task", is_error=True),
        _result(),
    ]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())
    tools = [e for e in events if isinstance(e, ToolUpdated)]
    assert len(tools) == 1
    assert tools[0].tool == "TaskUpdate"
    assert tools[0].status == "error"
    assert tools[0].error == "No such task"


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


async def test_options_env_lifts_artifact_sdk_default_off() -> None:
    # CLAUDE_CODE_ARTIFACT=1 skips the CLI's SDK-entrypoint default-off so the
    # Artifact tool + bundled artifact skills can load (account gates permitting);
    # see docs/claude-cli-gated-features.md.
    seen_options: list = []

    def query_fn(*, prompt, options):
        seen_options.append(options)

        async def gen():
            yield _result()

        return gen()

    await _collect(ClaudeSdkBackend(query_fn=query_fn), _turn())
    assert seen_options[0].env["CLAUDE_CODE_ARTIFACT"] == "1"


async def test_options_raise_the_json_line_buffer_above_the_1mb_default() -> None:
    # A single CLI message can exceed the SDK's 1 MB default and fatally error the
    # turn — a big file read, a long bash result, or a held-open subagent's final
    # report. Balam lifts the limit so those arrive instead of killing the turn.
    seen_options: list = []

    def query_fn(*, prompt, options):
        seen_options.append(options)

        async def gen():
            yield _result()

        return gen()

    await _collect(ClaudeSdkBackend(query_fn=query_fn), _turn())
    assert seen_options[0].max_buffer_size == 10 * 1024 * 1024


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


def test_coerce_mcp_disabled_returns_none() -> None:
    # The SDK has no wire toggle for `enabled: false`; the disable is honored by
    # not registering the server (OpenCode passes the flag through instead).
    assert coerce_sdk_mcp_config("x", {"command": "uvx", "enabled": False}) is None
    assert coerce_sdk_mcp_config("x", {"command": "uvx", "enabled": True}) is not None


async def test_disabled_context_mcp_server_not_registered() -> None:
    seen: list = []

    def query_fn(*, prompt, options):
        seen.append(options)

        async def gen():
            yield _result()

        return gen()

    backend = ClaudeSdkBackend(query_fn=query_fn)
    await _collect(
        backend,
        _turn(
            mcp={
                "on": {"command": "uvx", "args": ["mcp-on"]},
                "off": {"command": "uvx", "args": ["mcp-off"], "enabled": False},
            }
        ),
    )
    assert "on" in seen[0].mcp_servers
    assert "off" not in seen[0].mcp_servers


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


# --- mid-turn follow-ups (streaming input) -----------------------------------


async def test_follow_up_is_folded_into_the_live_turn() -> None:
    # A follow-up offered mid-turn is forwarded into the same query at the first
    # ResultMessage boundary: the driver emits TurnStepFinished, the follow-up
    # reaches the input stream, and a second response streams before TurnFinished.
    from balam.agent.backend import FollowUp, FollowUpChannel
    from balam.agent.events import TurnStepFinished

    forwarded: list = []

    async def query_fn(*, prompt, options):
        stream = prompt.__aiter__()
        await stream.__anext__()  # initial user message
        yield _init()
        yield _stream({"type": "message_start", "message": {"id": "m1"}})
        yield _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "answer one"},
            }
        )
        yield _result()  # first response done → driver forwards the follow-up
        forwarded.append(await stream.__anext__())
        yield _stream({"type": "message_start", "message": {"id": "m2"}})
        yield _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "answer two"},
            }
        )
        yield _result()  # no follow-up pending → turn ends

    channel = FollowUpChannel()
    channel.offer(FollowUp(prompt="the follow-up"))
    backend = ClaudeSdkBackend(query_fn=query_fn)
    events = await _collect(backend, _turn(follow_ups=channel))

    steps = [e for e in events if isinstance(e, TurnStepFinished)]
    finished = [e for e in events if isinstance(e, TurnFinished)]
    texts = [e.text for e in events if isinstance(e, TextUpdated)]
    assert len(steps) == 1  # exactly one fold
    assert len(finished) == 1 and isinstance(events[-1], TurnFinished)
    assert "answer one" in texts and "answer two" in texts
    # The follow-up actually reached the SDK as a user message.
    assert forwarded and forwarded[0]["message"]["content"] == "the follow-up"
    # The channel is drained and closed once the turn ends.
    assert channel.take() is None and channel.closed


async def test_turn_without_follow_ups_ends_after_one_result() -> None:
    # The common case: no channel wired → the turn ends at the first ResultMessage.
    from balam.agent.events import TurnStepFinished

    messages = [_init(), _result()]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())
    assert not any(isinstance(e, TurnStepFinished) for e in events)
    assert isinstance(events[-1], TurnFinished)


async def test_follow_up_offered_after_close_is_bounced() -> None:
    # A message racing the turn's end (channel already closed) is refused, so the
    # bot falls back to queueing it as the next turn.
    from balam.agent.backend import FollowUp, FollowUpChannel

    channel = FollowUpChannel()
    assert channel.offer(FollowUp(prompt="in time")) is True
    channel.close()
    assert channel.offer(FollowUp(prompt="too late")) is False
    assert channel.take().prompt == "in time"
    assert channel.take() is None


def _foreign_result() -> ResultMessage:
    """A ResultMessage for a prompt the CLI queued for itself.

    Shape taken from a live capture: on resume the CLI's orphan-task scan
    injects a ``<task-notification>`` prompt ahead of ours and answers it
    without ever calling the model.
    """
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=0,
        is_error=False,
        num_turns=0,
        session_id=SID,
        result="",
    )


async def test_result_for_a_prompt_balam_did_not_send_does_not_end_the_turn() -> None:
    # The bug: ending on the CLI's own queued prompt finalized an empty reply
    # ("the agent finished without producing any text") and tore the query down
    # while the model was still answering the real prompt.
    messages = [
        SystemMessage(subtype="task_notification", data={"status": "stopped"}),
        _init(),
        _foreign_result(),
        _init(),
        _stream({"type": "message_start", "message": {"id": "m1"}}),
        _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "the real answer"},
            }
        ),
        _result(result="the real answer"),
    ]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())

    assert [e.text for e in events if isinstance(e, TextUpdated)] == ["the real answer"]
    assert len([e for e in events if isinstance(e, TurnFinished)]) == 1
    assert isinstance(events[-1], TurnFinished)


async def test_short_real_reply_still_ends_the_turn() -> None:
    # Guard against over-eager skipping: a one-word reply is a real reply, and the
    # model *did* run, so its result must end the turn.
    messages = [
        _init(),
        _stream({"type": "message_start", "message": {"id": "m1"}}),
        _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "OK"},
            }
        ),
        # num_turns/duration_api_ms of a foreign result, but the model ran.
        _foreign_result(),
    ]
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())
    assert isinstance(events[-1], TurnFinished)


async def test_error_result_is_never_treated_as_foreign() -> None:
    # An error ends the turn whatever its counters say — otherwise a failure that
    # never reached the model would hang the topic until the guard fired.
    error = ResultMessage(
        subtype="error_during_execution",
        duration_ms=1,
        duration_api_ms=0,
        is_error=True,
        num_turns=0,
        session_id=SID,
        result="boom",
    )
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query([_init(), error])), _turn())
    assert isinstance(events[-1], TurnFailed) and events[-1].message == "boom"


async def test_ignored_result_that_leads_nowhere_ends_the_turn(monkeypatch) -> None:
    # Backstop: if the "foreign" judgement was wrong and nothing follows, the turn
    # ends on the grace timer instead of hanging the topic forever.
    import balam.agent.claude_sdk_backend as mod

    monkeypatch.setattr(mod, "_FOREIGN_RESULT_GRACE_S", 0.05)

    async def query_fn(*, prompt, options):
        yield _init()
        yield _foreign_result()
        await asyncio.sleep(5)  # the CLI goes quiet: nothing more is coming

    events = await asyncio.wait_for(
        _collect(ClaudeSdkBackend(query_fn=query_fn), _turn()), timeout=5
    )
    assert isinstance(events[-1], TurnFinished)


async def _task_events(messages):
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query(messages)), _turn())
    return [e for e in events if isinstance(e, BackgroundTasksChanged)]


def _started(task_id: str, description: str, task_type: str | None = None):
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description=description,
        uuid="u",
        session_id="ses_x",
        task_type=task_type,
    )


def _updated(task_id: str, **patch):
    return TaskUpdatedMessage(subtype="task_updated", data={}, task_id=task_id, patch=patch)


def _notified(task_id: str, status: str):
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file="/tmp/out",
        summary="done",
        uuid="u",
        session_id="ses_x",
    )


async def test_started_tasks_are_reported() -> None:
    # `background_tasks_changed` never reaches an SDK client; the live set is
    # rebuilt from the per-task lifecycle messages instead.
    reported = await _task_events(
        [_init(), _started("b1", "Start dev server", "local_bash"), _result()]
    )
    assert reported[-1].tasks == (
        BackgroundTask(task_id="b1", description="Start dev server", task_type="local_bash"),
    )


async def test_task_without_description_falls_back_to_its_id() -> None:
    reported = await _task_events([_init(), _started("b2", ""), _result()])
    assert reported[-1].tasks == (BackgroundTask(task_id="b2", description="b2", task_type=None),)


async def test_terminal_task_update_clears_the_task() -> None:
    reported = await _task_events(
        [_init(), _started("b1", "Build"), _updated("b1", status="completed"), _result()]
    )
    assert reported[-1].tasks == ()


async def test_terminal_task_notification_clears_the_task() -> None:
    # A task may report completion through either message, so both must clear it.
    reported = await _task_events(
        [_init(), _started("b1", "Build"), _notified("b1", "killed"), _result()]
    )
    assert reported[-1].tasks == ()


async def test_non_terminal_update_keeps_the_task_running() -> None:
    reported = await _task_events(
        [_init(), _started("b1", "Build"), _updated("b1", status="running"), _result()]
    )
    assert reported[-1].tasks == (
        BackgroundTask(task_id="b1", description="Build", task_type=None),
    )


async def test_surviving_task_is_still_live_at_the_end_of_the_turn() -> None:
    # Two started, one finished: the turn ends with work still running.
    reported = await _task_events(
        [
            _init(),
            _started("b1", "Agent one"),
            _started("b2", "Agent two"),
            _updated("b1", status="completed"),
            _result(),
        ]
    )
    assert reported[-1].tasks == (
        BackgroundTask(task_id="b2", description="Agent two", task_type=None),
    )


async def test_turn_is_held_open_while_background_work_runs() -> None:
    # Closing stdin winds the CLI down and kills its background tasks, so a
    # result that arrives with work still live must not end the turn.
    events = await _collect(
        ClaudeSdkBackend(query_fn=_fake_query([_init(), _started("b1", "Investigate"), _result()])),
        _turn(),
    )
    assert any(isinstance(e, TurnStepFinished) for e in events)
    assert not any(isinstance(e, TurnFinished) for e in events)


async def test_turn_ends_normally_when_nothing_is_left_running() -> None:
    events = await _collect(ClaudeSdkBackend(query_fn=_fake_query([_init(), _result()])), _turn())
    assert any(isinstance(e, TurnFinished) for e in events)
    assert not any(isinstance(e, TurnStepFinished) for e in events)


async def test_finished_background_work_lets_the_turn_end() -> None:
    # Held open at the first result, released at the second once the task is done.
    events = await _collect(
        ClaudeSdkBackend(
            query_fn=_fake_query(
                [
                    _init(),
                    _started("b1", "Investigate"),
                    _result(),
                    _updated("b1", status="completed"),
                    _result(),
                ]
            )
        ),
        _turn(),
    )
    assert any(isinstance(e, TurnStepFinished) for e in events)
    assert any(isinstance(e, TurnFinished) for e in events)


async def test_background_report_streams_as_its_own_step() -> None:
    # The CLI wakes the model when a task finishes; that report is an ordinary
    # assistant turn, and the TurnStepFinished before it makes the streamer
    # commit the previous answer so the report lands as a separate message.
    events = await _collect(
        ClaudeSdkBackend(
            query_fn=_fake_query(
                [
                    _init(),
                    _started("b1", "Investigate"),
                    _result(),
                    _updated("b1", status="completed"),
                    AssistantMessage(
                        content=[TextBlock(text="The investigation finished.")],
                        model="claude",
                        session_id=SID,
                    ),
                    _result(),
                ]
            )
        ),
        _turn(),
    )
    order = [type(e).__name__ for e in events]
    assert order.index("TurnStepFinished") < order.index("TurnFinished")
