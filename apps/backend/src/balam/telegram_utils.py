"""Small Telegram helpers shared by the bot, the pickers and the streamer."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def thread_kwargs(thread_id: int | None) -> dict[str, Any]:
    """Bot-API kwargs that route a send to a forum topic.

    The General topic carries no ``message_thread_id`` (it is ``None``); for it we
    pass nothing so the send lands in the chat root rather than a nonexistent
    thread. Used for drafts, final messages, chat actions, and error notices so
    they all reach the same topic.
    """
    return {} if thread_id is None else {"message_thread_id": thread_id}


async def clear_keyboard(query: Any, note: str | None = None) -> bool:
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
