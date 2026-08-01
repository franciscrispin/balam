"""The Telegram bot: the system's trust boundary (ADR-0008).

Two responsibilities for this slice:
  1. Allowlist — accept updates only from the single owner's numeric user ID;
     everyone else is silently ignored (a stranger's update matches no handler).
  2. Route messages — map the topic to its OpenCode session (ADR-0009), forward
     text plus any image/document attachments (§4), and stream the agent's reply
     back into the same topic.
  3. Handle ``/context`` — list workspaces, or open a new topic bound to one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from balam.agent.backend import AgentBackend
from balam.approvals import (
    PendingApprovals,
    PendingPicks,
    PendingQuestions,
)
from balam.attachments import collect_attachments
from balam.auth import callback_authorized
from balam.callbacks import (
    handle_approval_callback,
    handle_question_callback,
    handle_question_custom_callback,
    handle_question_done_callback,
    show_custom_answer_on_question,
)
from balam.commands.schedule import (
    handle_schedule,
    handle_schedule_confirm_callback,
    handle_schedule_dismiss_callback,
    handle_schedule_page_callback,
    handle_schedule_toggle_callback,
)
from balam.config import Config
from balam.contexts import EFFORT_LEVELS, split_provider_model
from balam.message_text import (
    command_remainder,
    forward_reply_prefix,
    forwarded_slash_command,
    strip_bot_mention_from_command,
)
from balam.miniapp import mini_app_reply
from balam.pickers import (
    DELETE_PICKER,
    handle_picker_page,
    handle_picker_toggle,
    picker_markup,
)
from balam.router import Router, TopicRef
from balam.store import SessionStore
from balam.telegram_utils import clear_keyboard, thread_kwargs
from balam.topics import (
    create_topic_from_general,
    is_forum_general_message,
    open_context_topic,
    rename_forum_topic,
    topic_label,
    topic_title,
)
from balam.turns import TurnRegistry, abort_turn, notify_error, submit_turn

logger = logging.getLogger(__name__)


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    chat_id = message.chat_id
    thread_id = message.message_thread_id
    is_slash_command = forwarded_slash_command(message)
    text = message.text or message.caption or ""
    # An unregistered slash command (e.g. ``/goal``) reaches us via the catch-all
    # handler so it can pass through to the agent as a Claude slash command. Telegram
    # may address it as ``/goal@thisbot`` in groups; strip our own @-mention from the
    # leading command token so the agent sees a clean ``/goal``.
    text = strip_bot_mention_from_command(text, getattr(context.bot, "username", None))

    pending_questions: PendingQuestions | None = context.application.bot_data.get(
        "pending_questions"
    )
    if text and pending_questions is not None:
        custom_result = pending_questions.resolve_custom(chat_id, thread_id, text)
        if custom_result is not None and custom_result.status == "resolved":
            await show_custom_answer_on_question(context.bot, chat_id, custom_result)
            await message.reply_text("✅ Answer sent.")
            return
        if custom_result is not None and custom_result.status == "added":
            await message.reply_text("✅ Custom answer added. Select more options or tap Done.")
            return

    # Download any image/document attachments as native file parts (tier-1 plan §4);
    # the text is the message text or an attachment's caption.
    try:
        files = await collect_attachments(message, context.bot)
    except Exception as exc:
        logger.exception("failed to download attachment")
        await notify_error(context.bot, chat_id, thread_id, exc)
        return
    if not text and not files:
        return

    router: Router = context.application.bot_data["router"]

    if is_forum_general_message(message):
        created_thread_id = await create_topic_from_general(
            message,
            context.bot,
            router,
            text,
            has_files=bool(files),
        )
        if created_thread_id is None:
            return
        thread_id = created_thread_id

    # Show a deterministic marker so the owner can see a slash command was routed to
    # the agent as a command (vs. treated as plain text). Expansion itself is invisible
    # in the SDK stream, but this fires exactly when Balam forwards a command, and the
    # agent's own ``Unknown command: /x`` reply closes the loop for a bad command.
    if is_slash_command:
        command = text.split(maxsplit=1)[0]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⌘ forwarding slash command {command} to the agent",
            **thread_kwargs(thread_id),
        )

    # Surface the owner's forward/reply gestures to the agent as a bracketed header
    # on the prompt (Telegram drops that metadata otherwise). Never for a slash
    # command — the agent expands ``/goal`` only when it leads the prompt.
    prompt_prefix = "" if is_slash_command else forward_reply_prefix(message)
    await submit_turn(
        message, context, text, files, thread_id=thread_id, prompt_prefix=prompt_prefix
    )


async def _handle_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
_ARTIFACT_SCOPES = ("mine", "shared", "all")


async def _handle_artifacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/effort [level|reset]`` — inspect or set the chat-wide effort.

    General-only to set, for the same reason as :func:`_handle_model`. Effort in
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


async def _handle_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_browser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
async def _handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_delete_toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Toggle a topic's checkbox in the /delete picker (``del:<token>:<thread_id>``)."""
    await handle_picker_toggle(update, context, DELETE_PICKER, "pending_deletions")


async def _handle_delete_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip the /delete picker to another page (``delp:<token>:<page>``)."""
    await handle_picker_page(update, context, DELETE_PICKER, "pending_deletions")


async def _handle_delete_confirm_callback(
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


async def _handle_delete_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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


async def _handle_topic_edited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sync the stored title when a topic is renamed from the Telegram UI.

    Every other title change is set by the bot itself; this service-message update
    is the one path it doesn't originate, so the /delete picker would otherwise show
    a stale name."""
    message = update.message
    if message is None or message.message_thread_id is None:
        return
    edited = message.forum_topic_edited
    if edited is None or not edited.name:
        return
    router: Router = context.application.bot_data["router"]
    router.set_topic_title(message.chat_id, message.message_thread_id, edited.name)


BOT_COMMANDS = [
    BotCommand("new", "Open a new topic: /new [context] [first message]"),
    BotCommand("rename", "Rename the current topic"),
    BotCommand("status", "Show this topic's context, session, and turn state"),
    BotCommand("model", "Show or set this topic's model override"),
    BotCommand("effort", "Show or set this topic's effort override"),
    BotCommand("cancel", "Abort the turn currently running in this topic"),
    BotCommand("context", "List contexts, or /context <name> [first message] to open a topic"),
    BotCommand("diff", "Open the Mini App git diff viewer for this topic's context"),
    BotCommand("browser", "Watch the agent's live browser (Mini App)"),
    BotCommand("artifacts", "List your published claude.ai artifacts (/artifacts [shared|all])"),
    BotCommand("delete", "Delete topics — pick which ones to remove"),
    BotCommand("schedule", "Run a prompt on a schedule: /schedule daily 07:30 <context> <prompt>"),
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
    backend: AgentBackend,
    router: Router,
    *,
    store: SessionStore | None = None,
    post_init: Any = None,
    post_shutdown: Any = None,
) -> Application:
    # Live-edit streaming, the todo checklist, and command handlers all share
    # one per-group flood budget (~20 messages/min). The limiter paces calls
    # under Telegram's limits and sleeps+retries on RetryAfter, so a burst
    # can't 429 a turn's final answer into the void.
    builder = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=3))
    )
    if post_init is not None:
        builder = builder.post_init(post_init)
    if post_shutdown is not None:
        builder = builder.post_shutdown(post_shutdown)
    app = builder.build()

    app.bot_data["config"] = config
    app.bot_data["backend"] = backend
    app.bot_data["router"] = router
    # The SQLite store, for the one caller that isn't topic→session routing:
    # /schedule reads and writes the schedules table directly (ADR-0016).
    app.bot_data["store"] = store
    # In-flight turns, keyed by topic, so /cancel can interrupt a running reply.
    app.bot_data["turns"] = TurnRegistry()
    # Outstanding tool-approval prompts + per-session "accept all edits" state.
    app.bot_data["pending"] = PendingApprovals()
    # Outstanding OpenCode question-tool prompts.
    app.bot_data["pending_questions"] = PendingQuestions()
    # Outstanding picker selections: /delete over topics, /schedule cancel over
    # schedules. Same class, one snapshot each (see PendingPicks).
    app.bot_data["pending_deletions"] = PendingPicks()
    app.bot_data["pending_schedule_picks"] = PendingPicks()
    # Anchors fire-and-forget background tasks (e.g. /cancel's server-side abort)
    # so the loop's weak task references can't let them be GC'd mid-flight.
    app.bot_data["background_tasks"] = set()

    # Trust boundary (ADR-0008): filters.User gates by sender id, so only the
    # owner's messages reach the handlers; everyone else is dropped silently.
    # When a target chat is configured (ADR-0010), additionally require that
    # chat, so the bot acts only inside the workspace supergroup. Unset → the
    # legacy owner-anywhere behavior, preserving the DM round-trip.
    allowed = filters.User(user_id=config.allowed_telegram_user_id)
    if config.allowed_telegram_chat_id is not None:
        allowed = allowed & filters.Chat(chat_id=config.allowed_telegram_chat_id)

    app.add_handler(CommandHandler("new", _handle_new, filters=allowed))
    app.add_handler(CommandHandler("rename", _handle_rename, filters=allowed))
    app.add_handler(CommandHandler("status", _handle_status, filters=allowed))
    app.add_handler(CommandHandler("model", _handle_model, filters=allowed))
    app.add_handler(CommandHandler("effort", _handle_effort, filters=allowed))
    app.add_handler(CommandHandler("cancel", _handle_cancel, filters=allowed))
    app.add_handler(CommandHandler("context", _handle_context, filters=allowed))
    app.add_handler(CommandHandler("diff", _handle_diff, filters=allowed))
    app.add_handler(CommandHandler("browser", _handle_browser, filters=allowed))
    app.add_handler(CommandHandler("artifacts", _handle_artifacts, filters=allowed))
    app.add_handler(CommandHandler("delete", _handle_delete, filters=allowed))
    app.add_handler(CommandHandler("schedule", handle_schedule, filters=allowed))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND & allowed,
            _handle_message,
        )
    )
    # Catch-all for *unregistered* slash commands. The specific CommandHandlers above
    # match first (PTB stops at the first handler in a group), so only commands Balam
    # doesn't own — e.g. a Claude slash command like ``/goal`` — fall through here and
    # get forwarded verbatim to the agent instead of being silently dropped.
    app.add_handler(MessageHandler(filters.COMMAND & allowed, _handle_message))
    # Keep the stored title in step with renames done from the Telegram UI (the one
    # title change the bot doesn't itself originate). A service message matches
    # neither commands nor the text/photo handler above, so it falls through here.
    app.add_handler(
        MessageHandler(filters.StatusUpdate.FORUM_TOPIC_EDITED & allowed, _handle_topic_edited)
    )
    # CallbackQueryHandler takes no filter; the handler re-checks the trust
    # boundary (ADR-0008) itself before resolving an approval.
    app.add_handler(CallbackQueryHandler(handle_approval_callback, pattern=r"^appr:"))
    app.add_handler(CallbackQueryHandler(handle_question_done_callback, pattern=r"^qstd:"))
    app.add_handler(CallbackQueryHandler(handle_question_callback, pattern=r"^qst:"))
    app.add_handler(CallbackQueryHandler(handle_question_custom_callback, pattern=r"^qstc:"))
    app.add_handler(CallbackQueryHandler(_handle_delete_confirm_callback, pattern=r"^deld:"))
    app.add_handler(CallbackQueryHandler(_handle_delete_cancel_callback, pattern=r"^delx:"))
    app.add_handler(CallbackQueryHandler(_handle_delete_page_callback, pattern=r"^delp:"))
    app.add_handler(CallbackQueryHandler(_handle_delete_toggle_callback, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(handle_schedule_confirm_callback, pattern=r"^schd:"))
    app.add_handler(CallbackQueryHandler(handle_schedule_dismiss_callback, pattern=r"^schx:"))
    app.add_handler(CallbackQueryHandler(handle_schedule_page_callback, pattern=r"^schp:"))
    app.add_handler(CallbackQueryHandler(handle_schedule_toggle_callback, pattern=r"^sch:"))

    return app
