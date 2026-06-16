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
from balam.agent_tools import AgentTool
from balam.contexts import ContextConfig
from balam.permissions import build_ruleset, evaluate

logger = logging.getLogger(__name__)

#: Pushed by the driver's ``finally`` to tell ``run_turn`` the stream is done.
_SENTINEL = None

#: SDK tool name → OpenCode wire tool name, for display/rendering. Aligns the
#: streamer's renderer (which special-cases ``bash`` etc. by the OpenCode
#: vocabulary). Unknown names (MCP tools) fall through unchanged.
_WIRE_TOOL: dict[str, str] = {
    "Read": "read",
    "Bash": "bash",
    "Edit": "edit",
    "Write": "write",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Glob": "glob",
    "Grep": "grep",
    "LS": "list",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "Task": "task",
    "TodoWrite": "todowrite",
}

#: SDK tool name → Balam permission *category* (what :func:`balam.approvals.decide`
#: keys on). Every file mutation collapses to ``edit``; unknown tools keep their
#: name so the boundary policy treats them as "ask".
_CATEGORY: dict[str, str] = {
    "Read": "read",
    "Bash": "bash",
    "Edit": "edit",
    "Write": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Glob": "glob",
    "Grep": "grep",
    "LS": "list",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "Task": "task",
    "TodoWrite": "todowrite",
}


def _wire_tool(name: str) -> str:
    return _WIRE_TOOL.get(name, name)


def _category(name: str) -> str:
    return _CATEGORY.get(name, name)


def _normalize_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Bridge the SDK's input keys to the OpenCode shape the streamer expects.

    The streamer's path/boundary logic reads ``filePath`` (OpenCode's camelCase);
    the SDK uses ``file_path``. Mirror it so reads/edits resolve and render.
    """
    if "file_path" in tool_input and "filePath" not in tool_input:
        out = dict(tool_input)
        out["filePath"] = out["file_path"]
        return out
    return tool_input


def coerce_sdk_mcp_config(name: str, raw_config: Any) -> dict[str, Any]:
    """Normalise one context MCP server entry into the SDK's ``mcp_servers`` shape.

    Mirrors :func:`balam.opencode.coerce_mcp_config` but targets the SDK's TypedDicts:
    stdio ``{"type":"stdio","command","args","env"}`` and remote
    ``{"type":"sse"|"http","url","headers"}``. The same loose ``config.yaml``
    spellings are accepted (already env-expanded + shape-validated at load).
    """
    if not isinstance(raw_config, dict):
        raise ValueError(f"MCP server {name!r} config must be a mapping")
    config = dict(raw_config)

    # `command: "uvx"` + `args: [...]` shorthand → an stdio server.
    if "command" in config and config.get("type") not in {"local", "remote", "http", "sse"}:
        command = config["command"]
        if not isinstance(command, str) or not command:
            raise ValueError(f"MCP server {name!r} command must be a non-empty string")
        out: dict[str, Any] = {"type": "stdio", "command": command}
        args = config.get("args", [])
        if args:
            out["args"] = [str(a) for a in args]
        env = config.get("env", config.get("environment"))
        if env:
            out["env"] = {str(k): str(v) for k, v in env.items()}
        return out

    cfg_type = config.get("type")
    if cfg_type == "local":
        command = config.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError(f"MCP server {name!r} local command must be a non-empty list")
        out = {"type": "stdio", "command": str(command[0])}
        if len(command) > 1:
            out["args"] = [str(a) for a in command[1:]]
        env = config.get("environment", config.get("env"))
        if env:
            out["env"] = {str(k): str(v) for k, v in env.items()}
        return out

    if cfg_type in {"remote", "http", "sse"}:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"MCP server {name!r} remote config requires a url")
        # OpenCode collapses http/sse to "remote"; default that to http here.
        out = {"type": "sse" if cfg_type == "sse" else "http", "url": url}
        headers = config.get("headers")
        if isinstance(headers, dict):
            out["headers"] = {str(k): str(v) for k, v in headers.items()}
        return out

    raise ValueError(f"MCP server {name!r} must be local (command) or remote (url)")


def _eval_target(category: str, tool_input: dict[str, Any]) -> str:
    """The resource a tool call acts on, for :func:`evaluate` (leading slash
    stripped to match ``build_ruleset``'s file-path patterns)."""
    if category == "bash":
        return tool_input.get("command") or "*"
    path = tool_input.get("filePath") or tool_input.get("path")
    if isinstance(path, str) and path:
        return path[1:] if path.startswith("/") else path
    return "*"


SendFileFactory = Callable[[int, int | None], "AgentTool | None"]
QueryFn = Callable[..., AsyncIterator[Any]]


class ClaudeSdkBackend:
    """Drive the Claude Agent SDK as an :class:`~balam.agent.backend.AgentBackend`.

    ``query_fn`` is injectable so tests can drive turns without spawning the real
    ``claude`` subprocess.
    """

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
                servers[name] = coerce_sdk_mcp_config(name, raw)
            except ValueError:
                logger.warning("skipping unusable MCP server %r for the SDK backend", name)

        allowed: list[str] = []
        if self._send_file_factory is not None and turn.chat_id is not None:
            agent_tool = self._send_file_factory(turn.chat_id, turn.thread_id)
            if agent_tool is not None:
                sdk_tool = tool(agent_tool.name, agent_tool.description, agent_tool.input_schema)(
                    agent_tool.handler
                )
                servers["balam"] = create_sdk_mcp_server(name="balam", tools=[sdk_tool])
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
        kwargs: dict[str, Any] = {
            "cwd": turn.directory,
            # Plan mode gates writes and lets the agent call ExitPlanMode when it
            # is ready to build; a default turn keeps native natural-language
            # planning available without forcing the formal mode.
            "permission_mode": "plan" if turn.plan_mode else "default",
            "can_use_tool": can_use_tool,
            "include_partial_messages": True,
            # Keep Claude Code's native behavior (incl. natural-language planning).
            "system_prompt": {"type": "preset", "preset": "claude_code"},
            "env": env,
        }
        if turn.session_id:
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
        # Per-streaming-message block accumulators (StreamEvent partials).
        cur_msg_id: str | None = None
        block_text: dict[int, str] = {}
        owned_perms: set[str] = set()
        owned_questions: set[str] = set()

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

        async def ask_plan_exit(
            input_data: dict[str, Any],
        ) -> PermissionResultAllow | PermissionResultDeny:
            """Surface ExitPlanMode as a Yes/No plan-approval question. "Yes"
            allows the agent to leave plan mode and build in this same turn; the
            streamer also drops the sticky plan flag so later turns run normally.
            "No" denies it, keeping the agent in planning."""
            request_id = f"q_{uuid.uuid4().hex[:16]}"
            future: asyncio.Future[list[list[str]] | None] = loop.create_future()
            self._pending_questions[request_id] = future
            owned_questions.add(request_id)
            await queue.put(
                QuestionAsked(
                    request_id=request_id,
                    questions=[
                        {
                            "question": "The plan is complete. Build it?",
                            "header": "Plan",
                            "options": [{"label": "Yes"}, {"label": "No"}],
                            "multiple": False,
                            "custom": False,
                        }
                    ],
                    plan_text=input_data.get("plan"),
                )
            )
            try:
                answers = await future
            finally:
                self._pending_questions.pop(request_id, None)
            if answers and answers[0] == ["Yes"]:
                return PermissionResultAllow()
            return PermissionResultDeny(message="Keep planning; the plan was not approved.")

        async def can_use_tool(
            tool_name: str, input_data: dict[str, Any], ctx: Any
        ) -> PermissionResultAllow | PermissionResultDeny:
            if tool_name == "ExitPlanMode":
                return await ask_plan_exit(input_data)
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

        async def driver() -> None:
            nonlocal cur_msg_id
            options = self._build_options(turn, can_use_tool, mcp_servers, allowed_tools)
            try:
                async for message in self._query(prompt=turn.prompt, options=options):
                    if isinstance(message, SystemMessage):
                        if message.subtype == "init":
                            maybe_session(message.data.get("session_id"))

                    elif isinstance(message, StreamEvent):
                        maybe_session(message.session_id)
                        ev = message.event
                        etype = ev.get("type")
                        if etype == "message_start":
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
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                wire = _wire_tool(block.name)
                                inp = _normalize_input(block.input)
                                tool_calls[block.id] = (wire, inp)
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
                        await queue.put(RetryNotice(detail="the model provider is rate-limited"))

                    elif isinstance(message, ResultMessage):
                        maybe_session(message.session_id)
                        if message.is_error:
                            detail = message.result or "; ".join(message.errors or [])
                            await queue.put(
                                TurnFailed(
                                    message=detail or f"the agent errored ({message.subtype})"
                                )
                            )
                        else:
                            await queue.put(TurnFinished())
                        return
            except Exception as exc:
                logger.exception("Claude Agent SDK query failed")
                await queue.put(TurnFailed(message=str(exc) or exc.__class__.__name__))
            finally:
                await queue.put(_SENTINEL)

        driver_task = asyncio.create_task(driver())
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
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
