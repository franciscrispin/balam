"""Scheduled prompts: saved ``(when, context, prompt)`` triples that run
themselves on a timer (ADR-0016).

A schedule fires by doing exactly what ``/new <context> <prompt>`` does: open a
fresh forum topic bound to that context and run the prompt in it. The owner wakes
up to a topic holding the answer and replies in it to keep working. One topic per
fire is deliberate — it keeps each run's session history separate, which is
ADR-0009's reasoning applied to time.

A **headless** schedule opens no topic at all (:mod:`balam.headless`). It is for
jobs whose result lands somewhere else, like a sync that writes a sheet and a log
another schedule reports. The chat hears about a headless run only when it
fails, or when its answer starts with ``ALERT:``, in General, so a broken job is
never silent and a check can still raise what it found.

Schedules are **user data**, not infrastructure: they live in SQLite
(:class:`balam.store.SessionStore`) and are created, listed and cancelled from
the phone with ``/schedule``. Contexts stay in ``config.yaml`` (ADR-0012) because
a directory and a tool policy are infrastructure; a 7am message you want to stop
should not need an SSH session.

Three things here are load-bearing and easy to get wrong:

* **Weekday numbering.** We store and reason in Python's
  :meth:`datetime.date.weekday` numbering (``Mon=0`` … ``Sun=6``). PTB's
  ``run_daily(days=…)`` uses ``Sun=0`` … ``Sat=6``. :func:`_ptb_days` is the one
  place that converts, so nothing else has to remember.
* **Timezone.** The VM runs UTC and the owner does not, so every time is
  resolved against :attr:`balam.config.Config.timezone` (``BALAM_TIMEZONE``),
  never the process default.
* **The run stamp is written when the run starts**, not when its turn ends, so a
  crash mid-turn cannot make :func:`catch_up` re-fire the whole thing on restart.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from balam.headless import HeadlessResult, run_headless
from balam.store import ScheduleRow, SessionStore
from balam.topics import TopicOpenError, open_topic_in_context
from balam.turns import start_prompt

logger = logging.getLogger(__name__)

#: Schedule kinds, as stored in ``schedules.kind``.
KIND_DAILY = "daily"
KIND_WEEKDAYS = "weekdays"
KIND_DOW = "dow"
KIND_MONTHLY = "monthly"

#: ``schedules.days`` for a monthly schedule on the last day of the month — PTB's
#: own spelling for it in :meth:`telegram.ext.JobQueue.run_monthly`.
LAST_DAY = -1

#: The highest fixed day of the month accepted. PTB *skips* a month that has no
#: such day, so "monthly 31" would silently not run in Feb, Apr, Jun, Sep or Nov;
#: past 28 the owner is pointed at ``last`` instead.
MAX_MONTH_DAY = 28

#: Schedule run modes, as stored in ``schedules.mode``.
MODE_TOPIC = "topic"
MODE_HEADLESS = "headless"

#: How much of a headless run's answer a report quotes.
_REPORT_TAIL = 800

#: Monday–Friday in Python's weekday numbering.
WORKWEEK = (0, 1, 2, 3, 4)

#: Weekday tokens accepted by :func:`parse_when`, in Python's numbering.
_WEEKDAY_TOKENS: dict[str, int] = {}
for _index, (_short, _long) in enumerate(
    [
        ("mon", "monday"),
        ("tue", "tuesday"),
        ("wed", "wednesday"),
        ("thu", "thursday"),
        ("fri", "friday"),
        ("sat", "saturday"),
        ("sun", "sunday"),
    ]
):
    _WEEKDAY_TOKENS[_short] = _index
    _WEEKDAY_TOKENS[_long] = _index
# Two spellings people actually type that aren't a prefix of the long form.
_WEEKDAY_TOKENS["tues"] = 1
_WEEKDAY_TOKENS["thur"] = 3
_WEEKDAY_TOKENS["thurs"] = 3

#: Display names, indexed by Python weekday number.
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

#: How late a missed run may still be worth doing (:func:`catch_up`). Beyond
#: this it is noise, not information — a 3-day-old daily brief tells you nothing,
#: and a VM that was off for a week would otherwise produce seven topics at boot.
CATCH_UP_WINDOW = dt.timedelta(hours=6)

#: The accepted forms, quoted back verbatim when parsing fails. A grammar dump
#: would be worse than four examples.
WHEN_EXAMPLES = (
    "/schedule daily 07:30 <context> <prompt>",
    "/schedule weekdays 09:00 <context> <prompt>",
    "/schedule fri 17:00 <context> <prompt>",
    "/schedule monthly 1 09:00 <context> <prompt> (day 1-28 or last)",
)


class ScheduleError(ValueError):
    """A ``/schedule`` argument the owner has to fix. The message is owner-facing."""


@dataclass(frozen=True)
class When:
    """A parsed schedule time: what kind of recurrence, at what hour/minute, on
    which weekdays (Python numbering; empty for daily and monthly), or on which
    day of the month (monthly only; :data:`LAST_DAY` for the last)."""

    kind: str
    hour: int
    minute: int
    days: tuple[int, ...] = ()
    month_day: int | None = None

    @property
    def days_csv(self) -> str | None:
        """The ``schedules.days`` column value for this recurrence. A monthly
        schedule stores its day of the month there — the column is otherwise
        unused by that kind, and reusing it needs no migration."""
        if self.month_day is not None:
            return str(self.month_day)
        return ",".join(str(d) for d in self.days) if self.days else None

    @property
    def token_count(self) -> int:
        """How many ``/schedule`` arguments this recurrence was spelled with, so
        the caller knows where the context name starts."""
        return 3 if self.kind == KIND_MONTHLY else 2

    @property
    def at(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


@dataclass(frozen=True)
class Schedule:
    """A stored schedule, parsed. See :class:`balam.store.ScheduleRow` for the raw
    row this comes from."""

    id: int
    chat_id: int
    context: str
    prompt: str
    kind: str
    hour: int
    minute: int
    days: tuple[int, ...]
    enabled: bool
    created_at: int
    last_run_at: int | None
    mode: str = MODE_TOPIC
    month_day: int | None = None

    @property
    def headless(self) -> bool:
        return self.mode == MODE_HEADLESS

    @classmethod
    def from_row(cls, row: ScheduleRow) -> Schedule:
        days = tuple(int(part) for part in row.days.split(",") if part.strip()) if row.days else ()
        month_day = None
        if row.kind == KIND_MONTHLY:
            # The days column holds the day of the month for this kind.
            (month_day,), days = days, ()
        return cls(
            id=row.id,
            chat_id=row.chat_id,
            context=row.context,
            prompt=row.prompt,
            kind=row.kind,
            hour=row.hour,
            minute=row.minute,
            days=days,
            enabled=bool(row.enabled),
            created_at=row.created_at,
            last_run_at=row.last_run_at,
            mode=row.mode,
            month_day=month_day,
        )

    @property
    def when(self) -> When:
        return When(
            kind=self.kind,
            hour=self.hour,
            minute=self.minute,
            days=self.days,
            month_day=self.month_day,
        )


def parse_when(tokens: Sequence[str]) -> When:
    """Parse the leading ``/schedule`` tokens into a :class:`When`.

    Four forms, each of which PTB's ``run_daily`` or ``run_monthly`` covers
    directly — which is why there is no cron parser and no APScheduler API
    surface of our own::

        daily 07:30          every day
        weekdays 09:00       Mon–Fri
        fri 17:00            one weekday
        monthly 15 09:00     one day of the month (1-28, or ``last``)

    The monthly form takes three tokens, the others two; :attr:`When.token_count`
    says which. Raises :class:`ScheduleError` with the examples for anything else.
    """
    if len(tokens) < 2:
        raise ScheduleError(_when_help("I need a time, like " + WHEN_EXAMPLES[0] + "."))

    kind_token = tokens[0].strip().lower()
    if kind_token == KIND_MONTHLY:
        if len(tokens) < 3:
            raise ScheduleError(_when_help("I need a day and a time, like monthly 1 09:00."))
        month_day = parse_month_day(tokens[1])
        hour, minute = parse_time(tokens[2])
        return When(kind=KIND_MONTHLY, hour=hour, minute=minute, month_day=month_day)

    hour, minute = parse_time(tokens[1])

    if kind_token == KIND_DAILY:
        return When(kind=KIND_DAILY, hour=hour, minute=minute)
    if kind_token in (KIND_WEEKDAYS, "weekday"):
        return When(kind=KIND_WEEKDAYS, hour=hour, minute=minute, days=WORKWEEK)
    day = _WEEKDAY_TOKENS.get(kind_token)
    if day is not None:
        return When(kind=KIND_DOW, hour=hour, minute=minute, days=(day,))
    raise ScheduleError(_when_help(f"I don't understand {tokens[0]!r}."))


def parse_time(token: str) -> tuple[int, int]:
    """``HH:MM`` in 24-hour time, as the owner's wall clock in ``BALAM_TIMEZONE``.

    Public because ``/schedule retime`` parses a time on its own, without the
    leading recurrence token :func:`parse_when` requires."""
    hour_text, _, minute_text = token.strip().partition(":")
    if not minute_text:
        raise ScheduleError(_when_help(f"{token!r} isn't a time — use 24-hour HH:MM."))
    try:
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        raise ScheduleError(_when_help(f"{token!r} isn't a time — use 24-hour HH:MM.")) from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(_when_help(f"{token!r} isn't a real time of day."))
    return hour, minute


def parse_month_day(token: str) -> int:
    """A day of the month: ``1``–``28`` (``1st``/``15th`` spellings too), or
    ``last`` → :data:`LAST_DAY`. See :data:`MAX_MONTH_DAY` for why not 29–31."""
    text = token.strip().lower()
    if text == "last":
        return LAST_DAY
    digits = text.removesuffix("st").removesuffix("nd").removesuffix("rd").removesuffix("th")
    try:
        day = int(digits)
    except ValueError:
        raise ScheduleError(_when_help(f"{token!r} isn't a day of the month.")) from None
    if 29 <= day <= 31:
        raise ScheduleError(
            _when_help(
                f"Day {day} doesn't exist in every month, and those months would be "
                "skipped — use last for the end of the month."
            )
        )
    if not 1 <= day <= MAX_MONTH_DAY:
        raise ScheduleError(_when_help(f"{token!r} isn't a day of the month."))
    return day


def _ordinal(day: int) -> str:
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _when_help(problem: str) -> str:
    examples = "\n".join(WHEN_EXAMPLES)
    return f"{problem}\n\nUse one of:\n{examples}"


def describe(when: When) -> str:
    """A one-line human reading of a recurrence, e.g. ``every day at 07:30``."""
    if when.kind == KIND_DAILY:
        return f"every day at {when.at}"
    if when.kind == KIND_WEEKDAYS:
        return f"Mon–Fri at {when.at}"
    if when.kind == KIND_MONTHLY:
        assert when.month_day is not None
        day = "last day" if when.month_day == LAST_DAY else _ordinal(when.month_day)
        return f"monthly on the {day} at {when.at}"
    names = ", ".join(_WEEKDAY_NAMES[d] for d in when.days)
    return f"every {names} at {when.at}"


# --- JobQueue registration -----------------------------------------------------


def job_name(schedule_id: int) -> str:
    """The JobQueue name for a schedule, so :func:`unregister` can find its job
    and a re-registration after an edit can't leave a duplicate behind."""
    return f"schedule:{schedule_id}"


def _ptb_days(days: tuple[int, ...]) -> tuple[int, ...]:
    """Python weekday numbers (``Mon=0`` … ``Sun=6``) → PTB's ``run_daily`` days.

    PTB numbers 0-6 as **Sunday**-Saturday (``JobQueue._CRON_MAPPING``), so the
    two conventions differ by one with a wrap. Getting this wrong shifts every
    weekday schedule by a day, silently.
    """
    return tuple((day + 1) % 7 for day in days)


def register_one(job_queue: Any, schedule: Schedule, tz: ZoneInfo) -> None:
    """(Re)register a schedule's timer, replacing any job it already had."""
    unregister(job_queue, schedule.id)
    if not schedule.enabled:
        return
    when = schedule.when
    if when.kind == KIND_MONTHLY:
        assert when.month_day is not None
        job_queue.run_monthly(
            fire,
            when=dt.time(hour=when.hour, minute=when.minute, tzinfo=tz),
            day=when.month_day,
            data=schedule.id,
            name=job_name(schedule.id),
        )
        return
    job_queue.run_daily(
        fire,
        time=dt.time(hour=when.hour, minute=when.minute, tzinfo=tz),
        days=_ptb_days(when.days) if when.days else tuple(range(7)),
        data=schedule.id,
        name=job_name(schedule.id),
    )


def register_all(job_queue: Any, store: SessionStore, tz: ZoneInfo) -> int:
    """Register every enabled schedule at boot; return how many. The JobQueue is
    in memory, so this runs on every start."""
    count = 0
    for row in store.all_schedules():
        schedule = Schedule.from_row(row)
        if not schedule.enabled:
            continue
        register_one(job_queue, schedule, tz)
        count += 1
    return count


def unregister(job_queue: Any, schedule_id: int) -> int:
    """Remove a schedule's timer; return how many jobs were removed."""
    jobs = job_queue.get_jobs_by_name(job_name(schedule_id))
    for job in jobs:
        job.schedule_removal()
    return len(jobs)


# --- Firing --------------------------------------------------------------------


def previous_due(when: When, now: dt.datetime) -> dt.datetime:
    """The most recent moment this recurrence was due, at or before ``now``.

    ``now`` must be timezone-aware; the result carries the same zone. Walks back
    day by day (at most a week plus a day) rather than doing modular arithmetic,
    because that stays obvious across weekday sets and needs no DST reasoning of
    its own.
    """
    if when.kind == KIND_MONTHLY:
        return _previous_monthly_due(when, now)
    days = set(when.days) if when.days else set(range(7))
    for back in range(8):
        day = (now - dt.timedelta(days=back)).date()
        if day.weekday() not in days:
            continue
        candidate = dt.datetime.combine(
            day, dt.time(hour=when.hour, minute=when.minute), tzinfo=now.tzinfo
        )
        if candidate <= now:
            return candidate
    # Unreachable: with any non-empty day set one of the last 8 days matches and
    # its time-of-day is at or before ``now`` on the earliest of them.
    raise AssertionError(f"no previous due time for {when!r} before {now!r}")


def _previous_monthly_due(when: When, now: dt.datetime) -> dt.datetime:
    """:func:`previous_due` for a monthly recurrence: this month's occurrence if
    it has passed, else last month's."""
    assert when.month_day is not None
    year, month = now.year, now.month
    for _ in range(2):
        last = calendar.monthrange(year, month)[1]
        day = last if when.month_day == LAST_DAY else when.month_day
        candidate = dt.datetime(year, month, day, when.hour, when.minute, tzinfo=now.tzinfo)
        if candidate <= now:
            return candidate
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    raise AssertionError(f"no previous due time for {when!r} before {now!r}")


def _already_ran(schedule: Schedule, due: dt.datetime) -> bool:
    """Whether ``last_run_at`` already covers the occurrence due at ``due``."""
    return schedule.last_run_at is not None and schedule.last_run_at >= int(due.timestamp() * 1000)


def due_catch_up(
    schedule: Schedule, now: dt.datetime, *, window: dt.timedelta = CATCH_UP_WINDOW
) -> dt.datetime | None:
    """The missed occurrence worth running now, or ``None``.

    A run is caught up only if it is in the past, has not already been stamped,
    and is inside ``window``. Outside the window it is skipped — see
    :data:`CATCH_UP_WINDOW`.
    """
    if not schedule.enabled:
        return None
    due = previous_due(schedule.when, now)
    if now - due > window:
        return None
    if _already_ran(schedule, due):
        return None
    return due


async def fire(context: Any) -> None:
    """The JobQueue callback: run the schedule whose id rides ``job.data``."""
    schedule_id = int(context.job.data)
    store: SessionStore = context.application.bot_data["store"]
    row = store.get_schedule(schedule_id)
    if row is None:
        # Deleted while the process was up but the job somehow survived; make sure.
        unregister(context.application.job_queue, schedule_id)
        return
    schedule = Schedule.from_row(row)
    if not schedule.enabled:
        return

    # Guard against the one way this can double-fire: booting within the same
    # minute as a due time, so catch-up and the timer both claim the occurrence.
    tz = _timezone(context)
    due = previous_due(schedule.when, dt.datetime.now(tz))
    if _already_ran(schedule, due):
        logger.info("schedule %s already ran for %s; skipping duplicate fire", schedule.id, due)
        return

    await run_schedule(context, schedule)


async def run_schedule(
    context: Any,
    schedule: Schedule,
    *,
    late_for: dt.datetime | None = None,
    report_thread: int | None = None,
    announce: bool = False,
) -> bool:
    """Open a topic for ``schedule`` and start its prompt there unattended, or,
    for a headless schedule, start it with no topic at all.

    Returns whether a run started. ``late_for`` marks a catch-up run and is the
    due time it missed, noted in the topic so a brief that shows up at 09:12 does
    not read as a 09:12 brief.

    ``report_thread`` and ``announce`` only affect a headless run: where its
    report goes (General when ``None``), and whether a *successful* run is
    reported too. ``/schedule run`` sets both, so a manual test shows its result
    where it was asked for; the timer sets neither, so success stays silent.
    """
    router = context.application.bot_data["router"]
    store: SessionStore = context.application.bot_data["store"]

    if schedule.context not in router.contexts.contexts:
        # A context can vanish from config.yaml under a schedule that names it.
        # Raising here would land in the JobQueue's error handler, where the owner
        # would never see it — so park the schedule and say so where they will.
        store.set_schedule_enabled(schedule.id, False)
        unregister(context.application.job_queue, schedule.id)
        await _notify_chat(
            context.bot,
            schedule.chat_id,
            f"⏸ Schedule #{schedule.id} is parked: its context {schedule.context!r} is no "
            f"longer in config.yaml.\n{describe(schedule.when)} — {summarize(schedule.prompt)}",
        )
        return False

    # Stamp before running, not after (see the module docstring).
    store.mark_schedule_run(schedule.id, int(time.time() * 1000))

    if schedule.headless:
        return await _start_headless(
            context, schedule, late_for=late_for, report_thread=report_thread, announce=announce
        )

    try:
        thread_id, title = await open_topic_in_context(
            context.bot, router, schedule.chat_id, schedule.context, prompt=schedule.prompt
        )
    except TopicOpenError as exc:
        logger.exception("schedule %s could not open its topic", schedule.id)
        await _notify_chat(context.bot, schedule.chat_id, f"Schedule #{schedule.id}: {exc}")
        return False

    if late_for is not None:
        await _notify_chat(
            context.bot,
            schedule.chat_id,
            f"⏱ Late run — this was due at {late_for:%H:%M} and Balam wasn't up then.",
            thread_id=thread_id,
        )

    return await start_prompt(
        context, schedule.chat_id, thread_id, schedule.prompt, title=title, unattended=True
    )


async def _start_headless(
    context: Any,
    schedule: Schedule,
    *,
    late_for: dt.datetime | None,
    report_thread: int | None,
    announce: bool,
) -> bool:
    """Resolve a topic-less session and run the prompt in a background task.

    Returns once the run has *started*, like a topic run: the JobQueue callback
    must not sit on a turn that can take many minutes."""
    router = context.application.bot_data["router"]
    backend = context.application.bot_data["backend"]
    try:
        resolved = await router.resolve_headless(
            schedule.chat_id, schedule.context, f"Schedule #{schedule.id}"
        )
    except Exception as exc:
        logger.exception("schedule %s could not start its headless session", schedule.id)
        await _notify_chat(
            context.bot,
            schedule.chat_id,
            f"⚠️ Headless schedule #{schedule.id} could not start: {exc}",
            thread_id=report_thread,
        )
        return False

    async def finish() -> None:
        result = await run_headless(backend, resolved, schedule.prompt)
        logger.info(
            "headless schedule %s finished ok=%s error=%r blocked=%s",
            schedule.id,
            result.ok,
            result.error,
            result.blocked,
        )
        if result.ok and not result.alert and not announce:
            return
        await _notify_chat(
            context.bot,
            schedule.chat_id,
            headless_report(schedule, result, late_for=late_for),
            thread_id=report_thread,
        )

    # Anchored, because the event loop holds only a weak reference to a task: an
    # unanchored run could be garbage-collected halfway through a sync.
    tasks: set[asyncio.Task[None]] = context.application.bot_data.setdefault(
        "headless_tasks", set()
    )
    task = asyncio.create_task(finish())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


def headless_report(
    schedule: Schedule, result: HeadlessResult, *, late_for: dt.datetime | None = None
) -> str:
    """The one message a headless run posts: always on failure or an alert, on
    plain success only for a manual ``/schedule run``."""
    late = f" (late run, due {late_for:%H:%M})" if late_for is not None else ""
    if result.ok and result.alert:
        lines = [f"🚨 Headless schedule #{schedule.id} raised an alert{late}."]
    elif result.ok:
        lines = [f"✅ Headless schedule #{schedule.id} finished{late}."]
    else:
        lines = [f"⚠️ Headless schedule #{schedule.id} failed{late}: {summarize(schedule.prompt)}"]
        if result.error:
            lines.append(f"Error: {result.error}")
        if result.blocked:
            lines.append("Refused: " + ", ".join(result.blocked))
    if result.answer:
        tail = result.answer[-_REPORT_TAIL:]
        lines.append(("…" if len(result.answer) > _REPORT_TAIL else "") + tail)
    return "\n".join(lines)


async def catch_up(
    context: Any, *, now: dt.datetime | None = None, window: dt.timedelta = CATCH_UP_WINDOW
) -> int:
    """Run the schedules that came due while Balam was down; return how many.

    The JobQueue is in memory, so a restart at 07:29 would otherwise lose the
    07:30 brief with no trace anywhere — the worst property a scheduled job can
    have. Called from ``_post_init`` after :func:`register_all`.
    """
    store: SessionStore = context.application.bot_data["store"]
    now = now or dt.datetime.now(_timezone(context))
    ran = 0
    for row in store.all_schedules():
        schedule = Schedule.from_row(row)
        due = due_catch_up(schedule, now, window=window)
        if due is None:
            if schedule.enabled:
                logger.debug("schedule %s has nothing to catch up", schedule.id)
            continue
        logger.info("catching up schedule %s, due %s", schedule.id, due)
        try:
            if await run_schedule(context, schedule, late_for=due):
                ran += 1
        except Exception:
            # One bad schedule must not stop the rest (or abort boot).
            logger.exception("failed to catch up schedule %s", schedule.id)
    return ran


def _timezone(context: Any) -> ZoneInfo:
    """The configured schedule timezone, defaulting to UTC when no config is
    wired (unit tests of the bot path)."""
    config = context.application.bot_data.get("config")
    tz = getattr(config, "timezone", None)
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo("UTC")


def summarize(prompt: str, limit: int = 60) -> str:
    """A one-line preview of a prompt, for lists and notices."""
    text = " ".join(prompt.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _notify_chat(bot: Any, chat_id: int, text: str, *, thread_id: int | None = None) -> None:
    """Post a notice into a chat (General unless a topic is named), swallowing
    delivery failures — this is already the error path."""
    kwargs: dict[str, Any] = {}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception:
        logger.debug("failed to deliver schedule notice", exc_info=True)
