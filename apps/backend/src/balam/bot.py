"""The Telegram bot: the system's trust boundary (ADR-0008).

Two responsibilities for this slice:
  1. Allowlist — accept updates only from the single owner's numeric user ID;
     everyone else is silently ignored (a stranger's update matches no handler).
  2. Route text messages — map the topic to its OpenCode session (ADR-0009) and
     stream the agent's reply back into the same topic.
  3. Handle ``/context`` — list workspaces, or open a new topic bound to one.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from balam.config import Config
from balam.opencode import OpenCode
from balam.router import Router, TopicRef
from balam.streamer import stream_reply
from balam.telegram_utils import thread_kwargs

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


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None:
        return

    chat_id = message.chat_id
    thread_id = message.message_thread_id

    router: Router = context.application.bot_data["router"]
    opencode: OpenCode = context.application.bot_data["opencode"]

    try:
        resolved = await router.resolve(
            TopicRef(chat_id=chat_id, thread_id=thread_id, title=_topic_title(message, thread_id))
        )
        await stream_reply(
            bot=context.bot,
            opencode=opencode,
            session_id=resolved.session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            prompt=message.text,
            directory=resolved.directory,
            provider=resolved.provider,
            model=resolved.model,
            effort=resolved.effort,
        )
    except Exception as exc:
        # OpenCode error → post a short message into the topic (ADR-0009 edge).
        logger.exception("failed to handle message")
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=f"⚠️ {exc}", **thread_kwargs(thread_id)
            )
        except Exception:
            logger.debug("failed to deliver error notice", exc_info=True)


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


async def _handle_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/context`` lists workspaces and the topic's current binding;
    ``/context <name>`` creates a *new* topic bound to that context and replies
    with a one-tap link to it.

    Switching never rebinds the current topic: one context per topic for life,
    so a topic's session always remembers its own history. The Bot API can't
    move the user's view, so we create the topic, greet inside it, and hand back
    a deep link to tap.
    """
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

    ctx = contexts.contexts[name]

    # Create a fresh forum topic for the context (allowing duplicate names: many
    # topics may share one context). Requires a forum supergroup and the bot to
    # be an admin with "Manage Topics".
    try:
        topic = await context.bot.create_forum_topic(chat_id=message.chat_id, name=name)
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
        # to default_context, not the one the user asked for. Best-effort delete.
        try:
            await context.bot.delete_forum_topic(
                chat_id=message.chat_id, message_thread_id=new_thread_id
            )
        except Exception:
            logger.debug("failed to delete orphan topic after session failure", exc_info=True)
        await message.reply_text(f"⚠️ Couldn't start a session for {name!r}: {exc}")
        return

    # Greet inside the new topic so it isn't empty, then hand back a one-tap
    # link in the originating chat/topic as an inline URL button.
    await context.bot.send_message(
        chat_id=message.chat_id,
        text=f"🗂 Context {name} — {ctx.directory}\nSend a message to start.",
        message_thread_id=new_thread_id,
    )
    link = _topic_link(message.chat_id, new_thread_id, bot_id=context.bot.id)
    if link:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Go to topic", url=link)]])
        await message.reply_text(f"Opened a new {name} topic.", reply_markup=keyboard)
    else:
        await message.reply_text(f"Opened a new {name} topic — pick it from the topic list.")


#: The slash commands Balam exposes. Registering them via ``setMyCommands`` is
#: what makes ``/context`` discoverable and reliably routed to the bot in a
#: group, where clients dispatch slash commands by the bot's registered list.
BOT_COMMANDS = [
    BotCommand("context", "List workspace contexts, or open a new topic bound to one"),
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

    # Trust boundary (ADR-0008): filters.User gates by sender id, so only the
    # owner's messages reach the handlers; everyone else is dropped silently.
    # When a target chat is configured (ADR-0010), additionally require that
    # chat, so the bot acts only inside the balamies supergroup. Unset → the
    # legacy owner-anywhere behavior, preserving the DM round-trip.
    allowed = filters.User(user_id=config.allowed_telegram_user_id)
    if config.allowed_telegram_chat_id is not None:
        allowed = allowed & filters.Chat(chat_id=config.allowed_telegram_chat_id)

    app.add_handler(CommandHandler("context", _handle_context, filters=allowed))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & allowed, _handle_message))

    return app
