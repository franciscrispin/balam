"""``/schedule`` — run a prompt on a timer (ADR-0016).

The command surface only: parsing what the owner typed, listing and formatting
existing schedules, and the inline picker for cancelling them. The timer itself
— registration on PTB's JobQueue, the fire path, and boot catch-up — lives in
:mod:`balam.schedules`, which this module drives but does not reimplement.

A scheduled run opens a *new* topic each time (:func:`~balam.topics.open_topic_in_context`),
so it never collides with a turn already running somewhere.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from balam import schedules
from balam.approvals import PendingPicks
from balam.auth import callback_authorized
from balam.config import Config
from balam.message_text import command_remainder
from balam.pickers import (
    SCHEDULE_PICKER,
    handle_picker_page,
    handle_picker_toggle,
    picker_markup,
)
from balam.router import Router
from balam.schedules import describe
from balam.store import SessionStore
from balam.telegram_utils import clear_keyboard

logger = logging.getLogger(__name__)


#: Words that lead a ``/schedule`` sub-command rather than a recurrence. A create
#: always starts with a when-token (``daily`` / ``weekdays`` / a weekday), so the
#: two can never be confused.
_SCHEDULE_SUBCOMMANDS = ("cancel", "run", "on", "off", "retime")

_SCHEDULE_USAGE = (
    "Usage:\n"
    "/schedule — list\n"
    "/schedule daily 07:30 <context> <prompt> — create\n"
    "/schedule cancel — pick schedules to remove\n"
    "/schedule run <id> — fire one now\n"
    "/schedule retime <id> <HH:MM> — change the time\n"
    "/schedule off <id> · /schedule on <id> — pause / resume"
)


def _schedule_label(schedule: schedules.Schedule) -> str:
    """Button label for the /schedule cancel picker."""
    base = f"#{schedule.id} {describe(schedule.when)} · {schedule.context}"
    return base if len(base) <= 48 else base[:47] + "…"


def _format_schedule(schedule: schedules.Schedule, tz: ZoneInfo) -> str:
    """One schedule as three list lines: when/where, prompt, last run."""
    state = "" if schedule.enabled else " ⏸ paused"
    lines = [
        f"#{schedule.id} · {describe(schedule.when)} · {schedule.context}{state}",
        f"   {schedules.summarize(schedule.prompt, 120)}",
    ]
    if schedule.last_run_at is not None:
        last = datetime.fromtimestamp(schedule.last_run_at / 1000, tz)
        lines.append(f"   last run {last:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


def _schedule_timezone(context: ContextTypes.DEFAULT_TYPE) -> ZoneInfo:
    """The configured schedule timezone; UTC when no config is wired (tests)."""
    config: Config | None = context.application.bot_data.get("config")
    tz = getattr(config, "timezone", None)
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo("UTC")


async def handle_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/schedule`` — run a prompt on a timer (ADR-0016).

    Bare, it lists this chat's schedules. ``/schedule <when> <context> <prompt>``
    saves a new one; ``cancel`` / ``run`` / ``on`` / ``off`` / ``retime`` manage
    the existing ones. See :data:`_SCHEDULE_USAGE` for the full surface and
    :func:`balam.schedules.parse_when` for the ``<when>`` grammar.
    """
    message = update.message
    if message is None:
        return
    args = context.args or []
    if not args:
        await _schedule_list(message, context)
        return

    head = args[0].strip().lower()
    if head == "cancel":
        await _schedule_cancel(message, context)
        return
    if head in ("run", "on", "off"):
        await _schedule_by_id(message, context, head, args[1:])
        return
    if head == "retime":
        await _schedule_retime(message, context, args[1:])
        return
    await _schedule_create(message, context, args)


async def _schedule_list(message: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: SessionStore = context.application.bot_data["store"]
    rows = store.list_schedules(message.chat_id)
    if not rows:
        await message.reply_text(f"No schedules yet.\n\n{_SCHEDULE_USAGE}")
        return
    tz = _schedule_timezone(context)
    body = "\n".join(_format_schedule(schedules.Schedule.from_row(row), tz) for row in rows)
    await message.reply_text(f"🗓 Schedules (times in {tz.key}):\n{body}\n\n{_SCHEDULE_USAGE}")


async def _schedule_create(
    message: Any, context: ContextTypes.DEFAULT_TYPE, args: list[str]
) -> None:
    router: Router = context.application.bot_data["router"]
    store: SessionStore = context.application.bot_data["store"]

    try:
        when = schedules.parse_when(args)
    except schedules.ScheduleError as exc:
        await message.reply_text(str(exc))
        return

    if len(args) < 3:
        await message.reply_text(f"I also need a context and a prompt.\n\n{_SCHEDULE_USAGE}")
        return
    name = router.contexts.match_name(args[2])
    if name is None:
        available = ", ".join(sorted(router.contexts.contexts))
        await message.reply_text(f"Unknown context {args[2]!r}. Available: {available}")
        return

    # The prompt comes from the raw text, not context.args: that is a whitespace
    # split and would collapse a multi-line prompt onto one line.
    prompt = command_remainder(message.text or "", args_consumed=3)
    if not prompt:
        await message.reply_text(f"I need a prompt to run.\n\n{_SCHEDULE_USAGE}")
        return

    schedule_id = store.add_schedule(
        chat_id=message.chat_id,
        context=name,
        prompt=prompt,
        kind=when.kind,
        hour=when.hour,
        minute=when.minute,
        days=when.days_csv,
        created_at=int(time.time() * 1000),
    )
    tz = _schedule_timezone(context)
    job_queue = getattr(context.application, "job_queue", None)
    if job_queue is None:
        # Saved but inert. Say so rather than implying a timer exists — a silent
        # None here is exactly the failure ADR-0016 calls out.
        await message.reply_text(
            f"⚠️ Saved schedule #{schedule_id}, but the job queue is unavailable, so it "
            "will not fire until Balam restarts with python-telegram-bot[job-queue]."
        )
        return
    row = store.get_schedule(schedule_id)
    assert row is not None  # just inserted
    schedules.register_one(job_queue, schedules.Schedule.from_row(row), tz)
    await message.reply_text(
        f"🗓 Schedule #{schedule_id} saved — {describe(when)} ({tz.key}) in {name}.\n"
        f"{schedules.summarize(prompt, 120)}\n\n"
        f"Test it now with /schedule run {schedule_id}."
    )


async def _schedule_by_id(
    message: Any, context: ContextTypes.DEFAULT_TYPE, action: str, rest: list[str]
) -> None:
    """``/schedule run|on|off <id>``."""
    store: SessionStore = context.application.bot_data["store"]
    if not rest:
        await message.reply_text(f"Which schedule? /schedule {action} <id>")
        return
    try:
        schedule_id = int(rest[0].lstrip("#"))
    except ValueError:
        await message.reply_text(f"{rest[0]!r} isn't a schedule id. /schedule {action} <id>")
        return
    row = store.get_schedule(schedule_id)
    if row is None or row.chat_id != message.chat_id:
        await message.reply_text(f"No schedule #{schedule_id} here.")
        return
    schedule = schedules.Schedule.from_row(row)
    tz = _schedule_timezone(context)
    job_queue = getattr(context.application, "job_queue", None)

    if action == "run":
        # The whole point of this sub-command: a 24-hour feedback loop becomes a
        # 5-second one. It runs unattended, exactly as the timer would.
        await message.reply_text(f"▶️ Running schedule #{schedule_id} now…")
        await schedules.run_schedule(context, schedule)
        return

    enable = action == "on"
    if schedule.enabled == enable:
        await message.reply_text(f"Schedule #{schedule_id} is already {'on' if enable else 'off'}.")
        return
    store.set_schedule_enabled(schedule_id, enable)
    if job_queue is not None:
        if enable:
            schedules.register_one(job_queue, replace(schedule, enabled=True), tz)
        else:
            schedules.unregister(job_queue, schedule_id)
    await message.reply_text(
        f"{'▶️ Resumed' if enable else '⏸ Paused'} schedule #{schedule_id} — "
        f"{describe(schedule.when)}."
    )


async def _schedule_retime(
    message: Any, context: ContextTypes.DEFAULT_TYPE, rest: list[str]
) -> None:
    """``/schedule retime <id> <HH:MM>`` — move a schedule to a new time.

    One step instead of the off/on dance: the row is updated *and* the live
    timer re-registered, keeping the recurrence, prompt, context and id. Editing
    the row alone would leave the old timer firing until the next boot —
    :func:`balam.schedules.register_all` only runs at startup.
    """
    store: SessionStore = context.application.bot_data["store"]
    if len(rest) < 2:
        await message.reply_text("Usage: /schedule retime <id> <HH:MM>")
        return
    try:
        schedule_id = int(rest[0].lstrip("#"))
    except ValueError:
        await message.reply_text(f"{rest[0]!r} isn't a schedule id. /schedule retime <id> <HH:MM>")
        return
    row = store.get_schedule(schedule_id)
    if row is None or row.chat_id != message.chat_id:
        await message.reply_text(f"No schedule #{schedule_id} here.")
        return
    try:
        hour, minute = schedules.parse_time(rest[1])
    except schedules.ScheduleError as exc:
        await message.reply_text(str(exc))
        return

    store.set_schedule_time(schedule_id, hour, minute)
    row = store.get_schedule(schedule_id)
    assert row is not None  # just updated
    schedule = schedules.Schedule.from_row(row)
    tz = _schedule_timezone(context)

    job_queue = getattr(context.application, "job_queue", None)
    if job_queue is None:
        if schedule.enabled:
            await message.reply_text(
                f"⚠️ Saved the new time for #{schedule_id}, but the job queue is unavailable, "
                "so the running timer is unchanged until Balam restarts with "
                "python-telegram-bot[job-queue]."
            )
            return
    else:
        # register_one replaces the old timer, and is a no-op arm for a paused
        # schedule — the new time then applies on /schedule on.
        schedules.register_one(job_queue, schedule, tz)
    paused = "" if schedule.enabled else " (still ⏸ paused)"
    await message.reply_text(
        f"🗓 Schedule #{schedule_id} retimed — {describe(schedule.when)} ({tz.key}){paused}."
    )


async def _schedule_cancel(message: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/schedule cancel`` — the paged multi-select picker, mirroring /delete."""
    store: SessionStore = context.application.bot_data["store"]
    rows = store.list_schedules(message.chat_id)
    if not rows:
        await message.reply_text("No schedules to cancel.")
        return

    picks: PendingPicks = context.application.bot_data["pending_schedule_picks"]
    token = picks.register(
        message.chat_id,
        [(row.id, _schedule_label(schedules.Schedule.from_row(row))) for row in rows],
    )
    text = "🗑 Select schedules to cancel, then tap “Cancel selected”."
    info = picks.page_info(token)
    if info and info[1] > 1:
        text += (
            f"\n\n{info[2]} schedules across {info[1]} pages — use ◀ ▶ to browse. "
            "Selections persist across pages."
        )
    await message.reply_text(text, reply_markup=picker_markup(SCHEDULE_PICKER, picks, token))


async def handle_schedule_toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Toggle a schedule's checkbox in the picker (``sch:<token>:<id>``)."""
    await handle_picker_toggle(update, context, SCHEDULE_PICKER, "pending_schedule_picks")


async def handle_schedule_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Flip the /schedule cancel picker to another page (``schp:<token>:<page>``)."""
    await handle_picker_page(update, context, SCHEDULE_PICKER, "pending_schedule_picks")


async def handle_schedule_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete the schedules selected in the picker (``schd:<token>``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("schd:"):
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

    picks: PendingPicks = context.application.bot_data["pending_schedule_picks"]
    schedule_ids = picks.selected_ids(token)
    if schedule_ids is None:
        await query.answer("This picker has expired.")
        await clear_keyboard(query)
        return
    if not schedule_ids:
        await query.answer("Select at least one schedule.")
        return
    picks.discard(token)

    store: SessionStore = context.application.bot_data["store"]
    job_queue = getattr(context.application, "job_queue", None)
    cancelled = 0
    for schedule_id in schedule_ids:
        # Drop the timer first: a row that survives a failed delete still has no
        # job, which is the safe direction (nothing fires unannounced).
        if job_queue is not None:
            schedules.unregister(job_queue, schedule_id)
        if store.delete_schedule(schedule_id):
            cancelled += 1

    await query.answer(f"Cancelled {cancelled} schedule(s).")
    message = getattr(query, "message", None)
    if message is not None:
        try:
            await message.edit_text(text=f"🗑 Cancelled {cancelled} schedule(s).", reply_markup=None)
        except Exception:
            logger.debug("failed to finalize schedule cancel message", exc_info=True)


async def handle_schedule_dismiss_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Dismiss the /schedule cancel picker, cancelling nothing (``schx:<token>``)."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith("schx:"):
        return
    config: Config = context.application.bot_data["config"]
    if not callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 1)
    if len(parts) == 2:
        context.application.bot_data["pending_schedule_picks"].discard(parts[1])
    await query.answer("Dismissed.")
    message = getattr(query, "message", None)
    if message is not None:
        try:
            await message.edit_text(text="🗑 Nothing cancelled.", reply_markup=None)
        except Exception:
            logger.debug("failed to finalize schedule dismiss message", exc_info=True)
