"""Commands that act on a topic's session: open, inspect, retune, stop.

``/context`` and ``/new`` open a *new* topic rather than rebinding this one —
one context per topic for life (ADR-0012), so a topic's session always remembers
its own history. The rest read or adjust the session bound to the topic they are
sent in: ``/status`` reports it, ``/model`` and ``/effort`` override it per
topic, ``/rename`` retitles it, and ``/cancel`` aborts the turn running in it.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from balam.agent.backend import AgentBackend
from balam.config import Config
from balam.contexts import EFFORT_LEVELS, split_provider_model
from balam.message_text import command_remainder
from balam.router import Router, TopicRef
from balam.topics import (
    is_forum_general_message,
    open_context_topic,
    rename_forum_topic,
    topic_title,
)
from balam.turns import TurnRegistry, abort_turn, submit_turn

logger = logging.getLogger(__name__)


async def handle_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/context`` lists workspaces and the topic's current binding;
    ``/context <name> [prompt]`` creates a *new* topic bound to that context,
    replies with a one-tap link to it (see :func:`open_context_topic`), and runs
    ``prompt`` there when one is given."""
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
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

    name = contexts.match_name(args[0])
    if name is None:
        available = ", ".join(sorted(contexts.contexts))
        await message.reply_text(f"Unknown context {args[0]!r}. Available: {available}")
        return

    prompt = command_remainder(message.text or "", args_consumed=1)
    thread_id = await open_context_topic(message, context.bot, router, name, prompt=prompt)
    if thread_id is not None and prompt:
        await submit_turn(message, context, prompt, [], thread_id=thread_id)


async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/new [name] [prompt]`` — open a fresh topic with a new session, and run
    ``prompt`` in it when one is given.

    The same flow as ``/context <name>`` (:func:`open_context_topic`). With an
    argument, the new topic is bound to context ``name``; without one, it reuses
    the current topic's binding (``default_context`` when unbound). The current
    topic is left untouched — one context per topic for life — so its history is
    preserved and the fresh start lives in its own topic.

    Everything after the context name is the first turn, so ``/new monies check
    last month`` opens a *monies* topic and starts working there in one step —
    the same one-step start a plain message in General gives, with the context
    picked explicitly. The first token still has to name a context: a prompt
    alone would silently run in whichever context this topic happens to be bound
    to, and a plain message already covers that.
    """
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    args = context.args or []
    if args:
        name = router.contexts.match_name(args[0])
        if name is None:
            available = ", ".join(sorted(router.contexts.contexts))
            await message.reply_text(f"Unknown context {args[0]!r}. Available: {available}")
            return
        prompt = command_remainder(message.text or "", args_consumed=1)
    else:
        ref = TopicRef(
            chat_id=message.chat_id,
            thread_id=message.message_thread_id,
            title=topic_title(message, message.message_thread_id),
        )
        name = router.current_context_name(ref)
        prompt = ""
    thread_id = await open_context_topic(message, context.bot, router, name, prompt=prompt)
    if thread_id is not None and prompt:
        await submit_turn(message, context, prompt, [], thread_id=thread_id)


#: Scopes the Artifact tool's ``list`` action accepts; ``mine`` is its default.
async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/status`` — report the topic's context, session, and whether a turn runs."""
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    turns: TurnRegistry = context.application.bot_data["turns"]
    config: Config = context.application.bot_data["config"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
    )

    name = router.current_context_name(ref)
    ctx = router.contexts.get(name)
    provider, model = ctx.provider_model
    override_provider, override_model = router.model_override(ref.chat_id)
    override_effort = router.effort_override(ref.chat_id)
    session_id = router.current_session_id(ref)
    running = turns.get(ref.chat_id, ref.thread_id) is not None
    queued = turns.queue_len(ref.chat_id, ref.thread_id)
    effective_model = _format_model(override_provider or provider, override_model or model)

    lines = [
        f"Context: {name}",
        f"Backend: {config.agent_backend}",
        f"Directory: {ctx.directory}",
        f"Model: {effective_model}",
        f"Effort: {override_effort or ctx.effort or '(server default)'}",
        f"Session: {session_id or '(none yet — send a message to start)'}",
        f"Turn: {'running' if running else 'idle'}",
        f"Queued: {queued}",
    ]
    await message.reply_text("\n".join(lines))


def _format_model(provider: str | None, model: str | None) -> str:
    if provider and model:
        return f"{provider}/{model}"
    return "(server default)"


async def handle_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/model [provider/model|reset]`` — inspect or set the chat-wide model.

    Reading works anywhere; **setting** is General-only, because the override is
    global rather than per topic (:data:`balam.router._GLOBAL_THREAD`). Keeping it
    out of topics is what lets a long-lived agent session keep one model for its
    whole life instead of having to switch mid-session.
    """
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
    )
    args = context.args or []

    if not args:
        name = router.current_context_name(ref)
        provider, model = router.contexts.get(name).provider_model
        override_provider, override_model = router.model_override(ref.chat_id)
        source = (
            "global override"
            if override_model
            else "context default"
            if model
            else "server default"
        )
        await message.reply_text(
            f"Model: {_format_model(override_provider or provider, override_model or model)}\n"
            f"Source: {source}\n"
            "Set with /model <provider/model> in General, reset with /model reset."
        )
        return

    if not is_forum_general_message(message):
        await message.reply_text(
            "The model is set for the whole workspace, so change it from General."
        )
        return

    value = args[0].strip()
    if value.lower() == "reset":
        router.reset_model_override(ref.chat_id)
        name = router.current_context_name(ref)
        provider, model = router.contexts.get(name).provider_model
        await message.reply_text(f"Model reset to {_format_model(provider, model)}.")
        return

    try:
        provider, model = split_provider_model(value)
    except ValueError as exc:
        await message.reply_text(f"{exc}\nUsage: /model <provider/model> or /model reset")
        return
    if not provider or not model:
        await message.reply_text("Usage: /model <provider/model> or /model reset")
        return

    router.set_model_override(ref.chat_id, provider, model)
    await message.reply_text(f"Model set to {provider}/{model} for every topic.")


async def handle_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/effort [level|reset]`` — inspect or set the chat-wide effort.

    General-only to set, for the same reason as :func:`handle_model`. Effort in
    particular cannot be changed on a live SDK client at all, so making it global
    removes the only case that would force a session to be rebuilt mid-life.
    """
    message = update.message
    if message is None:
        return

    router: Router = context.application.bot_data["router"]
    ref = TopicRef(
        chat_id=message.chat_id,
        thread_id=message.message_thread_id,
        title=topic_title(message, message.message_thread_id),
    )
    args = context.args or []

    if not args:
        name = router.current_context_name(ref)
        ctx = router.contexts.get(name)
        override = router.effort_override(ref.chat_id)
        source = (
            "global override" if override else "context default" if ctx.effort else "server default"
        )
        await message.reply_text(
            f"Effort: {override or ctx.effort or '(server default)'}\n"
            f"Source: {source}\n"
            "Set with /effort <level> in General, reset with /effort reset."
        )
        return

    if not is_forum_general_message(message):
        await message.reply_text(
            "Effort is set for the whole workspace, so change it from General."
        )
        return

    value = args[0].strip().lower()
    if value == "reset":
        router.reset_effort_override(ref.chat_id)
        name = router.current_context_name(ref)
        ctx = router.contexts.get(name)
        await message.reply_text(f"Effort reset to {ctx.effort or '(server default)'}.")
        return

    if value not in EFFORT_LEVELS:
        allowed = ", ".join(sorted(EFFORT_LEVELS))
        await message.reply_text(f"Unknown effort {value!r}. Available: {allowed}")
        return

    router.set_effort_override(ref.chat_id, value)
    await message.reply_text(f"Effort override set to {value}.")


async def handle_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/rename <name>`` — rename the current forum topic."""
    message = update.message
    if message is None:
        return

    new_name = " ".join(context.args or []).strip()
    if not new_name:
        await message.reply_text("Usage: /rename <topic name>")
        return
    if len(new_name) > 128:
        await message.reply_text("Topic names must be 128 characters or fewer.")
        return
    if message.message_thread_id is None:
        await message.reply_text("Use /rename inside the topic you want to rename.")
        return

    try:
        await rename_forum_topic(context.bot, message.chat_id, message.message_thread_id, new_name)
    except Exception as exc:
        logger.exception("failed to rename forum topic")
        await message.reply_text(f"⚠️ Couldn't rename this topic: {exc}")
        return

    router: Router = context.application.bot_data["router"]
    router.mark_topic_auto_named(
        TopicRef(
            chat_id=message.chat_id,
            thread_id=message.message_thread_id,
            title=topic_title(message, message.message_thread_id),
        )
    )
    router.set_topic_title(message.chat_id, message.message_thread_id, new_name)
    await message.reply_text(f"Renamed topic to {new_name}.")


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/cancel`` — abort the turn running in the current topic, if any."""
    message = update.message
    if message is None:
        return

    backend: AgentBackend = context.application.bot_data["backend"]
    turns: TurnRegistry = context.application.bot_data["turns"]

    turn = turns.get(message.chat_id, message.message_thread_id)
    # Drop anything queued behind the turn too — otherwise it would auto-run right
    # after the cancelled turn settles, which is not what /cancel means.
    dropped = turns.clear_queue(message.chat_id, message.message_thread_id)
    if turn is None:
        if dropped:
            await message.reply_text(
                f"🛑 Cleared {dropped} queued message(s); no turn was running."
            )
        else:
            await message.reply_text("No running turn.")
        return

    tasks: set[asyncio.Task[None]] = context.application.bot_data.setdefault(
        "background_tasks", set()
    )
    abort_turn(turn, backend, tasks)
    if dropped:
        await message.reply_text(f"🛑 Cancelled. Also cleared {dropped} queued message(s).")
    else:
        await message.reply_text("🛑 Cancelled.")


#: Per approval choice: ``(inline note appended to the prompt — already
#: MarkdownV2-escaped, toast shown on the callback answer)``.
