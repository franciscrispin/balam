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
from typing import Any

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
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from balam.agent.backend import AgentBackend
from balam.approvals import Choice, PendingApprovals, PendingDeletions, PendingQuestions
from balam.attachments import PromptFile, collect_attachments
from balam.config import Config
from balam.contexts import EFFORT_LEVELS, split_provider_model
from balam.miniapp import make_plan_view_button, mini_app_reply
from balam.router import Router, TopicRef
from balam.streamer import _question_keyboard, stream_reply
from balam.telegram_utils import thread_kwargs
from balam.turns import TurnJob, TurnRegistry

logger = logging.getLogger(__name__)

APPROVAL_DELETE_DELAY_S = 2.0


def is_owner(from_id: int | None, allowed_user_id: int) -> bool:
    """The allowlist check, isolated for testing (ADR-0008)."""
    return from_id is not None and from_id == allowed_user_id


def _topic_title(message: Any, thread_id: int | None) -> str:
    """Best-effort human label for a freshly created session."""
    reply_to = getattr(message, "reply_to_message", None)
    created = getattr(reply_to, "forum_topic_created", None)
    name = getattr(created, "name", None)
    if name:
        return name
    if thread_id is None:
        return "General"
    return f"Topic {thread_id}"


def _is_forum_general_message(message: Any) -> bool:
    """True for the General channel of a forum supergroup, not plain DMs."""
    chat = getattr(message, "chat", None)
    return message.message_thread_id is None and bool(getattr(chat, "is_forum", False))


def _topic_name(context_name: str, first_message: str, *, has_files: bool = False) -> str:
    """Build a Bot-API-safe topic name: ``context: truncated first message``."""
    summary = " ".join(first_message.split())
    if not summary:
        summary = "attachment" if has_files else "message"

    prefix = f"{context_name}: "
    max_len = 128
    available = max_len - len(prefix)
    if available < 4:
        return f"{prefix}{summary}"[: max_len - 3] + "..."
    if len(summary) > available:
        summary = summary[: available - 3].rstrip() + "..."
    return f"{prefix}{summary}"


async def _notify_error(bot: Any, chat_id: int, thread_id: int | None, exc: Exception) -> None:
    """Post a short error notice into the topic (ADR-0009 edge), swallowing any
    delivery failure so it never masks the original error."""
    try:
        await bot.send_message(chat_id=chat_id, text=f"⚠️ {exc}", **thread_kwargs(thread_id))
    except Exception:
        logger.debug("failed to deliver error notice", exc_info=True)


async def _rename_forum_topic(bot: Any, chat_id: int, thread_id: int, name: str) -> None:
    """Rename a normal forum topic."""
    await bot.edit_forum_topic(chat_id=chat_id, message_thread_id=thread_id, name=name)


async def _auto_name_topic(
    bot: Any,
    router: Router,
    ref: TopicRef,
    context_name: str,
    first_message: str,
    *,
    has_files: bool = False,
) -> None:
    if ref.thread_id is None or router.topic_auto_named(ref):
        return
    name = _topic_name(context_name, first_message, has_files=has_files)
    try:
        await _rename_forum_topic(bot, ref.chat_id, ref.thread_id, name)
    except Exception:
        logger.debug("failed to auto-name topic", exc_info=True)
        return
    router.mark_topic_auto_named(ref)
    router.set_topic_title(ref.chat_id, ref.thread_id, name)


async def _create_topic_from_general(
    message: Any,
    bot: Any,
    router: Router,
    text: str,
    *,
    has_files: bool = False,
) -> int | None:
    """Let a message in General open a new topic in General's current context."""
    general_ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=None,
        title=_topic_title(message, None),
    )
    context_name = router.current_context_name(general_ref)
    name = _topic_name(context_name, text, has_files=has_files)
    try:
        topic = await bot.create_forum_topic(chat_id=message.chat_id, name=name)
    except Exception as exc:
        logger.exception("failed to create topic from General message")
        await message.reply_text(
            f"⚠️ Couldn't create a topic for this message: {exc}\n"
            "This chat must be a forum supergroup and the bot an admin with "
            "the 'Manage Topics' permission."
        )
        return None

    thread_id = topic.message_thread_id
    try:
        await router.create_topic_session(
            message.chat_id,
            thread_id,
            name,
            context_name,
            auto_named=True,
        )
    except Exception as exc:
        logger.exception("failed to start session for General-created topic")
        try:
            await bot.delete_forum_topic(chat_id=message.chat_id, message_thread_id=thread_id)
        except Exception:
            logger.debug("failed to delete orphan topic after session failure", exc_info=True)
        await message.reply_text(f"⚠️ Couldn't start a session for {context_name!r}: {exc}")
        return None

    link = _topic_link(message.chat_id, thread_id, bot_id=getattr(bot, "id", None))
    if link:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Go to topic", url=link)]])
        await message.reply_text(f"Opened {name}.", reply_markup=keyboard)
    else:
        await message.reply_text(f"Opened {name} — pick it from the topic list.")
    return thread_id


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    chat_id = message.chat_id
    thread_id = message.message_thread_id
    text = message.text or message.caption or ""

    pending_questions: PendingQuestions | None = context.application.bot_data.get(
        "pending_questions"
    )
    if text and pending_questions is not None:
        custom_result = pending_questions.resolve_custom(chat_id, thread_id, text)
        if custom_result == "resolved":
            await message.reply_text("✅ Answer sent.")
            return
        if custom_result == "added":
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

    if _is_forum_general_message(message):
        created_thread_id = await _create_topic_from_general(
            message,
            context.bot,
            router,
            text,
            has_files=bool(files),
        )
        if created_thread_id is None:
            return
        thread_id = created_thread_id

    await _submit_turn(message, context, text, files, thread_id=thread_id)


async def _submit_turn(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    files: list[PromptFile],
    *,
    thread_id: int | None,
    queued_reply: str = "⏳ Queued (#{position}) — I'll run this after the current turn finishes.",
) -> None:
    """Resolve the topic's session and run ``text`` as its turn, or park it in the
    topic's queue when a turn is already streaming.

    Shared dispatch tail of the message and ``/plan`` paths. ``thread_id`` is
    explicit because a General message has already been rehomed into a freshly
    created topic by the time it gets here; ``queued_reply`` is formatted with
    the job's 1-based queue ``position``.
    """
    router: Router = context.application.bot_data["router"]
    turns: TurnRegistry = context.application.bot_data["turns"]
    chat_id = message.chat_id

    try:
        ref = TopicRef(
            chat_id=chat_id,
            thread_id=thread_id,
            title=_topic_title(message, thread_id),
        )
        resolved = await router.resolve(ref)
        await _auto_name_topic(
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
        return

    job = TurnJob(
        prompt=text,
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
    )

    # One turn per topic at a time (ADR-0009). OpenCode runs a single turn per
    # session, so a message that lands while a turn is still streaming must not
    # fire a second prompt — that collides and the message is silently dropped.
    # Queue it instead; the running turn drains it when it finishes. The check and
    # the enqueue run with no ``await`` between them, so the running turn's drain
    # can't race in and miss this message.
    if turns.get(chat_id, thread_id) is not None:
        position = turns.enqueue(chat_id, thread_id, job)
        await message.reply_text(queued_reply.format(position=position))
        return

    _start_turn(context, chat_id, thread_id, job)


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

    # Snapshot-and-button factory for plan_exit questions ("View plan" in the
    # Mini App). Only wired when app.py stashed a content store (unit tests of
    # the bot path don't, and the streamer treats None as "no button").
    plan_view = None
    content_store = context.application.bot_data.get("content_store")
    if content_store is not None:
        config: Config = context.application.bot_data["config"]
        plan_view = make_plan_view_button(
            config, content_store, getattr(context.bot, "username", None)
        )

    def _on_plan_approved() -> None:
        # The plan_exit question was answered "Yes": the server switches the
        # session to the build agent, so the sticky flag must drop with it.
        router.set_plan_mode(chat_id, thread_id, False)

    async def run() -> None:
        cancelled = False
        try:
            # Sticky plan mode (/plan): the backend maps it to the plan agent /
            # plan permission mode per turn. Read at turn start — not at enqueue —
            # so a job that waited in the queue respects a plan approval or
            # /plan off issued in the meantime.
            plan_mode = router.plan_mode(chat_id, thread_id)
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
                plan_mode=plan_mode,
                plan_view=plan_view,
                on_plan_approved=_on_plan_approved,
                on_session_started=lambda sid: router.persist_session(chat_id, thread_id, sid),
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
    turns.register(chat_id, thread_id, task, job.session_id, job.directory)


def _topic_link(chat_id: int, thread_id: int, bot_id: int | None = None) -> str | None:
    """A one-tap link to a forum topic, or ``None`` if not derivable.

    Telegram has **no documented** deep link to a topic in a *private* chat:
    thread-targeting ``t.me``/``tg://`` links are supergroup/channel only, and
    topics-in-private-chats (Bot API 9.3, Dec 2025; ``createForumTopic`` in
    private chats, 9.4, Feb 2026) shipped without a navigation scheme. So:

    - **Supergroup** (`-100<internal>` chat id): the official private-supergroup
      link ``t.me/c/<internal>/<thread>`` — works in every client.
    - **Private chat with topics** (positive chat id): fall back to the Telegram
      **Web** address ``web.telegram.org/a/#<bot_id>_<thread>`` — exactly how the
      Web client routes to the topic (verified to open it cold). Web-only (native
      apps have no private-chat topic link), but the owner drives Balam over Web,
      so it is a real one-tap link. The chat's peer in the owner's client is the
      bot, hence ``bot_id``.
    """
    text = str(chat_id)
    if text.startswith("-100"):
        return f"https://t.me/c/{text[4:]}/{thread_id}"
    if bot_id is not None:
        return f"https://web.telegram.org/a/#{bot_id}_{thread_id}"
    return None


async def _open_context_topic(message: Any, bot: Any, router: Router, name: str) -> None:
    """Create a fresh forum topic bound to context ``name``, start its session,
    greet inside it, and reply with a one-tap link. Shared by ``/context <name>``
    and ``/new``.

    One context per topic for life — we never rebind an existing topic, so a
    topic's session always remembers its own history. The Bot API can't move the
    user's view, so we create the topic, greet it, and hand back a deep link to
    tap. Requires a forum supergroup with the bot an admin holding "Manage
    Topics"; duplicate topic names are fine (many topics may share one context).
    """
    ctx = router.contexts.contexts[name]
    try:
        topic = await bot.create_forum_topic(chat_id=message.chat_id, name=name)
    except Exception as exc:
        logger.exception("failed to create forum topic")
        await message.reply_text(
            f"⚠️ Couldn't create a topic for {name!r}: {exc}\n"
            "This chat must be a forum supergroup and the bot an admin with "
            "the 'Manage Topics' permission."
        )
        return

    new_thread_id = topic.message_thread_id
    try:
        await router.create_topic_session(message.chat_id, new_thread_id, name, name)
    except Exception as exc:
        logger.exception("failed to start session for new topic")
        # Roll back the just-created topic: an unbound topic would silently route
        # to default_context, not the one we meant. Best-effort delete.
        try:
            await bot.delete_forum_topic(chat_id=message.chat_id, message_thread_id=new_thread_id)
        except Exception:
            logger.debug("failed to delete orphan topic after session failure", exc_info=True)
        await message.reply_text(f"⚠️ Couldn't start a session for {name!r}: {exc}")
        return

    # Greet inside the new topic so it isn't empty, then hand back a one-tap link
    # in the originating chat/topic as an inline URL button.
    await bot.send_message(
        chat_id=message.chat_id,
        text=f"🗂 Context {name} — {ctx.directory}\nSend a message to start.",
        message_thread_id=new_thread_id,
    )
    link = _topic_link(message.chat_id, new_thread_id, bot_id=bot.id)
    if link:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Go to topic", url=link)]])
        await message.reply_text(f"Opened a new {name} topic.", reply_markup=keyboard)
    else:
        await message.reply_text(f"Opened a new {name} topic — pick it from the topic list.")


async def _handle_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/context`` lists workspaces and the topic's current binding;
    ``/context <name>`` creates a *new* topic bound to that context and replies
    with a one-tap link to it (see :func:`_open_context_topic`)."""
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=_topic_title(message, message.message_thread_id),
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

    await _open_context_topic(message, context.bot, router, name)


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
    """``/new [name]`` — open a fresh topic with a new session.

    The same flow as ``/context <name>`` (:func:`_open_context_topic`). With an
    argument, the new topic is bound to context ``name``; without one, it reuses
    the current topic's binding (``default_context`` when unbound). The current
    topic is left untouched — one context per topic for life — so its history is
    preserved and the fresh start lives in its own topic.
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
    else:
        ref = TopicRef(
            chat_id=message.chat_id,
            thread_id=message.message_thread_id,
            title=_topic_title(message, message.message_thread_id),
        )
        name = router.current_context_name(ref)
    await _open_context_topic(message, context.bot, router, name)


async def _handle_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/plan [request]`` — put this topic in plan mode; ``/plan off`` leaves it.

    OpenCode's plan agent is selected per *prompt*, not per session, so plan mode
    is a sticky per-topic flag: while set, every prompt is sent with
    ``agent="plan"`` (the plan agent can read everything but only write its plan
    file). The flag drops when the plan_exit question is answered "Yes" (the
    server then switches the session to the build agent itself) or on
    ``/plan off``. With a ``request`` argument the planning prompt runs
    immediately; bare ``/plan`` arms the mode for the next message.
    """
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    chat_id = message.chat_id
    thread_id = message.message_thread_id
    args = context.args or []

    if args and args[0].lower() == "off":
        router.set_plan_mode(chat_id, thread_id, False)
        await message.reply_text("Plan mode off — messages run the build agent again.")
        return

    if _is_forum_general_message(message):
        # Plain General messages spawn fresh topics that would not inherit the
        # flag, so a sticky General flag would silently do nothing.
        await message.reply_text("Use /plan inside a topic (General messages open new topics).")
        return

    router.set_plan_mode(chat_id, thread_id, True)
    request = " ".join(args).strip()
    if not request:
        await message.reply_text(
            "📋 Plan mode on — messages here run the plan agent (read-only except its "
            "plan file) until you approve the plan or send /plan off."
        )
        return

    # Run the planning request right away through the shared dispatch tail (the
    # flag is already set, so the turn derives agent="plan" when it starts).
    await _submit_turn(
        message,
        context,
        request,
        [],
        thread_id=thread_id,
        queued_reply=(
            "📋 Plan mode on. ⏳ Queued (#{position}) — I'll plan after the current turn finishes."
        ),
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
        title=_topic_title(message, message.message_thread_id),
    )

    name = router.current_context_name(ref)
    ctx = router.contexts.get(name)
    provider, model = ctx.provider_model
    override_provider, override_model = router.model_override(ref.chat_id, ref.thread_id)
    override_effort = router.effort_override(ref.chat_id, ref.thread_id)
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
        f"Plan mode: {'on' if router.plan_mode(ref.chat_id, ref.thread_id) else 'off'}",
    ]
    await message.reply_text("\n".join(lines))


def _format_model(provider: str | None, model: str | None) -> str:
    if provider and model:
        return f"{provider}/{model}"
    return "(server default)"


async def _handle_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/model [provider/model|reset]`` — inspect or override this topic's model."""
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=_topic_title(message, message.message_thread_id),
    )
    args = context.args or []

    if not args:
        name = router.current_context_name(ref)
        provider, model = router.contexts.get(name).provider_model
        override_provider, override_model = router.model_override(ref.chat_id, ref.thread_id)
        source = (
            "topic override" if override_model else "context default" if model else "server default"
        )
        await message.reply_text(
            f"Model: {_format_model(override_provider or provider, override_model or model)}\n"
            f"Source: {source}\n"
            "Set with /model <provider/model>, reset with /model reset."
        )
        return

    value = args[0].strip()
    if value.lower() == "reset":
        router.reset_model_override(ref.chat_id, ref.thread_id)
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

    router.set_model_override(ref.chat_id, ref.thread_id, provider, model)
    await message.reply_text(f"Model override set to {provider}/{model}.")


async def _handle_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/effort [level|reset]`` — inspect or override this topic's effort."""
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=_topic_title(message, message.message_thread_id),
    )
    args = context.args or []

    if not args:
        name = router.current_context_name(ref)
        ctx = router.contexts.get(name)
        override = router.effort_override(ref.chat_id, ref.thread_id)
        source = (
            "topic override" if override else "context default" if ctx.effort else "server default"
        )
        await message.reply_text(
            f"Effort: {override or ctx.effort or '(server default)'}\n"
            f"Source: {source}\n"
            "Set with /effort <level>, reset with /effort reset."
        )
        return

    value = args[0].strip().lower()
    if value == "reset":
        router.reset_effort_override(ref.chat_id, ref.thread_id)
        name = router.current_context_name(ref)
        ctx = router.contexts.get(name)
        await message.reply_text(f"Effort reset to {ctx.effort or '(server default)'}.")
        return

    if value not in EFFORT_LEVELS:
        allowed = ", ".join(sorted(EFFORT_LEVELS))
        await message.reply_text(f"Unknown effort {value!r}. Available: {allowed}")
        return

    router.set_effort_override(ref.chat_id, ref.thread_id, value)
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
        await _rename_forum_topic(context.bot, message.chat_id, message.message_thread_id, new_name)
    except Exception as exc:
        logger.exception("failed to rename forum topic")
        await message.reply_text(f"⚠️ Couldn't rename this topic: {exc}")
        return

    router: Router = context.application.bot_data["router"]
    router.mark_topic_auto_named(
        TopicRef(
            chat_id=message.chat_id,
            thread_id=message.message_thread_id,
            title=_topic_title(message, message.message_thread_id),
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
        title=_topic_title(message, message.message_thread_id),
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

    if not pending_questions.resolve(token, q_index, o_index):
        await query.answer("This question has expired.")
        await _clear_keyboard(query)
        return
    await query.answer("Answered.")
    await _clear_keyboard(query, note=r"✅ Answered\.")


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
    finished = pending_questions.finish_multi(token, q_index)
    if finished is False:
        await query.answer("Select at least one option.")
        return
    if finished is None:
        await query.answer("This question has expired.")
        await _clear_keyboard(query)
        return
    await query.answer("Answered.")
    await _clear_keyboard(query, note=r"✅ Answered\.")


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


def _topic_label(title: str | None, context_name: str | None, thread_id: int) -> str:
    """Button label for a topic: its title, else the bound context + thread id
    (topics created before titles were tracked have no stored title)."""
    base = title or (f"{context_name} · #{thread_id}" if context_name else f"#{thread_id}")
    return base if len(base) <= 48 else base[:47] + "…"


def _delete_keyboard(
    token: str,
    entries: list[tuple[int, str, bool]],
    page: int = 0,
    page_count: int = 1,
    selected_count: int = 0,
) -> InlineKeyboardMarkup:
    """Checklist for the current page of topics (``del:<token>:<thread_id>``), a
    Prev/Next navigation row when the snapshot spans more than one page
    (``delp:<token>:<page>``), and the confirm/cancel row. ``selected_count`` spans
    the whole snapshot, so the confirm button reflects picks made on other pages."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{'☑️' if selected else '☐'} {label}",
                callback_data=f"del:{token}:{thread_id}",
            )
        ]
        for thread_id, label, selected in entries
    ]
    if page_count > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"delp:{token}:{page - 1}"))
        # The indicator points at the current page, so tapping it is a harmless no-op.
        nav.append(
            InlineKeyboardButton(
                f"Page {page + 1}/{page_count}", callback_data=f"delp:{token}:{page}"
            )
        )
        if page < page_count - 1:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"delp:{token}:{page + 1}"))
        rows.append(nav)
    confirm_label = "🗑 Delete selected"
    if selected_count:
        confirm_label += f" ({selected_count})"
    rows.append(
        [
            InlineKeyboardButton(confirm_label, callback_data=f"deld:{token}"),
            InlineKeyboardButton("Cancel", callback_data=f"delx:{token}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _delete_markup(
    pending_deletions: PendingDeletions, token: str
) -> InlineKeyboardMarkup | None:
    """Build the picker keyboard from current snapshot state, or ``None`` if the
    token expired."""
    entries = pending_deletions.entries(token)
    info = pending_deletions.page_info(token)
    if entries is None or info is None:
        return None
    page, page_count, _total, selected = info
    return _delete_keyboard(token, entries, page, page_count, selected)


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

    pending_deletions: PendingDeletions = context.application.bot_data["pending_deletions"]
    token = pending_deletions.register(
        message.chat_id,
        [(thread_id, _topic_label(title, ctx, thread_id)) for thread_id, title, ctx in topics],
    )
    text = "🗑 Select topics to delete, then tap “Delete selected”."
    info = pending_deletions.page_info(token)
    if info and info[1] > 1:
        text += (
            f"\n\n{info[2]} topics across {info[1]} pages — use ◀ ▶ to browse. "
            "Selections persist across pages."
        )
    await message.reply_text(text, reply_markup=_delete_markup(pending_deletions, token))


async def _handle_delete_toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Toggle a topic's checkbox in the /delete picker (``del:<token>:<thread_id>``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("del:"):
        return
    config: Config = context.application.bot_data["config"]
    if not _callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("Malformed selection.")
        return
    _, token, thread_id_raw = parts
    try:
        thread_id = int(thread_id_raw)
    except ValueError:
        await query.answer("Malformed selection.")
        return

    pending_deletions: PendingDeletions = context.application.bot_data["pending_deletions"]
    state = pending_deletions.toggle(token, thread_id)
    if state is None:
        await query.answer("This picker has expired.")
        await _clear_keyboard(query)
        return
    await query.answer("Selected." if state else "Unselected.")
    markup = _delete_markup(pending_deletions, token)
    message = getattr(query, "message", None)
    if markup is None or message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except Exception:
        logger.debug("failed to refresh delete keyboard", exc_info=True)


async def _handle_delete_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip the /delete picker to another page (``delp:<token>:<page>``). Selections
    are kept in the snapshot, so paging never loses what's already checked."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("delp:"):
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

    pending_deletions: PendingDeletions = context.application.bot_data["pending_deletions"]
    if pending_deletions.set_page(token, page) is None:
        await query.answer("This picker has expired.")
        await _clear_keyboard(query)
        return
    await query.answer()
    markup = _delete_markup(pending_deletions, token)
    message = getattr(query, "message", None)
    if markup is None or message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except Exception:
        logger.debug("failed to page delete keyboard", exc_info=True)


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

    pending_deletions: PendingDeletions = context.application.bot_data["pending_deletions"]
    thread_ids = pending_deletions.selected_thread_ids(token)
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
    BotCommand("new", "Open a new topic (this context, or /new <name> for another)"),
    BotCommand("rename", "Rename the current topic"),
    BotCommand("status", "Show this topic's context, session, and turn state"),
    BotCommand("model", "Show or set this topic's model override"),
    BotCommand("effort", "Show or set this topic's effort override"),
    BotCommand("cancel", "Abort the turn currently running in this topic"),
    BotCommand("context", "List workspace contexts, or open a new topic bound to one"),
    BotCommand("plan", "Plan mode for this topic (/plan [request], /plan off)"),
    BotCommand("diff", "Open the Mini App git diff viewer for this topic's context"),
    BotCommand("browser", "Watch the agent's live browser (Mini App)"),
    BotCommand("delete", "Delete topics — pick which ones to remove"),
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
    post_init: Any = None,
    post_shutdown: Any = None,
) -> Application:
    builder = ApplicationBuilder().token(config.telegram_bot_token)
    if post_init is not None:
        builder = builder.post_init(post_init)
    if post_shutdown is not None:
        builder = builder.post_shutdown(post_shutdown)
    app = builder.build()

    app.bot_data["config"] = config
    app.bot_data["backend"] = backend
    app.bot_data["router"] = router
    # In-flight turns, keyed by topic, so /cancel can interrupt a running reply.
    app.bot_data["turns"] = TurnRegistry()
    # Outstanding tool-approval prompts + per-session "accept all edits" state.
    app.bot_data["pending"] = PendingApprovals()
    # Outstanding OpenCode question-tool prompts.
    app.bot_data["pending_questions"] = PendingQuestions()
    # Outstanding /delete topic-picker selections.
    app.bot_data["pending_deletions"] = PendingDeletions()
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
    app.add_handler(CommandHandler("plan", _handle_plan, filters=allowed))
    app.add_handler(CommandHandler("diff", _handle_diff, filters=allowed))
    app.add_handler(CommandHandler("browser", _handle_browser, filters=allowed))
    app.add_handler(CommandHandler("delete", _handle_delete, filters=allowed))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND & allowed,
            _handle_message,
        )
    )
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

    return app
