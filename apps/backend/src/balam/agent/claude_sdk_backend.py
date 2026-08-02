"""Claude Agent SDK implementation of :class:`~balam.agent.backend.AgentBackend`
(ADR-0013).

Unlike OpenCode (a long-lived server we configure once per session), the SDK is
driven with a fresh, stateless ``query(resume=…)`` per turn — so each turn
re-supplies the context's capabilities through :class:`ClaudeAgentOptions`. That
choice is what lets model / effort / permission-mode vary per turn (a persistent
``ClaudeSDKClient`` cannot change effort mid-session). Session continuity comes
from ``resume`` plus the SDK's on-disk transcripts; the real session id is minted
lazily and surfaces on the first turn as a
:class:`~balam.agent.events.SessionStarted`.

**Producer/consumer, same as OpenCodeBackend.** A *driver* task iterates
``query()`` and translates messages into normalized events on a queue, while the
``can_use_tool`` callback (invoked by the SDK on the driver's call stack) parks a
future and enqueues a :class:`~balam.agent.events.PermissionRequested`; the
streamer's decision resolves the future via :meth:`reply_permission`. Text and
reasoning stream incrementally from ``StreamEvent`` partials
(``include_partial_messages``); tool calls/results come from the consolidated
messages. Reasoning is coarser than OpenCode's — extended thinking is not
streamed token-by-token.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from balam.agent.backend import TurnRequest
from balam.agent.events import (
    AgentEvent,
    BackgroundTasksChanged,
    PermissionRequested,
    QuestionAsked,
    ReasoningUpdated,
    RetryNotice,
    SessionStarted,
    TextUpdated,
    ToolUpdated,
    TurnFailed,
    TurnFinished,
    TurnStepFinished,
)
from balam.agent.sdk_tasks import (
    _TASK_TOOLS,
    LiveTasks,
    _apply_task_result,
)
from balam.agent.sdk_translate import (
    _category,
    _content_blocks,
    _eval_target,
    _is_resumable,
    _normalize_input,
    _wire_tool,
    coerce_sdk_mcp_config,
)
from balam.agent_tools import AgentTool
from balam.attachments import save_attachments
from balam.contexts import ContextConfig
from balam.permissions import build_ruleset, evaluate

logger = logging.getLogger(__name__)

#: Pushed by the driver's ``finally`` to tell ``run_turn`` the stream is done.
_SENTINEL = None


#: How long to keep a turn open after ignoring a ResultMessage as foreign
#: (:func:`_is_foreign_result`). Only reached when that judgement was wrong —
#: nothing follows the result we skipped — so it trades a slow turn for a hung
#: topic. Comfortably longer than the CLI's own gap between queued prompts.
_FOREIGN_RESULT_GRACE_S = 45.0

#: Absolute cap on holding a turn open for background work it left running. The
#: CLI kills its background tasks as the process winds down, and the process
#: winds down when we close stdin — so the turn stays open while work is live
#: (see ``run_turn``). One CLI process is ~200-500 MB here, so this bounds what a
#: task that never finishes can pin: at the deadline the turn ends and its
#: background work stops with it.
_BACKGROUND_HOLD_S = 30 * 60.0

#: Appended to the ``claude_code`` system prompt. Background tasks are children
#: of the CLI process, which the turn now keeps alive while any of them are
#: running (see ``run_turn``) — so backgrounding is safe, and promising to report
#: later is a promise the runtime can keep. The remaining limit is the
#: ``_BACKGROUND_HOLD_S`` cap, which a genuinely long-lived service should escape
#: with ``setsid`` (verified on this VM: a detached process gets its own session
#: and survives).
_SYSTEM_PROMPT_APPEND = """
## Background work in this environment

Work you start in the background — a `run_in_background` shell command, or a
subagent you do not wait on — keeps running after you reply. The turn stays open
while it runs, and when it finishes you are woken to report it, so telling the
user "I'll report back when this lands" is a promise that will be kept. You do
not need to hold the turn open yourself by waiting.

Two limits are worth knowing:

- Background work is capped at 30 minutes. Past that the turn ends and anything
  still running stops.
- A service meant to outlive the conversation entirely (a dev server, a tunnel,
  a watcher) should be detached instead, so nothing can reap it:

      setsid nohup <command> > /tmp/<name>.log 2>&1 &

  Then tell the user the log path and how to stop it (the PID, or `pkill -f`).
"""


def _is_foreign_result(message: ResultMessage, *, model_called: bool) -> bool:
    """Whether ``message`` answers a prompt the CLI queued for *itself*.

    The CLI has its own prompt queue: on resume, its background-task scan
    injects a ``<task-notification>`` ahead of ours, and every queued prompt gets
    its own ResultMessage, in order. Taking the first one as "the turn is over"
    finalizes an empty reply and tears the query down while the model is still
    answering the real prompt.

    Such an injected step never reaches the model — ``num_turns`` and
    ``duration_api_ms`` are both 0 and no assistant message is streamed — which
    is what separates it from a real reply, however short. An error result is
    never foreign: it ends the turn either way.
    """
    if message.is_error or model_called:
        return False
    return message.num_turns == 0 and message.duration_api_ms == 0


SendFileFactory = Callable[[int, int | None], "AgentTool | None"]
QueryFn = Callable[..., AsyncIterator[Any]]


class ClaudeSdkBackend:
    """Drive the Claude Agent SDK as an :class:`~balam.agent.backend.AgentBackend`.

    ``query_fn`` is injectable so tests can drive turns without spawning the real
    ``claude`` subprocess.
    """

    #: The SDK holds its stdin channel open for the whole turn (streaming-input
    #: mode), so a message that arrives mid-turn can be folded into the live
    #: session and picked up at the next step (see ``run_turn``).
    supports_streaming_input = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        cli_path: str | None = None,
        send_file_factory: SendFileFactory | None = None,
        query_fn: QueryFn = query,
    ) -> None:
        self._api_key = api_key
        self._cli_path = cli_path
        self._send_file_factory = send_file_factory
        self._query = query_fn
        # request_id -> future resolved by reply_permission / reply_question.
        self._pending_perms: dict[str, asyncio.Future[tuple[bool, str | None]]] = {}
        self._pending_questions: dict[str, asyncio.Future[list[list[str]] | None]] = {}

    def set_send_file_factory(self, factory: SendFileFactory) -> None:
        """Wire the per-topic send_file tool factory once the bot is available
        (app boot constructs the backend before the Telegram bot exists)."""
        self._send_file_factory = factory

    async def wait_for_ready(self) -> None:
        # The SDK spawns its CLI subprocess lazily per query; nothing to poll.
        return None

    async def aclose(self) -> None:
        return None

    async def session_exists(self, session_id: str, *, directory: str) -> bool:
        # Sessions resume from on-disk transcripts; assume resumable and let a
        # failed resume surface as a turn error rather than pre-checking here.
        return True

    async def abort(self, session_id: str, *, directory: str) -> None:
        # The streamer aborts by cancelling the turn task, which closes the
        # run_turn generator and tears down the query subprocess; nothing to do.
        return None

    async def reply_permission(
        self,
        request_id: str,
        *,
        allow: bool,
        message: str | None = None,
        directory: str | None = None,
    ) -> None:
        future = self._pending_perms.get(request_id)
        if future is not None and not future.done():
            future.set_result((allow, message))

    async def reply_question(
        self, request_id: str, answers: list[list[str]], *, directory: str | None = None
    ) -> None:
        future = self._pending_questions.get(request_id)
        if future is not None and not future.done():
            future.set_result(answers)

    async def reject_question(self, request_id: str, *, directory: str | None = None) -> None:
        future = self._pending_questions.get(request_id)
        if future is not None and not future.done():
            future.set_result(None)

    def _mcp_setup(self, turn: TurnRequest) -> tuple[dict[str, Any], list[str]]:
        """The turn's MCP servers + the tools to pre-approve natively.

        Context ``mcp`` servers are coerced to the SDK shape; Balam's own
        ``send_file`` is added as an in-process SDK tool (no HTTP server / scope
        token needed — the closure already carries the topic) and pre-approved so
        it runs without the keyboard, matching OpenCode's send_file_rules allow.
        """
        servers: dict[str, Any] = {}
        for name, raw in (turn.mcp or {}).items():
            try:
                coerced = coerce_sdk_mcp_config(name, raw)
            except ValueError:
                logger.warning("skipping unusable MCP server %r for the SDK backend", name)
                continue
            if coerced is not None:
                servers[name] = coerced

        allowed: list[str] = []
        if self._send_file_factory is not None and turn.chat_id is not None:
            agent_tool = self._send_file_factory(turn.chat_id, turn.thread_id)
            if agent_tool is not None:
                sdk_tool = tool(agent_tool.name, agent_tool.description, agent_tool.input_schema)(
                    agent_tool.handler
                )
                server = dict(create_sdk_mcp_server(name="balam", tools=[sdk_tool]))
                # Exempt send_file from the CLI's tool-search deferral: without
                # this the model sees only the tool *name* until it ToolSearches,
                # so the when-to-use guidance in the description never lands.
                # The SDK forwards unknown config keys (only ``instance`` is
                # stripped) and the CLI schema accepts ``alwaysLoad`` on sdk
                # servers, propagating anthropic/alwaysLoad to every tool.
                server["alwaysLoad"] = True
                servers["balam"] = server
                allowed.append(f"mcp__balam__{agent_tool.name}")
        return servers, allowed

    def _build_options(
        self,
        turn: TurnRequest,
        can_use_tool: Any,
        mcp_servers: dict[str, Any],
        allowed_tools: list[str],
    ) -> ClaudeAgentOptions:
        """Translate a turn + context into per-turn SDK options."""
        env: dict[str, str] = {}
        if self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        # The CLI ships the whole Artifact stack (Artifact tool, artifact-design/
        # artifact-capabilities bundled skills) but defaults it OFF for SDK
        # entrypoints (`isArtifactSdkDefaultOff`: sdk-py/sdk-ts/sdk-cli). This env
        # var skips only that check; the account-side gates (first-party OAuth,
        # paid tier, `tengu_cobalt_plinth` rollout) still apply, so on an
        # ineligible account the tool simply stays hidden. The CLI `/artifacts`
        # command stays interactive-only (local-jsx); Balam's /artifacts bot
        # command covers it via the tool's `action:"list"`. See
        # docs/claude-cli-gated-features.md.
        env["CLAUDE_CODE_ARTIFACT"] = "1"
        kwargs: dict[str, Any] = {
            "cwd": turn.directory,
            # Always the default mode: Balam has no plan-mode command, and the
            # CLI's native natural-language planning needs no formal mode. An
            # explicit value also neutralizes ``defaultMode`` from settings.
            "permission_mode": "default",
            "can_use_tool": can_use_tool,
            "include_partial_messages": True,
            # The SDK reads the CLI's stdout as line-delimited JSON and fatally
            # errors the turn if a single message exceeds this (default 1 MB —
            # "JSON message exceeded maximum buffer size"). One message really can
            # be that big: a large file read, a long bash result, or a background
            # subagent's final report — and holding the turn open (ADR-0015) is
            # exactly what now lets those big reports arrive instead of dying with
            # the subprocess. 10 MB, matching open-shrimp.
            "max_buffer_size": 10 * 1024 * 1024,
            # Keep Claude Code's native behavior (incl. natural-language planning),
            # plus what the agent cannot infer: its process only lives for this
            # turn, so anything meant to outlive it must be detached.
            "system_prompt": {
                "type": "preset",
                "preset": "claude_code",
                "append": _SYSTEM_PROMPT_APPEND,
            },
            # Load user + project + local settings so filesystem skills are
            # discovered (both the global ~/.claude/skills and the project's
            # .claude/skills), matching the interactive session. Skill discovery is
            # gated on setting sources — there is no skills-only switch for the
            # loose .claude/skills layout — so the whole source must be loaded.
            # ``skills="all"`` enables every discovered skill and pre-approves the
            # Skill tool. TRADE-OFF, accepted by the owner: a ``permissions.allow``
            # entry in any loaded settings file is evaluated by the CLI *before*
            # can_use_tool, so it pre-approves that tool and bypasses BOTH the
            # approval keyboard and the config.yaml ruleset (which only runs when
            # the CLI's native rules evaluate to "ask"). config.yaml still governs
            # everything the settings files don't pre-allow. ``defaultMode:
            # acceptEdits`` in settings.local.json is neutralized because we pass
            # permission_mode="default" explicitly each turn (an explicit
            # --permission-mode overrides the settings defaultMode), so edits still
            # reach the keyboard.
            "setting_sources": ["user", "project", "local"],
            "skills": "all",
            # Must stay False so the claude.ai account-managed connectors
            # (Google Calendar, Gmail, Drive, Notion) remain available: strict mode
            # uses ONLY the servers in mcp_servers and drops every "claudeai-proxy"
            # connector, and the SDK has no input form to whitelist them back
            # (McpClaudeAIProxyServerConfig is output-only). config.yaml still owns
            # the usable surface via allowed_tools + the approval keyboard; on this VM
            # nothing else leaks in (top-level ~/.claude.json mcpServers is empty and
            # project-scoped servers never match a Balam context cwd).
            "strict_mcp_config": False,
            "env": env,
        }
        if _is_resumable(turn.session_id):
            kwargs["resume"] = turn.session_id
        if turn.model:
            kwargs["model"] = turn.model
        if turn.effort:
            kwargs["effort"] = turn.effort
        if turn.additional_directories:
            kwargs["add_dirs"] = list(turn.additional_directories)
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        if allowed_tools:
            kwargs["allowed_tools"] = allowed_tools
        if self._cli_path:
            kwargs["cli_path"] = self._cli_path
        return ClaudeAgentOptions(**kwargs)

    async def run_turn(self, turn: TurnRequest) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        loop = asyncio.get_event_loop()
        session_started = False
        # tool_use_id -> (wire_tool, normalized_input), to render results later.
        tool_calls: dict[str, tuple[str, dict[str, Any]]] = {}
        # task id -> {"content", "status"}: the turn's mirror of the harness's
        # TaskCreate/TaskUpdate state, re-emitted as synthetic ``todowrite``
        # events so the streamer's checklist covers both tool vocabularies.
        task_mirror: dict[str, dict[str, str]] = {}
        # What the CLI has running right now, from its task lifecycle messages.
        # Read at the turn boundary to decide what would outlive the turn.
        live_tasks = LiveTasks()
        # Per-streaming-message block accumulators (StreamEvent partials).
        cur_msg_id: str | None = None
        block_text: dict[int, str] = {}
        # Whether the model ran since the last ResultMessage. A result for a step
        # that never called the model belongs to a prompt the CLI queued itself,
        # not to ours (see ``_is_foreign_result``).
        model_called = False
        owned_perms: set[str] = set()
        owned_questions: set[str] = set()
        # Messages the driver hands to the streaming-input stream: the initial
        # prompt, then any mid-turn follow-up the driver forwards at a step
        # boundary, then a ``None`` sentinel to close. The input stream must stay
        # OPEN for the whole turn: can_use_tool / question control requests travel
        # back over stdin, so closing it early (after one message) kills the
        # channel and the CLI denies tools with "Stream closed". The DRIVER is the
        # sole producer here so the "forward next follow-up vs end the turn"
        # decision stays atomic at the boundary (see the ResultMessage handling).
        outbound: asyncio.Queue[tuple[str, list[Any]] | None] = asyncio.Queue()
        # Mid-turn follow-ups from the bot (Claude Code-style). ``None`` when the
        # topic's transport can't deliver mid-turn (the bot only wires it for the
        # streaming-input backend, which is us).
        channel = turn.follow_ups

        # The context's opt-in ruleset, evaluated in process to pre-approve tool
        # calls the user allowed (the SDK has no server to delegate this to).
        ruleset: list[dict[str, str]] = []
        if turn.directory:
            ctx = ContextConfig(
                directory=turn.directory,
                description="",
                allowed_tools=list(turn.allowed_tools),
                additional_directories=list(turn.additional_directories),
            )
            ruleset = build_ruleset(ctx)

        def maybe_session(session_id: str | None) -> None:
            nonlocal session_started
            if session_id and not session_started:
                session_started = True
                queue.put_nowait(SessionStarted(session_id))

        # Backstop for the :func:`_is_foreign_result` skip. Armed when a result is
        # skipped and disarmed by the next message off the stream, so it only ever
        # fires if that judgement was wrong and nothing follows — ending the turn
        # the skipped result would have ended, instead of hanging the topic.
        idle_guard: asyncio.Task[None] | None = None

        def disarm_idle_guard() -> None:
            nonlocal idle_guard
            if idle_guard is not None:
                idle_guard.cancel()
                idle_guard = None

        def arm_idle_guard() -> None:
            nonlocal idle_guard

            async def expire() -> None:
                await asyncio.sleep(_FOREIGN_RESULT_GRACE_S)
                logger.warning(
                    "nothing followed the ignored ResultMessage within %ss; ending the turn",
                    _FOREIGN_RESULT_GRACE_S,
                )
                if channel is not None:
                    channel.close()
                outbound.put_nowait(None)
                await queue.put(TurnFinished())
                # End the stream here rather than waiting for the CLI to notice
                # its closed stdin: run_turn's finally then cancels the driver,
                # which closes the query and its subprocess.
                await queue.put(_SENTINEL)

            disarm_idle_guard()
            idle_guard = asyncio.create_task(expire())

        # Absolute cap on how long the turn is held open for background work
        # (below). Unlike the idle guard this is a deadline, not an
        # inactivity timer: a task that never finishes must not pin the topic
        # (and its CLI process) forever.
        hold_watchdog: asyncio.Task[None] | None = None
        held_for_background = False

        def disarm_hold_watchdog() -> None:
            nonlocal hold_watchdog
            if hold_watchdog is not None:
                hold_watchdog.cancel()
                hold_watchdog = None

        def arm_hold_watchdog() -> None:
            nonlocal hold_watchdog, held_for_background
            if hold_watchdog is not None:
                return  # already counting down; the cap is from the first hold
            held_for_background = True

            async def expire() -> None:
                await asyncio.sleep(_BACKGROUND_HOLD_S)
                logger.warning(
                    "background work still running after %ss; ending the turn "
                    "(its tasks stop with the agent process)",
                    _BACKGROUND_HOLD_S,
                )
                if channel is not None:
                    channel.close()
                outbound.put_nowait(None)
                await queue.put(TurnFinished())
                await queue.put(_SENTINEL)

            hold_watchdog = asyncio.create_task(expire())

        async def ask_user_question(
            input_data: dict[str, Any],
        ) -> PermissionResultAllow | PermissionResultDeny:
            """Surface the SDK's ``AskUserQuestion`` tool as Balam's structured
            question flow instead of a tool-approval prompt.

            Claude Code answers this tool not by allowing it bare, but by injecting
            an ``answers`` record (question text → answer string) into the tool
            input via ``updated_input`` — the tool then reads that record to build
            its result. So we render the questions on the Telegram keyboard, await
            the selections, and allow the call with ``answers`` populated. The model
            never sees a permission prompt; declining maps to a deny."""
            raw_questions = input_data.get("questions") or []
            if not isinstance(raw_questions, list) or not raw_questions:
                return PermissionResultDeny(message="No questions to ask.")
            request_id = f"q_{uuid.uuid4().hex[:16]}"
            future: asyncio.Future[list[list[str]] | None] = loop.create_future()
            self._pending_questions[request_id] = future
            owned_questions.add(request_id)
            # Map the SDK's input shape to the OpenCode-style question dict the
            # streamer renders: ``multiSelect`` → ``multiple``; ``custom`` stays on
            # so the user can type a free-text answer (Claude Code auto-adds the
            # "Other" option, which it tells the model not to supply itself).
            questions = [
                {
                    "question": q.get("question", ""),
                    "header": q.get("header", ""),
                    "options": [
                        {"label": o.get("label", ""), "description": o.get("description", "")}
                        for o in (q.get("options") or [])
                        if isinstance(o, dict)
                    ],
                    "multiple": bool(q.get("multiSelect", False)),
                    "custom": True,
                }
                for q in raw_questions
                if isinstance(q, dict)
            ]
            await queue.put(QuestionAsked(request_id=request_id, questions=questions))
            try:
                answers = await future
            finally:
                self._pending_questions.pop(request_id, None)
            if not answers:
                return PermissionResultDeny(message="User declined to answer the questions.")
            # answers is one label-list per question; the tool keys its result on
            # the question text and wants multi-select answers comma-joined.
            answers_record: dict[str, str] = {}
            for question, selected in zip(raw_questions, answers, strict=False):
                if isinstance(question, dict):
                    answers_record[question.get("question", "")] = ", ".join(selected)
            updated = dict(input_data)
            updated["answers"] = answers_record
            return PermissionResultAllow(updated_input=updated)

        async def can_use_tool(
            tool_name: str, input_data: dict[str, Any], ctx: Any
        ) -> PermissionResultAllow | PermissionResultDeny:
            if tool_name == "AskUserQuestion":
                return await ask_user_question(input_data)
            norm = _normalize_input(input_data)
            category = _category(tool_name)
            # Pre-approve (or deny) against the context's opt-in ruleset in
            # process; only "ask" falls through to the human via the streamer.
            effect = evaluate(category, _eval_target(category, norm), ruleset)
            if effect == "allow":
                return PermissionResultAllow()
            if effect == "deny":
                return PermissionResultDeny(message="Denied by the context's tool policy.")
            request_id = f"perm_{uuid.uuid4().hex[:16]}"
            metadata: dict[str, Any] = {}
            if category == "edit" and norm.get("filePath"):
                metadata = {"files": [{"filePath": norm["filePath"]}]}
            future: asyncio.Future[tuple[bool, str | None]] = loop.create_future()
            self._pending_perms[request_id] = future
            owned_perms.add(request_id)
            await queue.put(
                PermissionRequested(
                    request_id=request_id,
                    category=category,
                    tool=_wire_tool(tool_name),
                    input=norm,
                    metadata=metadata,
                    call_id=getattr(ctx, "tool_use_id", None),
                )
            )
            try:
                allow, message = await future
            finally:
                self._pending_perms.pop(request_id, None)
            if allow:
                return PermissionResultAllow()
            return PermissionResultDeny(message=message or "Denied by the user.")

        def handle_tool_result(block: ToolResultBlock) -> None:
            wire, inp = tool_calls.get(block.tool_use_id, (block.tool_use_id, {}))
            is_error = bool(block.is_error)
            if wire in _TASK_TOOLS and not is_error:
                # Task-list bookkeeping: fold into the mirror and re-emit as a
                # todowrite update for the live checklist; the raw call stays
                # out of the tool stream. A failed call falls through below so
                # the error is still loud.
                todos = _apply_task_result(task_mirror, wire, inp, block.content)
                if todos is not None:
                    queue.put_nowait(
                        ToolUpdated(
                            call_id=f"tasks:{block.tool_use_id}",
                            tool="todowrite",
                            input={"todos": todos},
                            status="completed",
                        )
                    )
                return
            queue.put_nowait(
                ToolUpdated(
                    call_id=block.tool_use_id,
                    tool=wire,
                    input=inp,
                    status="error" if is_error else "completed",
                    output=None if is_error else block.content,
                    error=block.content if is_error else None,
                )
            )

        mcp_servers, allowed_tools = self._mcp_setup(turn)

        async def _user_message(prompt: str, files: list[Any]) -> dict[str, Any]:
            # Attachments are written into the workspace before the message is
            # built, so the manifest can name a path that already exists. Only a
            # few types can be shown to the model directly; for the rest the file
            # on disk *is* the delivery mechanism, so this runs before every turn
            # and every mid-turn follow-up. Off-thread: a 20 MB write should not
            # stall the event loop that is streaming the reply.
            if files:
                files = await asyncio.to_thread(save_attachments, files, turn.directory)
            return {
                "type": "user",
                "session_id": "",
                "message": {"role": "user", "content": _content_blocks(prompt, files)},
                "parent_tool_use_id": None,
            }

        async def input_stream() -> AsyncIterator[dict[str, Any]]:
            # can_use_tool requires streaming-input mode: the prompt is an async
            # iterable of user-message dicts. Send the turn message, then relay
            # whatever the driver forwards (mid-turn follow-ups) until it sends the
            # ``None`` sentinel to close — holding the stream open in between so the
            # bidirectional control channel (permission/question requests) stays
            # alive.
            yield await _user_message(turn.prompt, turn.files or [])
            # The driver forwards a follow-up's *ingredients*, not a built message,
            # so that saving its attachments — which awaits — happens here rather
            # than inside the driver's take()/close() boundary, whose atomicity is
            # what stops a racing follow-up from being both taken and bounced.
            while (follow := await outbound.get()) is not None:
                yield await _user_message(*follow)

        async def driver() -> None:
            nonlocal cur_msg_id, model_called
            options = self._build_options(turn, can_use_tool, mcp_servers, allowed_tools)
            stream = self._query(prompt=input_stream(), options=options)
            try:
                async for message in stream:
                    disarm_idle_guard()
                    # The task messages subclass SystemMessage, so they have to be
                    # matched before the generic branch or they fall into it.
                    if isinstance(
                        message,
                        (TaskStartedMessage, TaskUpdatedMessage, TaskNotificationMessage),
                    ):
                        maybe_session(message.session_id)
                        if isinstance(message, TaskStartedMessage):
                            changed = live_tasks.started(message)
                        elif isinstance(message, TaskUpdatedMessage):
                            changed = live_tasks.updated(message)
                        else:
                            changed = live_tasks.notified(message)
                        if changed:
                            await queue.put(BackgroundTasksChanged(tasks=live_tasks.tasks))

                    elif isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            maybe_session(message.data.get("session_id"))

                    elif isinstance(message, StreamEvent):
                        maybe_session(message.session_id)
                        ev = message.event
                        etype = ev.get("type")
                        if etype == "message_start":
                            model_called = True
                            cur_msg_id = (ev.get("message") or {}).get("id")
                            block_text.clear()
                        elif etype == "content_block_delta":
                            idx = ev.get("index")
                            delta = ev.get("delta") or {}
                            dtype = delta.get("type")
                            if dtype == "text_delta":
                                block_text[idx] = block_text.get(idx, "") + delta.get("text", "")
                                await queue.put(
                                    TextUpdated(
                                        part_id=f"{cur_msg_id}:{idx}",
                                        text=block_text[idx],
                                        message_id=cur_msg_id,
                                    )
                                )
                            elif dtype == "thinking_delta":
                                block_text[idx] = block_text.get(idx, "") + delta.get(
                                    "thinking", ""
                                )
                                await queue.put(
                                    ReasoningUpdated(
                                        part_id=f"{cur_msg_id}:{idx}",
                                        text=block_text[idx],
                                        message_id=cur_msg_id,
                                    )
                                )

                    elif isinstance(message, AssistantMessage):
                        maybe_session(message.session_id)
                        model_called = True
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                wire = _wire_tool(block.name)
                                inp = _normalize_input(block.input)
                                tool_calls[block.id] = (wire, inp)
                                if wire in _TASK_TOOLS:
                                    # Mirrors into the checklist on result; the
                                    # raw call never enters the tool stream.
                                    continue
                                await queue.put(
                                    ToolUpdated(
                                        call_id=block.id, tool=wire, input=inp, status="running"
                                    )
                                )
                            elif isinstance(block, ToolResultBlock):
                                handle_tool_result(block)
                            # TextBlock/ThinkingBlock already streamed via StreamEvent.

                    elif isinstance(message, UserMessage):
                        if isinstance(message.content, list):
                            for block in message.content:
                                if isinstance(block, ToolResultBlock):
                                    handle_tool_result(block)

                    elif isinstance(message, RateLimitEvent):
                        # RateLimitEvent is emitted routinely with quota status;
                        # only surface a notice when actually throttled, not for
                        # the "allowed"/"allowed_warning" informational updates.
                        info = message.rate_limit_info
                        if getattr(info, "status", None) == "rejected":
                            detail = "the model provider is rate-limited"
                            if getattr(info, "rate_limit_type", None):
                                detail += f" ({info.rate_limit_type})"
                            await queue.put(RetryNotice(detail=detail))

                    elif isinstance(message, ResultMessage):
                        maybe_session(message.session_id)
                        if _is_foreign_result(message, model_called=model_called):
                            # Not our prompt: the CLI queues prompts of its own
                            # (on resume its orphan-task scan injects a
                            # ``<task-notification>``) and answers each with its
                            # own ResultMessage. Ending the turn here would
                            # finalize an empty reply and tear the query down
                            # while the model is still answering the real prompt.
                            # Keep consuming, but arm a guard so a result we
                            # misjudge can't hang the topic forever.
                            logger.info(
                                "ignoring a ResultMessage for a prompt Balam did not send "
                                "(subtype=%s, num_turns=%s)",
                                message.subtype,
                                message.num_turns,
                            )
                            arm_idle_guard()
                            continue
                        model_called = False
                        if message.is_error:
                            # An error ends the whole turn, mid-turn follow-ups
                            # notwithstanding.
                            detail = message.result or "; ".join(message.errors or [])
                            await queue.put(
                                TurnFailed(
                                    message=detail or f"the agent errored ({message.subtype})"
                                )
                            )
                            return
                        # This response finished. If a follow-up arrived mid-turn,
                        # fold it into the same session: forward it and keep the
                        # turn open (the streamer finalizes the current answer on
                        # TurnStepFinished, then streams the next one). Otherwise
                        # end the turn. take()==None → close() is one atomic block
                        # (no await between), so a follow-up racing the boundary is
                        # either taken here or bounced by offer() to the next turn.
                        follow = channel.take() if channel is not None else None
                        if follow is not None:
                            outbound.put_nowait((follow.prompt, follow.files))
                            await queue.put(TurnStepFinished())
                            continue
                        # The model has answered, but background work it started is
                        # still running. Closing stdin now would wind the CLI down
                        # and kill that work (verified: a task survives exactly as
                        # long as the client stays connected). So hold the turn
                        # open. The CLI wakes the model when a task finishes, and
                        # that report arrives as an ordinary assistant turn —
                        # TurnStepFinished commits the answer so far, so the report
                        # lands in the topic as its own message.
                        if live_tasks.tasks:
                            arm_hold_watchdog()
                            await queue.put(TurnStepFinished())
                            continue
                        if channel is not None:
                            channel.close()
                        outbound.put_nowait(None)
                        await queue.put(TurnFinished())
                        return
            except Exception as exc:
                logger.exception("Claude Agent SDK query failed")
                await queue.put(TurnFailed(message=str(exc) or exc.__class__.__name__))
            finally:
                disarm_idle_guard()
                disarm_hold_watchdog()
                # Refuse further mid-turn offers (they fall back to the bot's turn
                # queue) and release the input stream so it can close stdin cleanly.
                if channel is not None:
                    channel.close()
                outbound.put_nowait(None)
                # Close the SDK query generator (and its subprocess) within this
                # task, so it doesn't aclose at GC time and collide with the loop.
                if hasattr(stream, "aclose"):
                    try:
                        await stream.aclose()
                    except Exception:
                        logger.debug("error closing SDK query stream", exc_info=True)
                await queue.put(_SENTINEL)

        driver_task = asyncio.create_task(driver())
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            disarm_idle_guard()
            disarm_hold_watchdog()
            if not driver_task.done():
                driver_task.cancel()
            # Unblock any can_use_tool still awaiting a decision so the cancelled
            # driver can unwind instead of hanging on a future no one will resolve.
            for request_id in list(owned_perms):
                future = self._pending_perms.pop(request_id, None)
                if future is not None and not future.done():
                    future.cancel()
            for request_id in list(owned_questions):
                qfuture = self._pending_questions.pop(request_id, None)
                if qfuture is not None and not qfuture.done():
                    qfuture.cancel()
            await asyncio.gather(driver_task, return_exceptions=True)
