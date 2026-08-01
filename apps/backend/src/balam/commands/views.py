"""Commands that open a Mini App view, or list what the agent produced.

``/diff`` and ``/browser`` hand back a Mini App launch button — the git diff
viewer for the topic's workspace, and the live view of the agent's Chrome
(ADR-0006). ``/artifacts`` is the odd one out: it has no Mini App view and
instead asks the agent to list what it has published.

They are grouped because none of them touch the topic's session — they only
need to know which context the topic is bound to.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from balam.config import Config
from balam.miniapp import mini_app_reply
from balam.router import Router, TopicRef
from balam.topics import is_forum_general_message, topic_title
from balam.turns import submit_turn

logger = logging.getLogger(__name__)


_ARTIFACT_SCOPES = ("mine", "shared", "all")


async def handle_artifacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/artifacts [shared|all]`` — list the owner's published claude.ai artifacts.

    The CLI's own ``/artifacts`` is a local-jsx screen that only exists in
    interactive sessions, so it can never run through the SDK backend. The data
    behind it is reachable anyway: the Artifact tool's ``action:"list"`` returns
    the published pages (title, url, updatedAt). This command submits a regular
    turn instructing the agent to call it — which also means it degrades to a
    plain "tool unavailable" answer on a backend without the Artifact tool
    (OpenCode, or an account the rollout gate excludes).
    """
    message = update.message
    if message is None:
        return

    if is_forum_general_message(message):
        # General messages spawn fresh topics, so the listing would land in a
        # topic named after a bare command.
        await message.reply_text("Use /artifacts inside a topic.")
        return

    args = context.args or []
    scope = args[0].lower() if args else "mine"
    if scope not in _ARTIFACT_SCOPES:
        await message.reply_text("Usage: /artifacts [shared|all] — default lists your own.")
        return

    prompt = (
        f'Call the Artifact tool with action "list" and scope "{scope}", then present '
        "the artifacts as a compact list: each title as a link to its URL, with the "
        "updated date when available. Mention if the result was truncated. Do not "
        "take any other action. If the Artifact tool is not available in this "
        "session, say so instead of improvising."
    )
    await submit_turn(
        message,
        context,
        prompt,
        [],
        thread_id=message.message_thread_id,
    )


async def handle_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/diff`` — open the Mini App git diff viewer for this topic's context."""
    message = update.message
    if message is None:
        return

    config: Config = context.application.bot_data["config"]
    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
    )
    name = router.current_context_name(ref)
    text, keyboard = mini_app_reply(
        config,
        "diff",
        name,
        bot_username=getattr(context.bot, "username", None),
        is_private=getattr(message.chat, "type", None) == "private",
    )
    await message.reply_text(text, reply_markup=keyboard)


async def handle_browser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/browser`` — open the Mini App live view of the agent's Chrome (ADR-0006).

    The view is global (one X display on the VM), not per-context, so the launch
    carries no context: a placeholder would leak into the app shell's shared
    launch context and break the other views (e.g. the diff view 404s on an
    unknown context name).
    """
    message = update.message
    if message is None:
        return

    config: Config = context.application.bot_data["config"]
    text, keyboard = mini_app_reply(
        config,
        "browser",
        None,
        bot_username=getattr(context.bot, "username", None),
        is_private=getattr(message.chat, "type", None) == "private",
        label="Watch live",
        heading="Live browser view:",
    )
    await message.reply_text(text, reply_markup=keyboard)
