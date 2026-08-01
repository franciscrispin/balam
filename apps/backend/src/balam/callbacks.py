"""Inline-keyboard callbacks for tool approvals and agent questions.

When the agent needs the owner's decision — "may I run this?", "which of these?"
— the streamer posts a keyboard and parks the turn on a future. These handlers
are the other end: they re-check the trust boundary (a callback query carries no
handler filter of its own), resolve the parked future, and leave the spent
message showing what was decided.

Kept apart from the commands because the flow is inverted. A command starts
something the owner asked for; these finish something the *agent* asked for, and
a wrong answer here resumes a turn with the wrong decision.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from balam.approvals import Choice, CustomAnswer, PendingApprovals, PendingQuestions
from balam.auth import is_owner
from balam.config import Config
from balam.markdown import escape_markdown_v2
from balam.stream_render import _question_keyboard
from balam.telegram_utils import clear_keyboard

logger = logging.getLogger(__name__)

#: How long a resolved approval message lingers before it is deleted, so the
#: owner can see the outcome without the topic filling with spent keyboards.
APPROVAL_DELETE_DELAY_S = 2.0


_CHOICE_FEEDBACK = {
    Choice.ALLOW: ("✅ Approved\\.", "Approved."),
    Choice.ALL: (
        "✅ Approved — accepting all edits this session\\.",
        "Accepting all edits.",
    ),
    Choice.DENY: ("❌ Denied\\.", "Denied."),
}


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await clear_keyboard(query)
        return

    note, toast = _CHOICE_FEEDBACK[choice]
    await query.answer(toast)
    updated = await clear_keyboard(query, note=note)
    if updated and choice is not Choice.DENY:
        schedule_approval_cleanup(context, query.message)


async def handle_question_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            await clear_keyboard(query)
            return
        await query.answer("Selected." if selected else "Unselected.")
        await _refresh_question_keyboard(query, pending_questions, token, q_index)
        return

    labels = pending_questions.labels(token, q_index)
    if not pending_questions.resolve(token, q_index, o_index):
        await query.answer("This question has expired.")
        await clear_keyboard(query)
        return
    chosen = [labels[o_index]] if labels and 0 <= o_index < len(labels) else []
    await query.answer("Answered.")
    await clear_keyboard(query, note=_answered_note(chosen))


async def handle_question_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await clear_keyboard(query)
        return
    await query.answer("Answered.")
    await clear_keyboard(query, note=_answered_note(chosen))


async def handle_question_custom_callback(
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
        await clear_keyboard(query)
        return
    if pending_questions.is_multiple(token, q_index):
        await query.answer("Send your custom answer, then tap Done.")
        return
    await query.answer("Send your answer as the next message in this topic.")
    await clear_keyboard(query, note=r"Reply with your answer\.")


async def show_custom_answer_on_question(bot: Any, chat_id: int, outcome: CustomAnswer) -> None:
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


def schedule_approval_cleanup(context: ContextTypes.DEFAULT_TYPE, message: Any) -> None:
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
