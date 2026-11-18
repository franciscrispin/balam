"""``/prune`` — forget topics that were deleted straight from the Telegram UI.

Telegram sends a bot no update when someone deletes a forum topic by hand, so its
row lingers locally and keeps showing up in the ``/delete`` picker. The Bot API
also has no way to list a chat's topics, so ``/prune`` asks about each stored
topic in turn and forgets the ones Telegram says are gone.

The probe is ``reopenForumTopic``, chosen because it changes nothing in the usual
case. An open topic answers ``TOPIC_NOT_MODIFIED``; a deleted one answers
``TOPIC_ID_INVALID``, the same error ``/delete`` already reads as "already gone".
A *closed* topic does get reopened, so the probe closes it again straight away.
The other calls that look harmless (a no-op ``editForumTopic``,
``sendChatAction``) succeed even for thread ids that never existed, so they
can't tell the two apart.

Only local rows are touched, and only for topics Telegram has confirmed are gone.
Any other error leaves the topic alone, so the command runs without asking
first.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from balam.router import Router
from balam.topics import topic_label

logger = logging.getLogger(__name__)

#: How many pruned topic names the summary lists before it says "…and N more".
LIST_LIMIT = 20


async def topic_exists(bot: Any, chat_id: int, thread_id: int) -> bool | None:
    """Whether forum topic ``thread_id`` still exists: ``True``/``False``, or
    ``None`` when Telegram's answer doesn't settle it (a network error, a missing
    permission). A ``None`` must never be read as "gone"."""
    try:
        await bot.reopen_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
    except BadRequest as exc:
        text = (exc.message or "").lower()
        if "topic_not_modified" in text:
            return True  # open and untouched, the usual case
        if "topic_id_invalid" in text:
            return False
        logger.warning("could not probe topic %s: %s", thread_id, exc.message)
        return None
    except Exception:
        logger.warning("could not probe topic %s", thread_id, exc_info=True)
        return None
    # It was closed and the probe just reopened it, so put it back the way it was.
    try:
        await bot.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
    except Exception:
        logger.warning("reopened closed topic %s but failed to re-close it", thread_id)
    return True


async def handle_prune(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/prune`` — drop stored topics that no longer exist in Telegram."""
    message = update.message
    if message is None:
        return
    router: Router = context.application.bot_data["router"]
    chat_id = message.chat_id
    topics = router.list_topics(chat_id)
    if not topics:
        await message.reply_text("No topics to check.")
        return

    status = await message.reply_text(f"🔎 Checking {len(topics)} topic(s)…")

    pruned: list[str] = []
    unknown = 0
    for thread_id, title, ctx in topics:
        exists = await topic_exists(context.bot, chat_id, thread_id)
        if exists is None:
            unknown += 1
        elif not exists:
            router.purge_topic(chat_id, thread_id)
            pruned.append(topic_label(title, ctx, thread_id))

    kept = len(topics) - len(pruned) - unknown
    if pruned:
        lines = [f"🧹 Forgot {len(pruned)} topic(s) that were deleted in Telegram:"]
        lines += [f"• {name}" for name in pruned[:LIST_LIMIT]]
        if len(pruned) > LIST_LIMIT:
            lines.append(f"…and {len(pruned) - LIST_LIMIT} more.")
        lines.append(f"\n{kept} topic(s) still open.")
    else:
        lines = [f"✅ All {kept} topic(s) still exist — nothing to prune."]
    if unknown:
        lines.append(f"⚠️ {unknown} couldn't be checked and were kept; see the logs.")
    text = "\n".join(lines)

    # If editing the status message fails, send the summary as a new reply.
    try:
        await status.edit_text(text)
    except Exception:
        logger.debug("failed to edit /prune status message", exc_info=True)
        await message.reply_text(text)
