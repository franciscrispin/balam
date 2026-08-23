"""The Telegram application: what Balam listens to, and who may speak to it.

Two things live here, and only these two:

  1. **The plain-message path.** A message that is not one of Balam's own
     commands maps to its topic's session (ADR-0009), picks up any image or
     document attachments, and becomes a turn. This is the round-trip the whole
     system exists for, so it stays in the entry point rather than hiding in a
     command module. An album is several messages that have to become *one*
     turn, so it is buffered first (:mod:`balam.media_groups`).
  2. **The registrar.** :func:`build_application` builds the PTB application,
     puts the shared state every handler reads into ``bot_data``, applies the
     trust boundary (ADR-0008) as a handler filter, and wires each handler to
     its command or callback pattern.

Everything a handler *does* lives elsewhere — commands in :mod:`balam.commands`,
inline-keyboard replies in :mod:`balam.callbacks`, running a turn in
:mod:`balam.turns`, topics in :mod:`balam.topics`. The dependency arrow points
one way: this module imports them, none of them imports this one.

The allowlist itself is in :mod:`balam.auth`, because callback queries carry no
handler filter and have to re-check it themselves.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    Message,
    Update,
)
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

from balam.agent.backend import AgentBackend
from balam.approvals import (
    PendingApprovals,
    PendingPicks,
    PendingQuestions,
)
from balam.attachments import PromptFile, collect_attachments
from balam.callbacks import (
    handle_approval_callback,
    handle_question_callback,
    handle_question_custom_callback,
    handle_question_done_callback,
    show_custom_answer_on_question,
)
from balam.commands.delete import (
    handle_delete,
    handle_delete_cancel_callback,
    handle_delete_confirm_callback,
    handle_delete_page_callback,
    handle_delete_toggle_callback,
)
from balam.commands.schedule import (
    handle_schedule,
    handle_schedule_confirm_callback,
    handle_schedule_dismiss_callback,
    handle_schedule_page_callback,
    handle_schedule_toggle_callback,
)
from balam.commands.session import (
    handle_cancel,
    handle_context,
    handle_effort,
    handle_model,
    handle_new,
    handle_rename,
    handle_status,
)
from balam.commands.tasks import handle_tasks
from balam.commands.views import handle_artifacts, handle_browser, handle_diff
from balam.config import Config
from balam.media_groups import DEBOUNCE_SECONDS, MediaGroupBuffer
from balam.message_text import (
    forward_reply_prefix,
    forwarded_slash_command,
    strip_bot_mention_from_command,
)
from balam.router import Router
from balam.store import SessionStore
from balam.telegram_utils import thread_kwargs
from balam.topics import (
    create_topic_from_general,
    is_forum_general_message,
)
from balam.turns import TurnRegistry, notify_error, submit_turn

logger = logging.getLogger(__name__)


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    # An album arrives as one message per photo sharing a ``media_group_id``, and
    # nothing in the Bot API says which one is last (see :mod:`balam.media_groups`).
    # Park the group and let a timer flush it, so the agent is handed every photo
    # in one turn instead of the first starting a turn and the rest folding into
    # it one at a time. Without a JobQueue there is nothing to flush with, so the
    # message takes the ordinary path.
    group_id = getattr(message, "media_group_id", None)
    job_queue = getattr(context.application, "job_queue", None)
    if group_id is not None and job_queue is not None:
        buffer: MediaGroupBuffer = context.application.bot_data.setdefault(
            "media_groups", MediaGroupBuffer()
        )
        if buffer.add(group_id, message):
            job_queue.run_once(_flush_media_group, DEBOUNCE_SECONDS, data=group_id)
        return

    await _dispatch_messages([message], context)


async def _flush_media_group(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch a buffered album as a single turn (the JobQueue callback)."""
    buffer: MediaGroupBuffer = context.application.bot_data["media_groups"]
    messages = buffer.take(context.job.data)
    if messages:
        await _dispatch_messages(messages, context)


async def _dispatch_messages(messages: list[Message], context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run one or more messages as a single turn.

    ``messages`` is one message in the ordinary case and a whole album in the
    buffered one. The first message anchors everything the turn is addressed by —
    chat, topic, forward/reply gestures, the title to auto-name from — while the
    attachments are collected across all of them.
    """
    message = messages[0]
    chat_id = message.chat_id
    thread_id = message.message_thread_id
    is_slash_command = forwarded_slash_command(message)
    # An album carries its caption on exactly one of its messages (whichever the
    # sending client chose), so the prompt is the first message that has any text.
    text = next((m.text or m.caption or "" for m in messages if m.text or m.caption), "")
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
            await show_custom_answer_on_question(context.bot, chat_id, custom_result)
            await message.reply_text("✅ Answer sent.")
            return
        if custom_result is not None and custom_result.status == "added":
            await message.reply_text("✅ Custom answer added. Select more options or tap Done.")
            return

    # Download any image/document attachments as native file parts (tier-1 plan §4);
    # the text is the message text or an attachment's caption.
    try:
        files: list[PromptFile] = []
        for item in messages:
            files.extend(await collect_attachments(item, context.bot))
    except Exception as exc:
        logger.exception("failed to download attachment")
        await notify_error(context.bot, chat_id, thread_id, exc)
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
    await submit_turn(
        message, context, text, files, thread_id=thread_id, prompt_prefix=prompt_prefix
    )


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


BOT_COMMANDS = [
    BotCommand("new", "Open a new topic: /new [context] [first message]"),
    BotCommand("rename", "Rename the current topic"),
    BotCommand("status", "Show this topic's context, session, and turn state"),
    BotCommand("model", "Show or set this topic's model override"),
    BotCommand("effort", "Show or set this topic's effort override"),
    BotCommand("cancel", "Abort the turn currently running in this topic"),
    BotCommand("tasks", "List the background work running in this topic"),
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
    # Photos of an album, held until the group is complete enough to run as one turn.
    app.bot_data["media_groups"] = MediaGroupBuffer()

    # Trust boundary (ADR-0008): filters.User gates by sender id, so only the
    # owner's messages reach the handlers; everyone else is dropped silently.
    # When a target chat is configured (ADR-0010), additionally require that
    # chat, so the bot acts only inside the workspace supergroup. Unset → the
    # legacy owner-anywhere behavior, preserving the DM round-trip.
    allowed = filters.User(user_id=config.allowed_telegram_user_id)
    if config.allowed_telegram_chat_id is not None:
        allowed = allowed & filters.Chat(chat_id=config.allowed_telegram_chat_id)

    app.add_handler(CommandHandler("new", handle_new, filters=allowed))
    app.add_handler(CommandHandler("rename", handle_rename, filters=allowed))
    app.add_handler(CommandHandler("status", handle_status, filters=allowed))
    app.add_handler(CommandHandler("model", handle_model, filters=allowed))
    app.add_handler(CommandHandler("effort", handle_effort, filters=allowed))
    app.add_handler(CommandHandler("cancel", handle_cancel, filters=allowed))
    app.add_handler(CommandHandler("tasks", handle_tasks, filters=allowed))
    app.add_handler(CommandHandler("context", handle_context, filters=allowed))
    app.add_handler(CommandHandler("diff", handle_diff, filters=allowed))
    app.add_handler(CommandHandler("browser", handle_browser, filters=allowed))
    app.add_handler(CommandHandler("artifacts", handle_artifacts, filters=allowed))
    app.add_handler(CommandHandler("delete", handle_delete, filters=allowed))
    app.add_handler(CommandHandler("schedule", handle_schedule, filters=allowed))
    # ``ATTACHMENT`` is every kind Telegram models as an attachment, not just the
    # photo/document pair Balam used to accept — voice notes, video, audio, video
    # notes, animations and stickers reach the agent through the same path now.
    # It also admits the non-file attachments (polls, contacts, locations, dice);
    # those download to nothing and fall out at the empty-message check below.
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.ATTACHMENT) & ~filters.COMMAND & allowed,
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
    app.add_handler(CallbackQueryHandler(handle_approval_callback, pattern=r"^appr:"))
    app.add_handler(CallbackQueryHandler(handle_question_done_callback, pattern=r"^qstd:"))
    app.add_handler(CallbackQueryHandler(handle_question_callback, pattern=r"^qst:"))
    app.add_handler(CallbackQueryHandler(handle_question_custom_callback, pattern=r"^qstc:"))
    app.add_handler(CallbackQueryHandler(handle_delete_confirm_callback, pattern=r"^deld:"))
    app.add_handler(CallbackQueryHandler(handle_delete_cancel_callback, pattern=r"^delx:"))
    app.add_handler(CallbackQueryHandler(handle_delete_page_callback, pattern=r"^delp:"))
    app.add_handler(CallbackQueryHandler(handle_delete_toggle_callback, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(handle_schedule_confirm_callback, pattern=r"^schd:"))
    app.add_handler(CallbackQueryHandler(handle_schedule_dismiss_callback, pattern=r"^schx:"))
    app.add_handler(CallbackQueryHandler(handle_schedule_page_callback, pattern=r"^schp:"))
    app.add_handler(CallbackQueryHandler(handle_schedule_toggle_callback, pattern=r"^sch:"))

    return app
