"""``/delete`` — remove forum topics from an inline checklist.

Deleting a topic is irreversible and takes its session history with it, so the
flow is deliberately two-step: pick from a paged checklist, then confirm. The
picker machinery is shared with ``/schedule cancel`` (:mod:`balam.pickers`);
what is specific here is the topic list and what deletion actually means —
removing the forum topic *and* forgetting its stored session mapping.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from balam.approvals import PendingPicks
from balam.auth import callback_authorized
from balam.config import Config
from balam.pickers import (
    DELETE_PICKER,
    handle_picker_page,
    handle_picker_toggle,
    picker_markup,
)
from balam.router import Router
from balam.telegram_utils import clear_keyboard
from balam.topics import topic_label

logger = logging.getLogger(__name__)


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        text, reply_markup=picker_markup(DELETE_PICKER, pending_deletions, token)
    )


async def handle_delete_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a topic's checkbox in the /delete picker (``del:<token>:<thread_id>``)."""
    await handle_picker_toggle(update, context, DELETE_PICKER, "pending_deletions")


async def handle_delete_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip the /delete picker to another page (``delp:<token>:<page>``)."""
    await handle_picker_page(update, context, DELETE_PICKER, "pending_deletions")


async def handle_delete_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete the topics selected in the picker (``deld:<token>``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("deld:"):
        return
    config: Config = context.application.bot_data["config"]
    if not callback_authorized(query, config):
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
        await clear_keyboard(query)
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


async def handle_delete_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dismiss the /delete picker without deleting anything (``delx:<token>``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("delx:"):
        return
    config: Config = context.application.bot_data["config"]
    if not callback_authorized(query, config):
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
