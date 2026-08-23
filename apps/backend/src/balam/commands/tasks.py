"""``/tasks`` — what the agent has running in the background right now.

The terminal shows a running background task in the session itself; a Telegram
topic has nowhere to put that, so until now the only report was the turn-end
notice naming what got *stopped*. This command asks the same question while the
work is still alive: what is running, and is the turn waiting on it (ADR-0017).

Read-only and synchronous by design — it reports the live set the running turn
keeps published on the backend, so answering never has to interrupt that turn.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from balam.agent.backend import AgentBackend
from balam.turns import TurnRegistry

logger = logging.getLogger(__name__)


async def handle_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/tasks`` — list this topic's running background work."""
    message = update.message
    if message is None:
        return

    backend: AgentBackend = context.application.bot_data["backend"]
    turns: TurnRegistry = context.application.bot_data["turns"]
    chat_id = message.chat_id
    thread_id = message.message_thread_id

    tasks = backend.background_tasks(chat_id, thread_id)
    if not tasks:
        # Two different "nothing running", worth separating: a turn that is
        # working just hasn't backgrounded anything, which is the common case and
        # not what the owner is checking for.
        running = turns.get(chat_id, thread_id) is not None
        await message.reply_text(
            "No background tasks running in this topic."
            + (" The turn is still working." if running else "")
        )
        return

    # Plain text, like every other command reply: no parse_mode means Telegram
    # shows what we write, and a task description is the agent's own free text.
    heading = (
        "⚙️ 1 background task running:"
        if len(tasks) == 1
        else f"⚙️ {len(tasks)} background tasks running:"
    )
    lines = [heading, *(f"• {task.description}" for task in tasks)]
    lines += [
        "",
        "The turn stays open while these run, and I'll report each one as it "
        "finishes. You can keep sending messages — they reach me straight away.",
    ]
    await message.reply_text("\n".join(lines))
