"""The Telegram bot: the system's trust boundary (ADR-0008).

Two responsibilities for this slice:
  1. Allowlist — accept updates only from the single owner's numeric user ID;
     everyone else is silently ignored (a stranger's update matches no handler).
  2. Route text messages — map the topic to its OpenCode session (ADR-0009) and
     stream the agent's reply back into the same topic.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from balam.config import Config
from balam.opencode import OpenCode
from balam.router import Router, TopicRef
from balam.streamer import stream_reply

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
    thread_kwargs: dict[str, Any] = {} if thread_id is None else {"message_thread_id": thread_id}

    router: Router = context.application.bot_data["router"]
    opencode: OpenCode = context.application.bot_data["opencode"]

    try:
        session_id = await router.resolve(
            TopicRef(chat_id=chat_id, thread_id=thread_id, title=_topic_title(message, thread_id))
        )
        await stream_reply(
            bot=context.bot,
            opencode=opencode,
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            prompt=message.text,
        )
    except Exception as exc:
        # OpenCode error → post a short message into the topic (ADR-0009 edge).
        logger.exception("failed to handle message")
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ {exc}", **thread_kwargs)
        except Exception:
            logger.debug("failed to deliver error notice", exc_info=True)


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
    # owner's text messages reach the handler; everyone else is dropped silently.
    owner_only = filters.User(user_id=config.allowed_telegram_user_id)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & owner_only, _handle_message))

    return app
