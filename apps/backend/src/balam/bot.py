"""The Telegram bot: the system's trust boundary (ADR-0008).

Two responsibilities for this slice:
  1. Allowlist — accept updates only from the single owner's numeric user ID;
     everyone else is silently ignored (a stranger's update matches no handler).
  2. Route messages — map the topic to its OpenCode session (ADR-0009), forward
     text plus any image/document attachments (§4), and stream the agent's reply
     back into the same topic.
  3. Handle ``/context`` — list workspaces, or open a new topic bound to one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from balam import schedules
from balam.agent.backend import AgentBackend, FollowUp, FollowUpChannel
from balam.approvals import (
    Choice,
    CustomAnswer,
    PendingApprovals,
    PendingPicks,
    PendingQuestions,
)
from balam.attachments import PromptFile, collect_attachments
from balam.config import Config
from balam.contexts import EFFORT_LEVELS, split_provider_model
from balam.markdown import escape_markdown_v2
from balam.message_text import (
    forward_reply_prefix,
    forwarded_slash_command,
    strip_bot_mention_from_command,
)
from balam.miniapp import mini_app_reply
from balam.router import Router, TopicRef
from balam.schedules import describe
from balam.store import SessionStore
from balam.streamer import _question_keyboard, stream_reply
from balam.telegram_utils import thread_kwargs
from balam.topics import (
    auto_name_topic,
    create_topic_from_general,
    is_forum_general_message,
    open_context_topic,
    rename_forum_topic,
    topic_label,
    topic_title,
)
from balam.turns import TurnJob, TurnRegistry

logger = logging.getLogger(__name__)

APPROVAL_DELETE_DELAY_S = 2.0


def is_owner(from_id: int | None, allowed_user_id: int) -> bool:
    """The allowlist check, isolated for testing (ADR-0008)."""
    return from_id is not None and from_id == allowed_user_id


async def _notify_error(bot: Any, chat_id: int, thread_id: int | None, exc: Exception) -> None:
    """Post a short error notice into the topic (ADR-0009 edge), swallowing any
    delivery failure so it never masks the original error."""
    try:
        await bot.send_message(chat_id=chat_id, text=f"⚠️ {exc}", **thread_kwargs(thread_id))
    except Exception:
        logger.debug("failed to deliver error notice", exc_info=True)


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    chat_id = message.chat_id
    thread_id = message.message_thread_id
    is_slash_command = forwarded_slash_command(message)
    text = message.text or message.caption or ""
    # An unregistered slash command (e.g. ``/goal``) reaches us via the catch-all
    # handler so it can pass through to the agent as a Claude slash command. Telegram
    # may address it as ``/goal@thisbot`` in groups; strip our own @-mention from the
    # leading command token so the agent sees a clean ``/goal``.
    text = strip_bot_mention_from_command(text, getattr(context.bot, "username", None))

    pending_questions: PendingQuestions | None = context.application.bot_data.get(
        "pending_questions"
    )
    if text and pending_questions is not None:
        custom_result = pending_questions.resolve_custom(chat_id, thread_id, text)
        if custom_result is not None and custom_result.status == "resolved":
            await _show_custom_answer_on_question(context.bot, chat_id, custom_result)
            await message.reply_text("✅ Answer sent.")
            return
        if custom_result is not None and custom_result.status == "added":
            await message.reply_text("✅ Custom answer added. Select more options or tap Done.")
            return

    # Download any image/document attachments as native file parts (tier-1 plan §4);
    # the text is the message text or an attachment's caption.
    try:
        files = await collect_attachments(message, context.bot)
    except Exception as exc:
        logger.exception("failed to download attachment")
        await _notify_error(context.bot, chat_id, thread_id, exc)
        return
    if not text and not files:
        return

    router: Router = context.application.bot_data["router"]

    if is_forum_general_message(message):
        created_thread_id = await create_topic_from_general(
            message,
            context.bot,
            router,
            text,
            has_files=bool(files),
        )
        if created_thread_id is None:
            return
        thread_id = created_thread_id

    # Show a deterministic marker so the owner can see a slash command was routed to
    # the agent as a command (vs. treated as plain text). Expansion itself is invisible
    # in the SDK stream, but this fires exactly when Balam forwards a command, and the
    # agent's own ``Unknown command: /x`` reply closes the loop for a bad command.
    if is_slash_command:
        command = text.split(maxsplit=1)[0]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⌘ forwarding slash command {command} to the agent",
            **thread_kwargs(thread_id),
        )

    # Surface the owner's forward/reply gestures to the agent as a bracketed header
    # on the prompt (Telegram drops that metadata otherwise). Never for a slash
    # command — the agent expands ``/goal`` only when it leads the prompt.
    prompt_prefix = "" if is_slash_command else forward_reply_prefix(message)
    await _submit_turn(
        message, context, text, files, thread_id=thread_id, prompt_prefix=prompt_prefix
    )


async def _submit_turn(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    files: list[PromptFile],
    *,
    thread_id: int | None,
    queued_reply: str = "⏳ Queued (#{position}) — I'll run this after the current turn finishes.",
    prompt_prefix: str = "",
) -> None:
    """Run ``text`` as the topic's turn, or park it in the topic's queue when a
    turn is already streaming.

    The message-bound dispatch tail: it owns everything that needs a
    :class:`~telegram.Message` — the topic title, the follow-up acknowledgement,
    and the queued-turn reply. Resolving the session and building the job is
    :func:`_resolve_turn_job`; :func:`start_prompt` is the same path for a caller
    with no message (the scheduled runs of ADR-0016).

    ``thread_id`` is explicit because a General message has already been rehomed
    into a freshly created topic by the time it gets here; ``queued_reply`` is
    formatted with the job's 1-based queue ``position``. ``prompt_prefix`` is
    prepended only to the agent-facing prompt (forward/reply header) — topic
    auto-naming still uses the owner's own ``text``.
    """
    turns: TurnRegistry = context.application.bot_data["turns"]
    chat_id = message.chat_id

    job = await _resolve_turn_job(
        context,
        chat_id,
        thread_id,
        text,
        title=topic_title(message, thread_id),
        files=files,
        prompt_prefix=prompt_prefix,
    )
    if job is None:
        return

    # A message that lands while a turn is still streaming can't fire a second
    # prompt at the same session — one turn per topic (ADR-0009). Two paths, both
    # decided with no ``await`` between the check and the act so the running
    # turn's teardown can't race in and lose the message:
    #
    #  * Streaming-input backend (the SDK): fold it into the LIVE turn so the
    #    agent picks it up at its next step (Claude Code-style). ``offer`` returns
    #    False only if that turn is already closing, in which case we fall through
    #    to the queue and it runs as the next turn.
    #  * Otherwise (OpenCode): park it in the topic's FIFO queue; the running turn
    #    drains it when it finishes.
    running = turns.get(chat_id, thread_id)
    if running is not None:
        backend: AgentBackend = context.application.bot_data["backend"]
        if (
            backend.supports_streaming_input
            and running.follow_ups is not None
            and running.follow_ups.offer(FollowUp(prompt=text, files=files))
        ):
            await message.reply_text("📨 Sent — I'll pick this up in the current turn.")
            return
        position = turns.enqueue(chat_id, thread_id, job)
        await message.reply_text(queued_reply.format(position=position))
        return

    _start_turn(context, chat_id, thread_id, job)


async def _resolve_turn_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    thread_id: int | None,
    text: str,
    *,
    title: str,
    files: list[PromptFile] | None = None,
    prompt_prefix: str = "",
    unattended: bool = False,
) -> TurnJob | None:
    """Resolve the topic's session, auto-name the topic if it still needs one, and
    package everything a turn needs. ``None`` (with a notice already posted in the
    topic) if the session couldn't be resolved at all — OpenCode down, and so on.

    Split out of :func:`_submit_turn` so a caller with no originating message can
    build the same job: the queue/follow-up decision above needs the job in hand
    before it can choose, so it can't simply delegate to :func:`start_prompt`.
    ``prompt_prefix`` is prepended only to the agent-facing prompt (the
    forward/reply header) — auto-naming still uses the owner's own ``text``.
    """
    router: Router = context.application.bot_data["router"]
    files = files or []
    try:
        ref = TopicRef(chat_id=chat_id, thread_id=thread_id, title=title)
        resolved = await router.resolve(ref)
        await auto_name_topic(
            context.bot,
            router,
            ref,
            resolved.context_name,
            text,
            has_files=bool(files),
        )
    except Exception as exc:
        # Couldn't even resolve the session (OpenCode down, etc.) — report and stop.
        logger.exception("failed to resolve session")
        await _notify_error(context.bot, chat_id, thread_id, exc)
        return None

    return TurnJob(
        prompt=f"{prompt_prefix}{text}" if prompt_prefix else text,
        session_id=resolved.session_id,
        directory=resolved.directory,
        provider=resolved.provider,
        model=resolved.model,
        effort=resolved.effort,
        allowed_dirs=[resolved.directory, *resolved.additional_directories],
        files=files,
        allowed_tools=resolved.allowed_tools,
        additional_directories=resolved.additional_directories,
        mcp=resolved.mcp,
        unattended=unattended,
    )


async def start_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    thread_id: int | None,
    prompt: str,
    *,
    title: str,
    unattended: bool = False,
) -> bool:
    """Run ``prompt`` as a turn in an existing topic, with no originating message.

    The message-free half of :func:`_submit_turn`, used by the scheduled path
    (ADR-0016). It deliberately has no queue branch: a scheduled run targets a
    *brand-new* topic, so it can never collide with a turn already running there.
    Returns whether the turn started.
    """
    job = await _resolve_turn_job(
        context, chat_id, thread_id, prompt, title=title, unattended=unattended
    )
    if job is None:
        return False
    _start_turn(context, chat_id, thread_id, job)
    return True


def _start_turn(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    thread_id: int | None,
    job: TurnJob,
) -> None:
    """Run ``job`` as the topic's turn in a background task, then hand the running
    slot to the next queued message when it finishes.

    The turn runs as a background task registered in the turn registry, so the
    message handler returns immediately and a concurrent ``/cancel`` update can
    interrupt it (PTB processes updates sequentially, so awaiting in the handler
    would block ``/cancel``).
    """
    backend: AgentBackend = context.application.bot_data["backend"]
    turns: TurnRegistry = context.application.bot_data["turns"]
    pending: PendingApprovals = context.application.bot_data["pending"]
    router: Router = context.application.bot_data["router"]

    # Mid-turn messages fold into this live turn only on a streaming-input backend
    # (the SDK). On OpenCode the channel stays None, so they queue instead.
    follow_ups = FollowUpChannel() if backend.supports_streaming_input else None

    # Config is optional here so unit tests of the bot path can omit it; the
    # streamer's defaults then apply.
    config: Config | None = context.application.bot_data.get("config")

    async def run() -> None:
        cancelled = False
        try:
            await stream_reply(
                bot=context.bot,
                backend=backend,
                session_id=job.session_id,
                chat_id=chat_id,
                thread_id=thread_id,
                prompt=job.prompt,
                directory=job.directory,
                provider=job.provider,
                model=job.model,
                effort=job.effort,
                pending=pending,
                pending_questions=context.application.bot_data.setdefault(
                    "pending_questions", PendingQuestions()
                ),
                allowed_dirs=job.allowed_dirs,
                additional_directories=job.additional_directories,
                allowed_tools=job.allowed_tools,
                mcp=job.mcp,
                files=job.files,
                on_session_started=lambda sid: router.persist_session(chat_id, thread_id, sid),
                follow_ups=follow_ups,
                tool_stream=config.tool_stream if config is not None else "collapsed",
                rich_messages=config.rich_messages if config is not None else False,
                unattended=job.unattended,
            )
        except asyncio.CancelledError:
            cancelled = True  # /cancel aborted the turn; don't auto-run queued work.
            raise
        except Exception as exc:
            logger.exception("failed to handle message")
            await _notify_error(context.bot, chat_id, thread_id, exc)
        finally:
            # Release the slot and hand it straight to the next queued message.
            # clear → pop → _start_turn run without an ``await`` between them, so
            # the slot never blinks empty and a concurrent message can't slip a
            # second turn onto the same session.
            turns.clear(chat_id, thread_id, task)
            next_job = None if cancelled else turns.pop_next(chat_id, thread_id)
            if next_job is not None:
                _start_turn(context, chat_id, thread_id, next_job)

    task = asyncio.create_task(run())
    turns.register(chat_id, thread_id, task, job.session_id, job.directory, follow_ups)


def _command_remainder(text: str, *, args_consumed: int = 0) -> str:
    """Everything after the leading ``/command`` and ``args_consumed`` argument
    tokens, as the owner typed it.

    ``context.args`` is a whitespace split, so a multi-line prompt would come
    back collapsed onto one line; commands that forward a prompt to the agent
    take it from the raw text instead.
    """
    rest = text.lstrip()
    for _ in range(1 + args_consumed):
        parts = rest.split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else ""
    return rest.strip()


async def _handle_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/context`` lists workspaces and the topic's current binding;
    ``/context <name> [prompt]`` creates a *new* topic bound to that context,
    replies with a one-tap link to it (see :func:`open_context_topic`), and runs
    ``prompt`` there when one is given."""
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
    )
    contexts = router.contexts
    args = context.args or []

    if not args:
        current = router.current_context_name(ref)
        lines = ["Workspace contexts:"]
        for name, ctx in sorted(contexts.contexts.items()):
            marker = "→" if name == current else "•"
            lines.append(f"{marker} {name} — {ctx.description} ({ctx.directory})")
        lines.append("")
        lines.append("Switch with /context <name> (opens a new topic).")
        await message.reply_text("\n".join(lines))
        return

    name = contexts.match_name(args[0])
    if name is None:
        available = ", ".join(sorted(contexts.contexts))
        await message.reply_text(f"Unknown context {args[0]!r}. Available: {available}")
        return

    prompt = _command_remainder(message.text or "", args_consumed=1)
    thread_id = await open_context_topic(message, context.bot, router, name, prompt=prompt)
    if thread_id is not None and prompt:
        await _submit_turn(message, context, prompt, [], thread_id=thread_id)


def _abort_turn(
    turn: Any, backend: AgentBackend, tasks: set[asyncio.Task[None]]
) -> asyncio.Task[None] | None:
    """Cancel a running turn locally and abort it on the backend (best-effort).

    Cancelling the local task stops streaming; the abort tells the backend to
    stop generating. The abort runs as a background task so callers needn't await
    the round-trip before replying — but it is anchored in ``tasks`` (with a done
    callback that removes it) because the event loop keeps only a *weak*
    reference to a bare task: an unanchored one can be garbage-collected
    mid-flight, dropping the abort. ``None`` when there is no turn (or no session
    id yet, e.g. an SDK turn that hasn't minted one — cancelling the task is
    enough to tear down its query)."""
    if turn is None:
        return None
    turn.task.cancel()
    if not turn.session_id:
        return None
    task = asyncio.create_task(backend.abort(turn.session_id, directory=turn.directory))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def _handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/new [name] [prompt]`` — open a fresh topic with a new session, and run
    ``prompt`` in it when one is given.

    The same flow as ``/context <name>`` (:func:`open_context_topic`). With an
    argument, the new topic is bound to context ``name``; without one, it reuses
    the current topic's binding (``default_context`` when unbound). The current
    topic is left untouched — one context per topic for life — so its history is
    preserved and the fresh start lives in its own topic.

    Everything after the context name is the first turn, so ``/new monies check
    last month`` opens a *monies* topic and starts working there in one step —
    the same one-step start a plain message in General gives, with the context
    picked explicitly. The first token still has to name a context: a prompt
    alone would silently run in whichever context this topic happens to be bound
    to, and a plain message already covers that.
    """
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    args = context.args or []
    if args:
        name = router.contexts.match_name(args[0])
        if name is None:
            available = ", ".join(sorted(router.contexts.contexts))
            await message.reply_text(f"Unknown context {args[0]!r}. Available: {available}")
            return
        prompt = _command_remainder(message.text or "", args_consumed=1)
    else:
        ref = TopicRef(
            chat_id=message.chat_id,
            thread_id=message.message_thread_id,
            title=topic_title(message, message.message_thread_id),
        )
        name = router.current_context_name(ref)
        prompt = ""
    thread_id = await open_context_topic(message, context.bot, router, name, prompt=prompt)
    if thread_id is not None and prompt:
        await _submit_turn(message, context, prompt, [], thread_id=thread_id)


#: Scopes the Artifact tool's ``list`` action accepts; ``mine`` is its default.
_ARTIFACT_SCOPES = ("mine", "shared", "all")


async def _handle_artifacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/artifacts [shared|all]`` — list the owner's published claude.ai artifacts.

    The CLI's own ``/artifacts`` is a local-jsx screen that only exists in
    interactive sessions, so it can never run through the SDK backend. The data
    behind it is reachable anyway: the Artifact tool's ``action:"list"`` returns
    the published pages (title, url, updatedAt). This command submits a regular
    turn instructing the agent to call it — which also means it degrades to a
    plain "tool unavailable" answer on a backend without the Artifact tool
    (OpenCode, or an account the rollout gate excludes).
    """
    message = update.message
    if message is None:
        return

    if is_forum_general_message(message):
        # General messages spawn fresh topics, so the listing would land in a
        # topic named after a bare command.
        await message.reply_text("Use /artifacts inside a topic.")
        return

    args = context.args or []
    scope = args[0].lower() if args else "mine"
    if scope not in _ARTIFACT_SCOPES:
        await message.reply_text("Usage: /artifacts [shared|all] — default lists your own.")
        return

    prompt = (
        f'Call the Artifact tool with action "list" and scope "{scope}", then present '
        "the artifacts as a compact list: each title as a link to its URL, with the "
        "updated date when available. Mention if the result was truncated. Do not "
        "take any other action. If the Artifact tool is not available in this "
        "session, say so instead of improvising."
    )
    await _submit_turn(
        message,
        context,
        prompt,
        [],
        thread_id=message.message_thread_id,
    )


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/status`` — report the topic's context, session, and whether a turn runs."""
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    turns: TurnRegistry = context.application.bot_data["turns"]
    config: Config = context.application.bot_data["config"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
    )

    name = router.current_context_name(ref)
    ctx = router.contexts.get(name)
    provider, model = ctx.provider_model
    override_provider, override_model = router.model_override(ref.chat_id)
    override_effort = router.effort_override(ref.chat_id)
    session_id = router.current_session_id(ref)
    running = turns.get(ref.chat_id, ref.thread_id) is not None
    queued = turns.queue_len(ref.chat_id, ref.thread_id)
    effective_model = _format_model(override_provider or provider, override_model or model)

    lines = [
        f"Context: {name}",
        f"Backend: {config.agent_backend}",
        f"Directory: {ctx.directory}",
        f"Model: {effective_model}",
        f"Effort: {override_effort or ctx.effort or '(server default)'}",
        f"Session: {session_id or '(none yet — send a message to start)'}",
        f"Turn: {'running' if running else 'idle'}",
        f"Queued: {queued}",
    ]
    await message.reply_text("\n".join(lines))


def _format_model(provider: str | None, model: str | None) -> str:
    if provider and model:
        return f"{provider}/{model}"
    return "(server default)"


async def _handle_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/model [provider/model|reset]`` — inspect or set the chat-wide model.

    Reading works anywhere; **setting** is General-only, because the override is
    global rather than per topic (:data:`balam.router._GLOBAL_THREAD`). Keeping it
    out of topics is what lets a long-lived agent session keep one model for its
    whole life instead of having to switch mid-session.
    """
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
    )
    args = context.args or []

    if not args:
        name = router.current_context_name(ref)
        provider, model = router.contexts.get(name).provider_model
        override_provider, override_model = router.model_override(ref.chat_id)
        source = (
            "global override"
            if override_model
            else "context default"
            if model
            else "server default"
        )
        await message.reply_text(
            f"Model: {_format_model(override_provider or provider, override_model or model)}\n"
            f"Source: {source}\n"
            "Set with /model <provider/model> in General, reset with /model reset."
        )
        return

    if not is_forum_general_message(message):
        await message.reply_text(
            "The model is set for the whole workspace, so change it from General."
        )
        return

    value = args[0].strip()
    if value.lower() == "reset":
        router.reset_model_override(ref.chat_id)
        name = router.current_context_name(ref)
        provider, model = router.contexts.get(name).provider_model
        await message.reply_text(f"Model reset to {_format_model(provider, model)}.")
        return

    try:
        provider, model = split_provider_model(value)
    except ValueError as exc:
        await message.reply_text(f"{exc}\nUsage: /model <provider/model> or /model reset")
        return
    if not provider or not model:
        await message.reply_text("Usage: /model <provider/model> or /model reset")
        return

    router.set_model_override(ref.chat_id, provider, model)
    await message.reply_text(f"Model set to {provider}/{model} for every topic.")


async def _handle_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/effort [level|reset]`` — inspect or set the chat-wide effort.

    General-only to set, for the same reason as :func:`_handle_model`. Effort in
    particular cannot be changed on a live SDK client at all, so making it global
    removes the only case that would force a session to be rebuilt mid-life.
    """
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
    )
    args = context.args or []

    if not args:
        name = router.current_context_name(ref)
        ctx = router.contexts.get(name)
        override = router.effort_override(ref.chat_id)
        source = (
            "global override" if override else "context default" if ctx.effort else "server default"
        )
        await message.reply_text(
            f"Effort: {override or ctx.effort or '(server default)'}\n"
            f"Source: {source}\n"
            "Set with /effort <level> in General, reset with /effort reset."
        )
        return

    if not is_forum_general_message(message):
        await message.reply_text(
            "Effort is set for the whole workspace, so change it from General."
        )
        return

    value = args[0].strip().lower()
    if value == "reset":
        router.reset_effort_override(ref.chat_id)
        name = router.current_context_name(ref)
        ctx = router.contexts.get(name)
        await message.reply_text(f"Effort reset to {ctx.effort or '(server default)'}.")
        return

    if value not in EFFORT_LEVELS:
        allowed = ", ".join(sorted(EFFORT_LEVELS))
        await message.reply_text(f"Unknown effort {value!r}. Available: {allowed}")
        return

    router.set_effort_override(ref.chat_id, value)
    await message.reply_text(f"Effort override set to {value}.")


async def _handle_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/rename <name>`` — rename the current forum topic."""
    message = update.message
    if message is None:
        return

    new_name = " ".join(context.args or []).strip()
    if not new_name:
        await message.reply_text("Usage: /rename <topic name>")
        return
    if len(new_name) > 128:
        await message.reply_text("Topic names must be 128 characters or fewer.")
        return
    if message.message_thread_id is None:
        await message.reply_text("Use /rename inside the topic you want to rename.")
        return

    try:
        await rename_forum_topic(context.bot, message.chat_id, message.message_thread_id, new_name)
    except Exception as exc:
        logger.exception("failed to rename forum topic")
        await message.reply_text(f"⚠️ Couldn't rename this topic: {exc}")
        return

    router: Router = context.application.bot_data["router"]
    router.mark_topic_auto_named(
        TopicRef(
            chat_id=message.chat_id,
            thread_id=message.message_thread_id,
            title=topic_title(message, message.message_thread_id),
        )
    )
    router.set_topic_title(message.chat_id, message.message_thread_id, new_name)
    await message.reply_text(f"Renamed topic to {new_name}.")


async def _handle_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/diff`` — open the Mini App git diff viewer for this topic's context."""
    message = update.message
    if message is None:
        return

    config: Config = context.application.bot_data["config"]
    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
    )
    name = router.current_context_name(ref)
    text, keyboard = mini_app_reply(
        config,
        "diff",
        name,
        bot_username=getattr(context.bot, "username", None),
        is_private=getattr(message.chat, "type", None) == "private",
    )
    await message.reply_text(text, reply_markup=keyboard)


async def _handle_browser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/browser`` — open the Mini App live view of the agent's Chrome (ADR-0006).

    The view is global (one X display on the VM), not per-context, so the launch
    carries no context: a placeholder would leak into the app shell's shared
    launch context and break the other views (e.g. the diff view 404s on an
    unknown context name).
    """
    message = update.message
    if message is None:
        return

    config: Config = context.application.bot_data["config"]
    text, keyboard = mini_app_reply(
        config,
        "browser",
        None,
        bot_username=getattr(context.bot, "username", None),
        is_private=getattr(message.chat, "type", None) == "private",
        label="Watch live",
        heading="Live browser view:",
    )
    await message.reply_text(text, reply_markup=keyboard)


async def _handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/cancel`` — abort the turn running in the current topic, if any."""
    message = update.message
    if message is None:
        return

    backend: AgentBackend = context.application.bot_data["backend"]
    turns: TurnRegistry = context.application.bot_data["turns"]

    turn = turns.get(message.chat_id, message.message_thread_id)
    # Drop anything queued behind the turn too — otherwise it would auto-run right
    # after the cancelled turn settles, which is not what /cancel means.
    dropped = turns.clear_queue(message.chat_id, message.message_thread_id)
    if turn is None:
        if dropped:
            await message.reply_text(
                f"🛑 Cleared {dropped} queued message(s); no turn was running."
            )
        else:
            await message.reply_text("No running turn.")
        return

    tasks: set[asyncio.Task[None]] = context.application.bot_data.setdefault(
        "background_tasks", set()
    )
    _abort_turn(turn, backend, tasks)
    if dropped:
        await message.reply_text(f"🛑 Cancelled. Also cleared {dropped} queued message(s).")
    else:
        await message.reply_text("🛑 Cancelled.")


#: Per approval choice: ``(inline note appended to the prompt — already
#: MarkdownV2-escaped, toast shown on the callback answer)``.
_CHOICE_FEEDBACK = {
    Choice.ALLOW: ("✅ Approved\\.", "Approved."),
    Choice.ALL: (
        "✅ Approved — accepting all edits this session\\.",
        "Accepting all edits.",
    ),
    Choice.DENY: ("❌ Denied\\.", "Denied."),
}


async def _handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolve an approval inline keyboard (``appr:<choice>:<token>``).

    ``CallbackQueryHandler`` carries no ``filters``, so the ADR-0008 trust
    boundary is re-checked here by hand: the press must come from the owner (and
    the configured chat, when scoped). The matching pending future is resolved in
    :class:`PendingApprovals`; the streamer's waiting task then replies to
    OpenCode. We just acknowledge and strip the now-spent keyboard.
    """
    query = update.callback_query
    if query is None or not (query.data or "").startswith("appr:"):
        return

    config: Config = context.application.bot_data["config"]
    user = query.from_user
    if user is None or not is_owner(user.id, config.allowed_telegram_user_id):
        await query.answer()
        return
    if config.allowed_telegram_chat_id is not None:
        chat = query.message.chat if query.message else None
        if chat is None or chat.id != config.allowed_telegram_chat_id:
            await query.answer()
            return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("Malformed approval.")
        return
    _, choice_str, token = parts
    try:
        choice = Choice(choice_str)
    except ValueError:
        await query.answer("Unknown approval choice.")
        return

    pending: PendingApprovals = context.application.bot_data["pending"]
    if not pending.resolve(token, choice):
        await query.answer("This approval has expired.")
        await _clear_keyboard(query)
        return

    note, toast = _CHOICE_FEEDBACK[choice]
    await query.answer(toast)
    updated = await _clear_keyboard(query, note=note)
    if updated and choice is not Choice.DENY:
        _schedule_approval_cleanup(context, query.message)


async def _handle_question_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an OpenCode question-tool option button (``qst:<token>:i:j``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("qst:"):
        return

    config: Config = context.application.bot_data["config"]
    user = query.from_user
    if user is None or not is_owner(user.id, config.allowed_telegram_user_id):
        await query.answer()
        return
    if config.allowed_telegram_chat_id is not None:
        chat = query.message.chat if query.message else None
        if chat is None or chat.id != config.allowed_telegram_chat_id:
            await query.answer()
            return

    parts = (query.data or "").split(":", 3)
    if len(parts) != 4:
        await query.answer("Malformed question answer.")
        return
    _, token, question_index, option_index = parts
    try:
        q_index = int(question_index)
        o_index = int(option_index)
    except ValueError:
        await query.answer("Malformed question answer.")
        return

    pending_questions: PendingQuestions = context.application.bot_data["pending_questions"]
    if pending_questions.is_multiple(token, q_index):
        selected = pending_questions.toggle(token, q_index, o_index)
        if selected is None:
            await query.answer("This question has expired.")
            await _clear_keyboard(query)
            return
        await query.answer("Selected." if selected else "Unselected.")
        await _refresh_question_keyboard(query, pending_questions, token, q_index)
        return

    labels = pending_questions.labels(token, q_index)
    if not pending_questions.resolve(token, q_index, o_index):
        await query.answer("This question has expired.")
        await _clear_keyboard(query)
        return
    chosen = [labels[o_index]] if labels and 0 <= o_index < len(labels) else []
    await query.answer("Answered.")
    await _clear_keyboard(query, note=_answered_note(chosen))


async def _handle_question_done_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Resolve a multi-select OpenCode question after the user taps Done."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("qstd:"):
        return

    config: Config = context.application.bot_data["config"]
    user = query.from_user
    if user is None or not is_owner(user.id, config.allowed_telegram_user_id):
        await query.answer()
        return
    if config.allowed_telegram_chat_id is not None:
        chat = query.message.chat if query.message else None
        if chat is None or chat.id != config.allowed_telegram_chat_id:
            await query.answer()
            return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("Malformed question answer.")
        return
    _, token, question_index = parts
    try:
        q_index = int(question_index)
    except ValueError:
        await query.answer("Malformed question answer.")
        return

    pending_questions: PendingQuestions = context.application.bot_data["pending_questions"]
    chosen = pending_questions.selected_answers(token, q_index) or []
    finished = pending_questions.finish_multi(token, q_index)
    if finished is False:
        await query.answer("Select at least one option.")
        return
    if finished is None:
        await query.answer("This question has expired.")
        await _clear_keyboard(query)
        return
    await query.answer("Answered.")
    await _clear_keyboard(query, note=_answered_note(chosen))


async def _handle_question_custom_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Arm an OpenCode question prompt to use the owner's next topic message."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("qstc:"):
        return

    config: Config = context.application.bot_data["config"]
    user = query.from_user
    if user is None or not is_owner(user.id, config.allowed_telegram_user_id):
        await query.answer()
        return
    chat = query.message.chat if query.message else None
    if config.allowed_telegram_chat_id is not None:
        if chat is None or chat.id != config.allowed_telegram_chat_id:
            await query.answer()
            return
    if chat is None:
        await query.answer("Malformed question answer.")
        return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("Malformed question answer.")
        return
    _, token, question_index = parts
    try:
        q_index = int(question_index)
    except ValueError:
        await query.answer("Malformed question answer.")
        return

    thread_id = getattr(query.message, "message_thread_id", None)
    pending_questions: PendingQuestions = context.application.bot_data["pending_questions"]
    if not pending_questions.await_custom(token, q_index, chat.id, thread_id):
        await query.answer("This question has expired.")
        await _clear_keyboard(query)
        return
    if pending_questions.is_multiple(token, q_index):
        await query.answer("Send your custom answer, then tap Done.")
        return
    await query.answer("Send your answer as the next message in this topic.")
    await _clear_keyboard(query, note=r"Reply with your answer\.")


@dataclass(frozen=True)
class _PickerStyle:
    """What distinguishes one paged multi-select picker from another: its four
    callback prefixes and its confirm button's label. ``/delete`` and ``/schedule
    cancel`` share the keyboard, the paging, and :class:`PendingPicks`; only these
    differ, plus what the confirm handler does with the chosen ids."""

    toggle: str
    page: str
    confirm: str
    cancel: str
    confirm_label: str


#: Distinct prefixes per picker — PTB dispatches a callback to the first pattern
#: that matches, so two pickers must never share one.
_DELETE_PICKER = _PickerStyle("del", "delp", "deld", "delx", "🗑 Delete selected")
_SCHEDULE_PICKER = _PickerStyle("sch", "schp", "schd", "schx", "🗑 Cancel selected")


def _picker_keyboard(
    style: _PickerStyle,
    token: str,
    entries: list[tuple[int, str, bool]],
    page: int = 0,
    page_count: int = 1,
    selected_count: int = 0,
) -> InlineKeyboardMarkup:
    """Checklist for the current page (``<toggle>:<token>:<id>``), a Prev/Next
    navigation row when the snapshot spans more than one page
    (``<page>:<token>:<page>``), and the confirm/cancel row. ``selected_count``
    spans the whole snapshot, so the confirm button reflects picks made on other
    pages."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{'☑️' if selected else '☐'} {label}",
                callback_data=f"{style.toggle}:{token}:{item_id}",
            )
        ]
        for item_id, label, selected in entries
    ]
    if page_count > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀ Prev", callback_data=f"{style.page}:{token}:{page - 1}")
            )
        # The indicator points at the current page, so tapping it is a harmless no-op.
        nav.append(
            InlineKeyboardButton(
                f"Page {page + 1}/{page_count}", callback_data=f"{style.page}:{token}:{page}"
            )
        )
        if page < page_count - 1:
            nav.append(
                InlineKeyboardButton("Next ▶", callback_data=f"{style.page}:{token}:{page + 1}")
            )
        rows.append(nav)
    confirm_label = style.confirm_label
    if selected_count:
        confirm_label += f" ({selected_count})"
    rows.append(
        [
            InlineKeyboardButton(confirm_label, callback_data=f"{style.confirm}:{token}"),
            InlineKeyboardButton("Cancel", callback_data=f"{style.cancel}:{token}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _picker_markup(
    style: _PickerStyle, picks: PendingPicks, token: str
) -> InlineKeyboardMarkup | None:
    """Build a picker keyboard from current snapshot state, or ``None`` if the
    token expired."""
    entries = picks.entries(token)
    info = picks.page_info(token)
    if entries is None or info is None:
        return None
    page, page_count, _total, selected = info
    return _picker_keyboard(style, token, entries, page, page_count, selected)


async def _handle_picker_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE, style: _PickerStyle, bot_data_key: str
) -> None:
    """Toggle an item's checkbox (``<toggle>:<token>:<id>``). Shared by both
    pickers — only the prefix set and which ``bot_data`` snapshot to read differ."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith(f"{style.toggle}:"):
        return
    config: Config = context.application.bot_data["config"]
    if not _callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("Malformed selection.")
        return
    _, token, item_id_raw = parts
    try:
        item_id = int(item_id_raw)
    except ValueError:
        await query.answer("Malformed selection.")
        return

    picks: PendingPicks = context.application.bot_data[bot_data_key]
    state = picks.toggle(token, item_id)
    if state is None:
        await query.answer("This picker has expired.")
        await _clear_keyboard(query)
        return
    await query.answer("Selected." if state else "Unselected.")
    await _refresh_picker(query, style, picks, token)


async def _handle_picker_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE, style: _PickerStyle, bot_data_key: str
) -> None:
    """Flip a picker to another page (``<page>:<token>:<page>``). Selections are
    kept in the snapshot, so paging never loses what's already checked."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith(f"{style.page}:"):
        return
    config: Config = context.application.bot_data["config"]
    if not _callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("Malformed request.")
        return
    _, token, page_raw = parts
    try:
        page = int(page_raw)
    except ValueError:
        await query.answer("Malformed request.")
        return

    picks: PendingPicks = context.application.bot_data[bot_data_key]
    if picks.set_page(token, page) is None:
        await query.answer("This picker has expired.")
        await _clear_keyboard(query)
        return
    await query.answer()
    await _refresh_picker(query, style, picks, token)


async def _refresh_picker(query: Any, style: _PickerStyle, picks: PendingPicks, token: str) -> None:
    """Redraw a picker's keyboard in place; a failed edit is cosmetic only."""
    markup = _picker_markup(style, picks, token)
    message = getattr(query, "message", None)
    if markup is None or message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except Exception:
        logger.debug("failed to refresh %s keyboard", style.toggle, exc_info=True)


def _callback_authorized(query: Any, config: Config) -> bool:
    """Re-check the trust boundary (ADR-0008) for a callback: owner id, plus the
    configured chat when set. Callbacks carry no handler filter, so each must
    verify the sender itself."""
    user = query.from_user
    if user is None or not is_owner(user.id, config.allowed_telegram_user_id):
        return False
    if config.allowed_telegram_chat_id is not None:
        chat = query.message.chat if query.message else None
        if chat is None or chat.id != config.allowed_telegram_chat_id:
            return False
    return True


async def _handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/delete`` — pick forum topics to remove from an inline checklist.

    Lists the topics this bot tracks for the chat; the General topic is never
    listed (the Bot API can't delete it). Confirming deletes each selected Telegram
    topic and forgets it locally (all per-topic tables) — the OpenCode session is
    left warm on the server.
    """
    message = update.message
    if message is None:
        return
    router: Router = context.application.bot_data["router"]
    topics = router.list_topics(message.chat_id)
    if not topics:
        await message.reply_text("No topics to delete.")
        return

    pending_deletions: PendingPicks = context.application.bot_data["pending_deletions"]
    token = pending_deletions.register(
        message.chat_id,
        [(thread_id, topic_label(title, ctx, thread_id)) for thread_id, title, ctx in topics],
    )
    text = "🗑 Select topics to delete, then tap “Delete selected”."
    info = pending_deletions.page_info(token)
    if info and info[1] > 1:
        text += (
            f"\n\n{info[2]} topics across {info[1]} pages — use ◀ ▶ to browse. "
            "Selections persist across pages."
        )
    await message.reply_text(
        text, reply_markup=_picker_markup(_DELETE_PICKER, pending_deletions, token)
    )


async def _handle_delete_toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Toggle a topic's checkbox in the /delete picker (``del:<token>:<thread_id>``)."""
    await _handle_picker_toggle(update, context, _DELETE_PICKER, "pending_deletions")


async def _handle_delete_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip the /delete picker to another page (``delp:<token>:<page>``)."""
    await _handle_picker_page(update, context, _DELETE_PICKER, "pending_deletions")


async def _handle_delete_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete the topics selected in the picker (``deld:<token>``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("deld:"):
        return
    config: Config = context.application.bot_data["config"]
    if not _callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 1)
    if len(parts) != 2:
        await query.answer("Malformed request.")
        return
    token = parts[1]

    pending_deletions: PendingPicks = context.application.bot_data["pending_deletions"]
    thread_ids = pending_deletions.selected_ids(token)
    chat_id = pending_deletions.chat_id(token)
    if thread_ids is None or chat_id is None:
        await query.answer("This picker has expired.")
        await _clear_keyboard(query)
        return
    if not thread_ids:
        await query.answer("Select at least one topic.")
        return
    pending_deletions.discard(token)

    router: Router = context.application.bot_data["router"]
    deleted = 0
    failed = 0
    for thread_id in thread_ids:
        try:
            await context.bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        except BadRequest as exc:
            # A topic deleted straight from the Telegram UI (Telegram sends no
            # "topic deleted" update, so its row lingers locally) makes the API
            # reject re-deletion with TOPIC_ID_INVALID. The topic is already gone,
            # which is what the user wanted — so fall through and purge the stale
            # row instead of counting it as a permanent, un-clearable failure.
            if "topic_id_invalid" not in (exc.message or "").lower():
                logger.exception("failed to delete forum topic %s", thread_id)
                failed += 1
                continue
            logger.info("topic %s already gone from Telegram; purging stale row", thread_id)
        except Exception:
            logger.exception("failed to delete forum topic %s", thread_id)
            failed += 1
            continue
        # The Telegram topic is gone, so drop every local trace of it.
        router.purge_topic(chat_id, thread_id)
        deleted += 1

    await query.answer(f"Deleted {deleted} topic(s).")
    summary = f"🗑 Deleted {deleted} topic(s)."
    if failed:
        summary += f" {failed} could not be deleted."
    message = getattr(query, "message", None)
    if message is not None:
        # The picker message may itself sit in a just-deleted topic; ignore the edit
        # failure that follows.
        try:
            await message.edit_text(text=summary, reply_markup=None)
        except Exception:
            logger.debug("failed to finalize delete message", exc_info=True)


async def _handle_delete_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Dismiss the /delete picker without deleting anything (``delx:<token>``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("delx:"):
        return
    config: Config = context.application.bot_data["config"]
    if not _callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 1)
    if len(parts) == 2:
        context.application.bot_data["pending_deletions"].discard(parts[1])
    await query.answer("Cancelled.")
    message = getattr(query, "message", None)
    if message is not None:
        try:
            await message.edit_text(text="🗑 Delete cancelled.", reply_markup=None)
        except Exception:
            logger.debug("failed to finalize cancel message", exc_info=True)


# --- /schedule (ADR-0016) -----------------------------------------------------

#: Words that lead a ``/schedule`` sub-command rather than a recurrence. A create
#: always starts with a when-token (``daily`` / ``weekdays`` / a weekday), so the
#: two can never be confused.
_SCHEDULE_SUBCOMMANDS = ("cancel", "run", "on", "off")

_SCHEDULE_USAGE = (
    "Usage:\n"
    "/schedule — list\n"
    "/schedule daily 07:30 <context> <prompt> — create\n"
    "/schedule cancel — pick schedules to remove\n"
    "/schedule run <id> — fire one now\n"
    "/schedule off <id> · /schedule on <id> — pause / resume"
)


def _schedule_label(schedule: schedules.Schedule) -> str:
    """Button label for the /schedule cancel picker."""
    base = f"#{schedule.id} {describe(schedule.when)} · {schedule.context}"
    return base if len(base) <= 48 else base[:47] + "…"


def _format_schedule(schedule: schedules.Schedule, tz: ZoneInfo) -> str:
    """One schedule as three list lines: when/where, prompt, last run."""
    state = "" if schedule.enabled else " ⏸ paused"
    lines = [
        f"#{schedule.id} · {describe(schedule.when)} · {schedule.context}{state}",
        f"   {schedules.summarize(schedule.prompt, 120)}",
    ]
    if schedule.last_run_at is not None:
        last = datetime.fromtimestamp(schedule.last_run_at / 1000, tz)
        lines.append(f"   last run {last:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


def _schedule_timezone(context: ContextTypes.DEFAULT_TYPE) -> ZoneInfo:
    """The configured schedule timezone; UTC when no config is wired (tests)."""
    config: Config | None = context.application.bot_data.get("config")
    tz = getattr(config, "timezone", None)
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo("UTC")


async def _handle_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/schedule`` — run a prompt on a timer (ADR-0016).

    Bare, it lists this chat's schedules. ``/schedule <when> <context> <prompt>``
    saves a new one; ``cancel`` / ``run`` / ``on`` / ``off`` manage the existing
    ones. See :data:`_SCHEDULE_USAGE` for the full surface and
    :func:`balam.schedules.parse_when` for the ``<when>`` grammar.
    """
    message = update.message
    if message is None:
        return
    args = context.args or []
    if not args:
        await _schedule_list(message, context)
        return

    head = args[0].strip().lower()
    if head == "cancel":
        await _schedule_cancel(message, context)
        return
    if head in ("run", "on", "off"):
        await _schedule_by_id(message, context, head, args[1:])
        return
    await _schedule_create(message, context, args)


async def _schedule_list(message: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: SessionStore = context.application.bot_data["store"]
    rows = store.list_schedules(message.chat_id)
    if not rows:
        await message.reply_text(f"No schedules yet.\n\n{_SCHEDULE_USAGE}")
        return
    tz = _schedule_timezone(context)
    body = "\n".join(_format_schedule(schedules.Schedule.from_row(row), tz) for row in rows)
    await message.reply_text(f"🗓 Schedules (times in {tz.key}):\n{body}\n\n{_SCHEDULE_USAGE}")


async def _schedule_create(
    message: Any, context: ContextTypes.DEFAULT_TYPE, args: list[str]
) -> None:
    router: Router = context.application.bot_data["router"]
    store: SessionStore = context.application.bot_data["store"]

    try:
        when = schedules.parse_when(args)
    except schedules.ScheduleError as exc:
        await message.reply_text(str(exc))
        return

    if len(args) < 3:
        await message.reply_text(f"I also need a context and a prompt.\n\n{_SCHEDULE_USAGE}")
        return
    name = router.contexts.match_name(args[2])
    if name is None:
        available = ", ".join(sorted(router.contexts.contexts))
        await message.reply_text(f"Unknown context {args[2]!r}. Available: {available}")
        return

    # The prompt comes from the raw text, not context.args: that is a whitespace
    # split and would collapse a multi-line prompt onto one line.
    prompt = _command_remainder(message.text or "", args_consumed=3)
    if not prompt:
        await message.reply_text(f"I need a prompt to run.\n\n{_SCHEDULE_USAGE}")
        return

    schedule_id = store.add_schedule(
        chat_id=message.chat_id,
        context=name,
        prompt=prompt,
        kind=when.kind,
        hour=when.hour,
        minute=when.minute,
        days=when.days_csv,
        created_at=int(time.time() * 1000),
    )
    tz = _schedule_timezone(context)
    job_queue = getattr(context.application, "job_queue", None)
    if job_queue is None:
        # Saved but inert. Say so rather than implying a timer exists — a silent
        # None here is exactly the failure ADR-0016 calls out.
        await message.reply_text(
            f"⚠️ Saved schedule #{schedule_id}, but the job queue is unavailable, so it "
            "will not fire until Balam restarts with python-telegram-bot[job-queue]."
        )
        return
    row = store.get_schedule(schedule_id)
    assert row is not None  # just inserted
    schedules.register_one(job_queue, schedules.Schedule.from_row(row), tz)
    await message.reply_text(
        f"🗓 Schedule #{schedule_id} saved — {describe(when)} ({tz.key}) in {name}.\n"
        f"{schedules.summarize(prompt, 120)}\n\n"
        f"Test it now with /schedule run {schedule_id}."
    )


async def _schedule_by_id(
    message: Any, context: ContextTypes.DEFAULT_TYPE, action: str, rest: list[str]
) -> None:
    """``/schedule run|on|off <id>``."""
    store: SessionStore = context.application.bot_data["store"]
    if not rest:
        await message.reply_text(f"Which schedule? /schedule {action} <id>")
        return
    try:
        schedule_id = int(rest[0].lstrip("#"))
    except ValueError:
        await message.reply_text(f"{rest[0]!r} isn't a schedule id. /schedule {action} <id>")
        return
    row = store.get_schedule(schedule_id)
    if row is None or row.chat_id != message.chat_id:
        await message.reply_text(f"No schedule #{schedule_id} here.")
        return
    schedule = schedules.Schedule.from_row(row)
    tz = _schedule_timezone(context)
    job_queue = getattr(context.application, "job_queue", None)

    if action == "run":
        # The whole point of this sub-command: a 24-hour feedback loop becomes a
        # 5-second one. It runs unattended, exactly as the timer would.
        await message.reply_text(f"▶️ Running schedule #{schedule_id} now…")
        await schedules.run_schedule(context, schedule)
        return

    enable = action == "on"
    if schedule.enabled == enable:
        await message.reply_text(f"Schedule #{schedule_id} is already {'on' if enable else 'off'}.")
        return
    store.set_schedule_enabled(schedule_id, enable)
    if job_queue is not None:
        if enable:
            schedules.register_one(job_queue, replace(schedule, enabled=True), tz)
        else:
            schedules.unregister(job_queue, schedule_id)
    await message.reply_text(
        f"{'▶️ Resumed' if enable else '⏸ Paused'} schedule #{schedule_id} — "
        f"{describe(schedule.when)}."
    )


async def _schedule_cancel(message: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/schedule cancel`` — the paged multi-select picker, mirroring /delete."""
    store: SessionStore = context.application.bot_data["store"]
    rows = store.list_schedules(message.chat_id)
    if not rows:
        await message.reply_text("No schedules to cancel.")
        return

    picks: PendingPicks = context.application.bot_data["pending_schedule_picks"]
    token = picks.register(
        message.chat_id,
        [(row.id, _schedule_label(schedules.Schedule.from_row(row))) for row in rows],
    )
    text = "🗑 Select schedules to cancel, then tap “Cancel selected”."
    info = picks.page_info(token)
    if info and info[1] > 1:
        text += (
            f"\n\n{info[2]} schedules across {info[1]} pages — use ◀ ▶ to browse. "
            "Selections persist across pages."
        )
    await message.reply_text(text, reply_markup=_picker_markup(_SCHEDULE_PICKER, picks, token))


async def _handle_schedule_toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Toggle a schedule's checkbox in the picker (``sch:<token>:<id>``)."""
    await _handle_picker_toggle(update, context, _SCHEDULE_PICKER, "pending_schedule_picks")


async def _handle_schedule_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Flip the /schedule cancel picker to another page (``schp:<token>:<page>``)."""
    await _handle_picker_page(update, context, _SCHEDULE_PICKER, "pending_schedule_picks")


async def _handle_schedule_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete the schedules selected in the picker (``schd:<token>``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("schd:"):
        return
    config: Config = context.application.bot_data["config"]
    if not _callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 1)
    if len(parts) != 2:
        await query.answer("Malformed request.")
        return
    token = parts[1]

    picks: PendingPicks = context.application.bot_data["pending_schedule_picks"]
    schedule_ids = picks.selected_ids(token)
    if schedule_ids is None:
        await query.answer("This picker has expired.")
        await _clear_keyboard(query)
        return
    if not schedule_ids:
        await query.answer("Select at least one schedule.")
        return
    picks.discard(token)

    store: SessionStore = context.application.bot_data["store"]
    job_queue = getattr(context.application, "job_queue", None)
    cancelled = 0
    for schedule_id in schedule_ids:
        # Drop the timer first: a row that survives a failed delete still has no
        # job, which is the safe direction (nothing fires unannounced).
        if job_queue is not None:
            schedules.unregister(job_queue, schedule_id)
        if store.delete_schedule(schedule_id):
            cancelled += 1

    await query.answer(f"Cancelled {cancelled} schedule(s).")
    message = getattr(query, "message", None)
    if message is not None:
        try:
            await message.edit_text(text=f"🗑 Cancelled {cancelled} schedule(s).", reply_markup=None)
        except Exception:
            logger.debug("failed to finalize schedule cancel message", exc_info=True)


async def _handle_schedule_dismiss_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Dismiss the /schedule cancel picker, cancelling nothing (``schx:<token>``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("schx:"):
        return
    config: Config = context.application.bot_data["config"]
    if not _callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 1)
    if len(parts) == 2:
        context.application.bot_data["pending_schedule_picks"].discard(parts[1])
    await query.answer("Dismissed.")
    message = getattr(query, "message", None)
    if message is not None:
        try:
            await message.edit_text(text="🗑 Nothing cancelled.", reply_markup=None)
        except Exception:
            logger.debug("failed to finalize schedule dismiss message", exc_info=True)


async def _handle_topic_edited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sync the stored title when a topic is renamed from the Telegram UI.

    Every other title change is set by the bot itself; this service-message update
    is the one path it doesn't originate, so the /delete picker would otherwise show
    a stale name."""
    message = update.message
    if message is None or message.message_thread_id is None:
        return
    edited = message.forum_topic_edited
    if edited is None or not edited.name:
        return
    router: Router = context.application.bot_data["router"]
    router.set_topic_title(message.chat_id, message.message_thread_id, edited.name)


async def _show_custom_answer_on_question(bot: Any, chat_id: int, outcome: CustomAnswer) -> None:
    """Show a typed custom answer on its original question message, replacing the
    "Reply with your answer" note. Best-effort: needs the recorded message id and
    prompt text, and a failed edit (message too old) is logged, not raised — the
    ``✅ Answer sent.`` reply already confirms the answer landed."""
    if outcome.message_id is None or outcome.text is None:
        return
    answer = escape_markdown_v2(outcome.answer)
    text = f"{outcome.text}\n\n✅ *Answered:* {answer}"
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=outcome.message_id,
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
    except Exception:
        logger.debug("failed to show custom answer on question message", exc_info=True)


def _answered_note(answers: list[str]) -> str:
    """A MarkdownV2 outcome line naming what was chosen, for a resolved question
    keyboard. Falls back to a bare confirmation when the answers are unknown."""
    if not answers:
        return r"✅ Answered\."
    joined = ", ".join(answers)
    return f"✅ *Answered:* {escape_markdown_v2(joined)}"


async def _clear_keyboard(query: Any, note: str | None = None) -> bool:
    """Strip a spent approval keyboard, appending a one-line outcome when given.

    ``note`` (when set) must already be MarkdownV2-escaped. Best-effort: a failed
    edit — e.g. a message too old to edit — is logged, not raised; the callback
    answer already told the user the outcome.
    """
    message = getattr(query, "message", None)
    if message is None:
        return False
    original = message.text_markdown_v2 or message.text or ""
    text = f"{original}\n\n{note}" if note else original
    try:
        await message.edit_text(text=text, parse_mode="MarkdownV2", reply_markup=None)
        return True
    except Exception:
        logger.debug("failed to update spent approval message", exc_info=True)
        return False


async def _refresh_question_keyboard(
    query: Any, pending_questions: PendingQuestions, token: str, question_index: int
) -> bool:
    message = getattr(query, "message", None)
    if message is None:
        return False
    labels = pending_questions.labels(token, question_index)
    selected = pending_questions.selected_indexes(token, question_index)
    if labels is None or selected is None:
        return False
    options = [{"label": label} for label in labels]
    text = message.text_markdown_v2 or message.text or ""
    try:
        await message.edit_text(
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=_question_keyboard(
                token,
                question_index,
                options,
                custom=pending_questions.allows_custom(token, question_index),
                multiple=True,
                selected_indexes=selected,
            ),
        )
        return True
    except Exception:
        logger.debug("failed to refresh question keyboard", exc_info=True)
        return False


def _schedule_approval_cleanup(context: ContextTypes.DEFAULT_TYPE, message: Any) -> None:
    """Delete approved approval prompts after Telegram has shown the edit."""
    bot_data = context.application.bot_data
    delay_s = bot_data.get("approval_delete_delay_s", APPROVAL_DELETE_DELAY_S)
    task = asyncio.create_task(_delete_message_after_delay(message, delay_s))
    background_tasks = bot_data.setdefault("background_tasks", set())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def _delete_message_after_delay(message: Any, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    try:
        await message.delete()
    except Exception:
        logger.debug("failed to delete approved approval message", exc_info=True)


#: The slash commands Balam exposes. Registering them via ``setMyCommands`` is
#: what makes them discoverable and reliably routed to the bot in a group, where
#: clients dispatch slash commands by the bot's registered list.
BOT_COMMANDS = [
    BotCommand("new", "Open a new topic: /new [context] [first message]"),
    BotCommand("rename", "Rename the current topic"),
    BotCommand("status", "Show this topic's context, session, and turn state"),
    BotCommand("model", "Show or set this topic's model override"),
    BotCommand("effort", "Show or set this topic's effort override"),
    BotCommand("cancel", "Abort the turn currently running in this topic"),
    BotCommand("context", "List contexts, or /context <name> [first message] to open a topic"),
    BotCommand("diff", "Open the Mini App git diff viewer for this topic's context"),
    BotCommand("browser", "Watch the agent's live browser (Mini App)"),
    BotCommand("artifacts", "List your published claude.ai artifacts (/artifacts [shared|all])"),
    BotCommand("delete", "Delete topics — pick which ones to remove"),
    BotCommand("schedule", "Run a prompt on a schedule: /schedule daily 07:30 <context> <prompt>"),
]


async def register_commands(bot: Bot, chat_id: int | None = None) -> None:
    """Publish :data:`BOT_COMMANDS` so clients surface and route ``/context``.

    In groups a client routes a slash command by the bot's registered command
    list (and may send it as ``/context@<bot>``); without ``setMyCommands`` the
    command is never offered and bare ``/context`` may not be delivered. We set
    the default and all-group-chats scopes, plus the specific group chat when
    Balam is scoped to one, so the command appears exactly where it is used.
    """
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())
    if chat_id is not None:
        from telegram import BotCommandScopeChat

        await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeChat(chat_id=chat_id))


def build_application(
    config: Config,
    backend: AgentBackend,
    router: Router,
    *,
    store: SessionStore | None = None,
    post_init: Any = None,
    post_shutdown: Any = None,
) -> Application:
    # Live-edit streaming, the todo checklist, and command handlers all share
    # one per-group flood budget (~20 messages/min). The limiter paces calls
    # under Telegram's limits and sleeps+retries on RetryAfter, so a burst
    # can't 429 a turn's final answer into the void.
    builder = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=3))
    )
    if post_init is not None:
        builder = builder.post_init(post_init)
    if post_shutdown is not None:
        builder = builder.post_shutdown(post_shutdown)
    app = builder.build()

    app.bot_data["config"] = config
    app.bot_data["backend"] = backend
    app.bot_data["router"] = router
    # The SQLite store, for the one caller that isn't topic→session routing:
    # /schedule reads and writes the schedules table directly (ADR-0016).
    app.bot_data["store"] = store
    # In-flight turns, keyed by topic, so /cancel can interrupt a running reply.
    app.bot_data["turns"] = TurnRegistry()
    # Outstanding tool-approval prompts + per-session "accept all edits" state.
    app.bot_data["pending"] = PendingApprovals()
    # Outstanding OpenCode question-tool prompts.
    app.bot_data["pending_questions"] = PendingQuestions()
    # Outstanding picker selections: /delete over topics, /schedule cancel over
    # schedules. Same class, one snapshot each (see PendingPicks).
    app.bot_data["pending_deletions"] = PendingPicks()
    app.bot_data["pending_schedule_picks"] = PendingPicks()
    # Anchors fire-and-forget background tasks (e.g. /cancel's server-side abort)
    # so the loop's weak task references can't let them be GC'd mid-flight.
    app.bot_data["background_tasks"] = set()

    # Trust boundary (ADR-0008): filters.User gates by sender id, so only the
    # owner's messages reach the handlers; everyone else is dropped silently.
    # When a target chat is configured (ADR-0010), additionally require that
    # chat, so the bot acts only inside the workspace supergroup. Unset → the
    # legacy owner-anywhere behavior, preserving the DM round-trip.
    allowed = filters.User(user_id=config.allowed_telegram_user_id)
    if config.allowed_telegram_chat_id is not None:
        allowed = allowed & filters.Chat(chat_id=config.allowed_telegram_chat_id)

    app.add_handler(CommandHandler("new", _handle_new, filters=allowed))
    app.add_handler(CommandHandler("rename", _handle_rename, filters=allowed))
    app.add_handler(CommandHandler("status", _handle_status, filters=allowed))
    app.add_handler(CommandHandler("model", _handle_model, filters=allowed))
    app.add_handler(CommandHandler("effort", _handle_effort, filters=allowed))
    app.add_handler(CommandHandler("cancel", _handle_cancel, filters=allowed))
    app.add_handler(CommandHandler("context", _handle_context, filters=allowed))
    app.add_handler(CommandHandler("diff", _handle_diff, filters=allowed))
    app.add_handler(CommandHandler("browser", _handle_browser, filters=allowed))
    app.add_handler(CommandHandler("artifacts", _handle_artifacts, filters=allowed))
    app.add_handler(CommandHandler("delete", _handle_delete, filters=allowed))
    app.add_handler(CommandHandler("schedule", _handle_schedule, filters=allowed))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND & allowed,
            _handle_message,
        )
    )
    # Catch-all for *unregistered* slash commands. The specific CommandHandlers above
    # match first (PTB stops at the first handler in a group), so only commands Balam
    # doesn't own — e.g. a Claude slash command like ``/goal`` — fall through here and
    # get forwarded verbatim to the agent instead of being silently dropped.
    app.add_handler(MessageHandler(filters.COMMAND & allowed, _handle_message))
    # Keep the stored title in step with renames done from the Telegram UI (the one
    # title change the bot doesn't itself originate). A service message matches
    # neither commands nor the text/photo handler above, so it falls through here.
    app.add_handler(
        MessageHandler(filters.StatusUpdate.FORUM_TOPIC_EDITED & allowed, _handle_topic_edited)
    )
    # CallbackQueryHandler takes no filter; the handler re-checks the trust
    # boundary (ADR-0008) itself before resolving an approval.
    app.add_handler(CallbackQueryHandler(_handle_approval_callback, pattern=r"^appr:"))
    app.add_handler(CallbackQueryHandler(_handle_question_done_callback, pattern=r"^qstd:"))
    app.add_handler(CallbackQueryHandler(_handle_question_callback, pattern=r"^qst:"))
    app.add_handler(CallbackQueryHandler(_handle_question_custom_callback, pattern=r"^qstc:"))
    app.add_handler(CallbackQueryHandler(_handle_delete_confirm_callback, pattern=r"^deld:"))
    app.add_handler(CallbackQueryHandler(_handle_delete_cancel_callback, pattern=r"^delx:"))
    app.add_handler(CallbackQueryHandler(_handle_delete_page_callback, pattern=r"^delp:"))
    app.add_handler(CallbackQueryHandler(_handle_delete_toggle_callback, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(_handle_schedule_confirm_callback, pattern=r"^schd:"))
    app.add_handler(CallbackQueryHandler(_handle_schedule_dismiss_callback, pattern=r"^schx:"))
    app.add_handler(CallbackQueryHandler(_handle_schedule_page_callback, pattern=r"^schp:"))
    app.add_handler(CallbackQueryHandler(_handle_schedule_toggle_callback, pattern=r"^sch:"))

    return app
