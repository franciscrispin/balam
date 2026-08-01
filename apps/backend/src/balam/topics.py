"""Forum topics: naming them, opening them, and linking to them.

A *topic* is Balam's unit of conversation (ADR-0009): one forum topic binds to
one workspace context for its lifetime and owns one agent session. This module
holds everything about the topic itself — deriving a name from the first message,
creating the topic and its session (rolling back if either half fails), and
building the one-tap deep link that lets the owner jump to it.

It deliberately knows nothing about commands or turns, so ``/context``,
``/new``, a message in General, and a ``/schedule`` timer can all open a topic
through the same path. :func:`open_topic_in_context` takes no originating
message for exactly that reason — a timer has none to give.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from balam.router import Router, TopicRef

logger = logging.getLogger(__name__)


def topic_title(message: Any, thread_id: int | None) -> str:
    """Best-effort human label for a freshly created session."""
    reply_to = getattr(message, "reply_to_message", None)
    created = getattr(reply_to, "forum_topic_created", None)
    name = getattr(created, "name", None)
    if name:
        return name
    if thread_id is None:
        return "General"
    return f"Topic {thread_id}"


def is_forum_general_message(message: Any) -> bool:
    """True for the General channel of a forum supergroup, not plain DMs."""
    chat = getattr(message, "chat", None)
    return message.message_thread_id is None and bool(getattr(chat, "is_forum", False))


def topic_name(context_name: str, first_message: str, *, has_files: bool = False) -> str:
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


async def rename_forum_topic(bot: Any, chat_id: int, thread_id: int, name: str) -> None:
    """Rename a normal forum topic."""
    await bot.edit_forum_topic(chat_id=chat_id, message_thread_id=thread_id, name=name)


async def auto_name_topic(
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
    name = topic_name(context_name, first_message, has_files=has_files)
    try:
        await rename_forum_topic(bot, ref.chat_id, ref.thread_id, name)
    except Exception:
        logger.debug("failed to auto-name topic", exc_info=True)
        return
    router.mark_topic_auto_named(ref)
    router.set_topic_title(ref.chat_id, ref.thread_id, name)


async def create_topic_from_general(
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
        title=topic_title(message, None),
    )
    context_name = router.current_context_name(general_ref)
    name = topic_name(context_name, text, has_files=has_files)
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

    link = topic_link(message.chat_id, thread_id, bot_id=getattr(bot, "id", None))
    if link:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Go to topic", url=link)]])
        await message.reply_text(f"Opened {name}.", reply_markup=keyboard)
    else:
        await message.reply_text(f"Opened {name} — pick it from the topic list.")
    return thread_id


def topic_link(chat_id: int, thread_id: int, bot_id: int | None = None) -> str | None:
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


class TopicOpenError(Exception):
    """A new topic couldn't be created or bound. The message is owner-facing —
    :func:`open_topic_in_context` has already rolled back, so the caller only has
    to deliver it wherever it has somewhere to write."""


async def open_topic_in_context(
    bot: Any, router: Router, chat_id: int, name: str, *, prompt: str = ""
) -> tuple[int, str]:
    """Create a fresh forum topic bound to context ``name``, start its session,
    and greet inside it. Returns ``(thread_id, topic_name)``; raises
    :class:`TopicOpenError` if the topic couldn't be opened.

    Takes no originating :class:`~telegram.Message`, because two callers have
    none to give: ``/schedule`` fires from a timer (ADR-0016), and the
    ``/context`` / ``/new`` wrapper below wants to own its own reply anyway. The
    "Opened …" reply and the one-tap deep-link button therefore live in
    :func:`open_context_topic`, not here.

    One context per topic for life — we never rebind an existing topic, so a
    topic's session always remembers its own history. The Bot API can't move the
    user's view, so we create the topic and greet it; the caller hands back a deep
    link to tap. Requires a forum supergroup with the bot an admin holding "Manage
    Topics"; duplicate topic names are fine (many topics may share one context).

    With a ``prompt`` the topic is named after it (``context: prompt``, as a
    General message would), and marked auto-named so the first turn doesn't
    rename it again.
    """
    ctx = router.contexts.contexts[name]
    title = topic_name(name, prompt) if prompt else name
    try:
        topic = await bot.create_forum_topic(chat_id=chat_id, name=title)
    except Exception as exc:
        logger.exception("failed to create forum topic")
        raise TopicOpenError(
            f"⚠️ Couldn't create a topic for {name!r}: {exc}\n"
            "This chat must be a forum supergroup and the bot an admin with "
            "the 'Manage Topics' permission."
        ) from exc

    new_thread_id = topic.message_thread_id
    try:
        await router.create_topic_session(
            chat_id, new_thread_id, title, name, auto_named=bool(prompt)
        )
    except Exception as exc:
        logger.exception("failed to start session for new topic")
        # Roll back the just-created topic: an unbound topic would silently route
        # to default_context, not the one we meant. Best-effort delete.
        try:
            await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=new_thread_id)
        except Exception:
            logger.debug("failed to delete orphan topic after session failure", exc_info=True)
        raise TopicOpenError(f"⚠️ Couldn't start a session for {name!r}: {exc}") from exc

    # Greet inside the new topic so it isn't empty. With a prompt the turn lands
    # right below the header, so don't ask for a message.
    header = f"🗂 Context {name} — {ctx.directory}"
    await bot.send_message(
        chat_id=chat_id,
        text=header if prompt else f"{header}\nSend a message to start.",
        message_thread_id=new_thread_id,
    )
    return new_thread_id, title


async def open_context_topic(
    message: Any, bot: Any, router: Router, name: str, *, prompt: str = ""
) -> int | None:
    """``/context <name>`` / ``/new``'s wrapper around
    :func:`open_topic_in_context`: open the topic, then reply in the originating
    chat/topic with a one-tap link to it. Returns the new topic's thread id, or
    ``None`` when it couldn't be opened — the caller runs ``prompt`` there (see
    :func:`_submit_turn`)."""
    try:
        new_thread_id, topic_name = await open_topic_in_context(
            bot, router, message.chat_id, name, prompt=prompt
        )
    except TopicOpenError as exc:
        await message.reply_text(str(exc))
        return None

    opened = f"Opened {topic_name}." if prompt else f"Opened a new {name} topic."
    link = topic_link(message.chat_id, new_thread_id, bot_id=bot.id)
    if link:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Go to topic", url=link)]])
        await message.reply_text(opened, reply_markup=keyboard)
    else:
        await message.reply_text(f"{opened} Pick it from the topic list.")
    return new_thread_id


def topic_label(title: str | None, context_name: str | None, thread_id: int) -> str:
    """Button label for a topic: its title, else the bound context + thread id
    (topics created before titles were tracked have no stored title)."""
    base = title or (f"{context_name} · #{thread_id}" if context_name else f"#{thread_id}")
    return base if len(base) <= 48 else base[:47] + "…"
