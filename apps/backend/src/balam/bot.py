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
from urllib.parse import quote

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from balam.approvals import Choice, PendingApprovals
from balam.attachments import collect_attachments
from balam.config import Config
from balam.opencode import OpenCode
from balam.router import Router, TopicRef
from balam.streamer import stream_reply
from balam.telegram_utils import thread_kwargs
from balam.turns import TurnRegistry

logger = logging.getLogger(__name__)


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

    # Download any image/document attachments as native file parts (tier-1 plan §4);
    # the text is the message text or an attachment's caption.
    try:
        files = await collect_attachments(message, context.bot)
    except Exception as exc:
        logger.exception("failed to download attachment")
        await _notify_error(context.bot, chat_id, thread_id, exc)
        return
    text = message.text or message.caption or ""
    if not text and not files:
        return

    router: Router = context.application.bot_data["router"]
    opencode: OpenCode = context.application.bot_data["opencode"]
    turns: TurnRegistry = context.application.bot_data["turns"]
    pending: PendingApprovals = context.application.bot_data["pending"]

    try:
        resolved = await router.resolve(
            TopicRef(
                chat_id=chat_id,
                thread_id=thread_id,
                title=_topic_title(message, thread_id),
            )
        )
    except Exception as exc:
        # Couldn't even resolve the session (OpenCode down, etc.) — report and stop.
        logger.exception("failed to resolve session")
        await _notify_error(context.bot, chat_id, thread_id, exc)
        return

    # Run the turn as a background task registered in the turn registry, so the
    # handler returns immediately and a concurrent /cancel update can interrupt it
    # (PTB processes updates sequentially, so awaiting here would block /cancel).
    async def run() -> None:
        try:
            await stream_reply(
                bot=context.bot,
                opencode=opencode,
                session_id=resolved.session_id,
                chat_id=chat_id,
                thread_id=thread_id,
                prompt=text,
                directory=resolved.directory,
                provider=resolved.provider,
                model=resolved.model,
                effort=resolved.effort,
                pending=pending,
                allowed_dirs=[resolved.directory, *resolved.additional_directories],
                files=files,
            )
        except asyncio.CancelledError:
            raise  # /cancel aborted the turn; let the task settle as cancelled.
        except Exception as exc:
            logger.exception("failed to handle message")
            await _notify_error(context.bot, chat_id, thread_id, exc)
        finally:
            turns.clear(chat_id, thread_id, task)

    task = asyncio.create_task(run())
    turns.register(chat_id, thread_id, task, resolved.session_id, resolved.directory)


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

    name = args[0]
    if name not in contexts.contexts:
        available = ", ".join(sorted(contexts.contexts))
        await message.reply_text(f"Unknown context {name!r}. Available: {available}")
        return

    await _open_context_topic(message, context.bot, router, name)


def _abort_turn(
    turn: Any, opencode: OpenCode, tasks: set[asyncio.Task[None]]
) -> asyncio.Task[None] | None:
    """Cancel a running turn locally and abort it server-side (best-effort).

    Cancelling the local task stops streaming; the abort tells OpenCode to stop
    generating. The abort runs as a background task so callers needn't await the
    round-trip before replying — but it is anchored in ``tasks`` (with a done
    callback that removes it) because the event loop keeps only a *weak*
    reference to a bare task: an unanchored one can be garbage-collected
    mid-flight, dropping the abort and leaving OpenCode generating. ``None`` when
    there is no turn.
    """
    if turn is None:
        return None
    turn.task.cancel()
    task = asyncio.create_task(opencode.abort_session(turn.session_id, directory=turn.directory))
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
        name = args[0]
        if name not in router.contexts.contexts:
            available = ", ".join(sorted(router.contexts.contexts))
            await message.reply_text(f"Unknown context {name!r}. Available: {available}")
            return
    else:
        ref = TopicRef(
            chat_id=message.chat_id,
            thread_id=message.message_thread_id,
            title=_topic_title(message, message.message_thread_id),
        )
        name = router.current_context_name(ref)
    await _open_context_topic(message, context.bot, router, name)


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/status`` — report the topic's context, session, and whether a turn runs."""
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    turns: TurnRegistry = context.application.bot_data["turns"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=_topic_title(message, message.message_thread_id),
    )

    name = router.current_context_name(ref)
    ctx = router.contexts.get(name)
    session_id = router.current_session_id(ref)
    running = turns.get(ref.chat_id, ref.thread_id) is not None

    lines = [
        f"Context: {name}",
        f"Directory: {ctx.directory}",
        f"Model: {ctx.model or '(server default)'}",
        f"Effort: {ctx.effort or '(server default)'}",
        f"Session: {session_id or '(none yet — send a message to start)'}",
        f"Turn: {'running' if running else 'idle'}",
    ]
    await message.reply_text("\n".join(lines))


def _mini_app_url(config: Config, view: str, context_name: str) -> tuple[str, bool]:
    """Build the Mini App URL for ``view`` bound to ``context_name``.

    Returns ``(url, is_public)``. With a configured HTTPS base, the URL is public
    and can back a native ``web_app`` button; otherwise it is the local
    ``127.0.0.1`` address, which Telegram won't open in-app (ADR-0007) but the
    owner can open in a browser.
    """
    base = config.balam_public_url or f"http://127.0.0.1:{config.balam_port}"
    is_public = config.balam_public_url is not None
    return f"{base}/?view={view}&context={quote(context_name)}", is_public


async def _handle_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/diff`` — open the Mini App git diff viewer for this topic's context.

    With a public HTTPS base (``BALAM_PUBLIC_URL``) we attach a native ``web_app``
    button that opens inside Telegram with ``initData`` auth; without one we reply
    with the local URL to open in a browser, since Telegram requires HTTPS for
    web_app buttons and the VM is localhost-only (ADR-0007)."""
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
    url, is_public = _mini_app_url(config, "diff", name)
    is_private = getattr(message.chat, "type", None) == "private"

    # Preferred path: a direct Mini App link (ADR-0013). It opens the app inside
    # Telegram's webview — with signed initData — in ANY chat type, so it works in
    # the workspace supergroup where web_app inline buttons are rejected. Needs a
    # BotFather-registered short name and the bot's username. The start_param
    # carries view+context as "view__context" (Telegram allows [A-Za-z0-9_-]).
    shortname = config.balam_miniapp_shortname
    bot_username = getattr(context.bot, "username", None)
    if is_public and shortname and bot_username:
        start_param = f"diff__{name}"
        link = f"https://t.me/{bot_username}/{shortname}?startapp={quote(start_param)}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("View changes", url=link)]])
        await message.reply_text(f"Changes in {name}:", reply_markup=keyboard)
        return

    if is_public and is_private:
        # web_app inline buttons open the Mini App inside Telegram's webview (with
        # initData, ADR-0008) — but Telegram allows them ONLY in private chats.
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("View changes", web_app=WebAppInfo(url=url))]]
        )
        await message.reply_text(f"Changes in {name}:", reply_markup=keyboard)
    elif is_public:
        # In groups/supergroups a web_app inline button is rejected
        # (Button_type_invalid). Offer a plain URL button instead; it opens in the
        # external browser (no initData there), so the Mini App needs the owner's
        # Telegram session to authenticate — see the note.
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("View changes", url=url)]])
        await message.reply_text(
            f"Changes in {name}: {url}\n\n"
            "Opens in your browser. (Telegram only allows the in-app Mini App button "
            "in a private chat with the bot.)",
            reply_markup=keyboard,
        )
    else:
        await message.reply_text(
            f"Diff viewer for {name}:\n{url}\n\n"
            "Opens in a browser. To open inside Telegram, serve the Mini App from "
            "a public HTTPS URL and set BALAM_PUBLIC_URL (ADR-0013)."
        )


async def _handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/cancel`` — abort the turn running in the current topic, if any."""
    message = update.message
    if message is None:
        return

    opencode: OpenCode = context.application.bot_data["opencode"]
    turns: TurnRegistry = context.application.bot_data["turns"]

    turn = turns.get(message.chat_id, message.message_thread_id)
    if turn is None:
        await message.reply_text("No running turn.")
        return

    tasks: set[asyncio.Task[None]] = context.application.bot_data.setdefault(
        "background_tasks", set()
    )
    _abort_turn(turn, opencode, tasks)
    await message.reply_text("🛑 Cancelled.")


#: Per approval choice: ``(inline note appended to the prompt — already
#: MarkdownV2-escaped, toast shown on the callback answer)``.
_CHOICE_FEEDBACK = {
    Choice.ALLOW: ("✅ Allowed\\.", "Allowed."),
    Choice.ALL: (
        "✅ Allowed — accepting all edits this session\\.",
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
    await _clear_keyboard(query, note=note)


async def _clear_keyboard(query: Any, note: str | None = None) -> None:
    """Strip a spent approval keyboard, appending a one-line outcome when given.

    ``note`` (when set) must already be MarkdownV2-escaped. Best-effort: a failed
    edit — e.g. a message too old to edit — is logged, not raised; the callback
    answer already told the user the outcome.
    """
    message = getattr(query, "message", None)
    if message is None:
        return
    original = message.text_markdown_v2 or message.text or ""
    text = f"{original}\n\n{note}" if note else original
    try:
        await message.edit_text(text=text, parse_mode="MarkdownV2", reply_markup=None)
    except Exception:
        logger.debug("failed to update spent approval message", exc_info=True)


#: The slash commands Balam exposes. Registering them via ``setMyCommands`` is
#: what makes them discoverable and reliably routed to the bot in a group, where
#: clients dispatch slash commands by the bot's registered list.
BOT_COMMANDS = [
    BotCommand("new", "Open a new topic (this context, or /new <name> for another)"),
    BotCommand("status", "Show this topic's context, session, and turn state"),
    BotCommand("cancel", "Abort the turn currently running in this topic"),
    BotCommand("context", "List workspace contexts, or open a new topic bound to one"),
    BotCommand("diff", "Open the Mini App git diff viewer for this topic's context"),
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
    opencode: OpenCode,
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
    app.bot_data["opencode"] = opencode
    app.bot_data["router"] = router
    # In-flight turns, keyed by topic, so /cancel can interrupt a running reply.
    app.bot_data["turns"] = TurnRegistry()
    # Outstanding tool-approval prompts + per-session "accept all edits" state.
    app.bot_data["pending"] = PendingApprovals()
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
    app.add_handler(CommandHandler("status", _handle_status, filters=allowed))
    app.add_handler(CommandHandler("cancel", _handle_cancel, filters=allowed))
    app.add_handler(CommandHandler("context", _handle_context, filters=allowed))
    app.add_handler(CommandHandler("diff", _handle_diff, filters=allowed))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND & allowed,
            _handle_message,
        )
    )
    # CallbackQueryHandler takes no filter; the handler re-checks the trust
    # boundary (ADR-0008) itself before resolving an approval.
    app.add_handler(CallbackQueryHandler(_handle_approval_callback, pattern=r"^appr:"))

    return app
