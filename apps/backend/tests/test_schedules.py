"""Scheduled prompts (/schedule, ADR-0016).

Three things get their own attention here, because each is a silent failure if
wrong: the Python↔PTB weekday conversion, the timezone the times are read in, and
the missed-run catch-up window.
"""

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telegram.ext import JobQueue

from balam import schedules as S
from balam.agent.opencode_backend import OpenCodeBackend
from balam.approvals import PendingApprovals
from balam.contexts import ContextConfig, ContextsConfig
from balam.router import Router, TopicRef
from balam.store import SessionStore
from balam.turns import TurnRegistry

SGT = ZoneInfo("Asia/Singapore")
SUPERGROUP = -1001234567890


# --- parse_when: the three v1 forms -------------------------------------------


def test_parse_daily() -> None:
    when = S.parse_when(["daily", "07:30"])
    assert (when.kind, when.hour, when.minute, when.days) == ("daily", 7, 30, ())
    assert when.days_csv is None


def test_parse_weekdays_is_monday_to_friday() -> None:
    when = S.parse_when(["weekdays", "09:00"])
    assert when.kind == "weekdays"
    assert when.days == (0, 1, 2, 3, 4)  # Python numbering: Mon=0
    assert when.days_csv == "0,1,2,3,4"


def test_parse_single_weekday() -> None:
    when = S.parse_when(["fri", "17:00"])
    assert (when.kind, when.hour, when.minute, when.days) == ("dow", 17, 0, (4,))


def test_parse_accepts_long_and_alternate_weekday_spellings() -> None:
    assert S.parse_when(["Monday", "08:00"]).days == (0,)
    assert S.parse_when(["tues", "08:00"]).days == (1,)
    assert S.parse_when(["THURS", "08:00"]).days == (3,)


def test_parse_accepts_a_single_digit_hour() -> None:
    assert S.parse_when(["daily", "7:05"]).at == "07:05"


@pytest.mark.parametrize(
    "tokens",
    [
        [],
        ["daily"],
        ["daily", "0730"],
        ["daily", "25:00"],
        ["daily", "07:99"],
        ["daily", "seven"],
        ["hourly", "07:30"],
        ["someday", "07:30"],
    ],
)
def test_parse_rejects_junk_with_the_three_examples(tokens: list[str]) -> None:
    with pytest.raises(S.ScheduleError) as exc:
        S.parse_when(tokens)
    # The error shows the accepted forms rather than a grammar dump.
    for example in S.WHEN_EXAMPLES:
        assert example in str(exc.value)


# --- describe ------------------------------------------------------------------


def test_describe_reads_back_each_form() -> None:
    assert S.describe(S.parse_when(["daily", "07:30"])) == "every day at 07:30"
    assert S.describe(S.parse_when(["weekdays", "09:00"])) == "Mon–Fri at 09:00"
    assert S.describe(S.parse_when(["fri", "17:00"])) == "every Fri at 17:00"


# --- the weekday conversion PTB gets backwards --------------------------------


def test_ptb_days_shifts_python_weekdays_onto_sunday_zero() -> None:
    # PTB's run_daily numbers 0-6 as Sunday-Saturday; datetime.weekday() is
    # Monday-Sunday. Getting this wrong shifts every schedule by one day.
    assert JobQueue._CRON_MAPPING == ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
    assert S._ptb_days((0,)) == (1,)  # Mon
    assert S._ptb_days((4,)) == (5,)  # Fri
    assert S._ptb_days((6,)) == (0,)  # Sun
    assert S._ptb_days(S.WORKWEEK) == (1, 2, 3, 4, 5)


def test_ptb_day_names_match_the_python_weekday_they_came_from() -> None:
    for python_day in range(7):
        (ptb_day,) = S._ptb_days((python_day,))
        # 2026-01-05 is a Monday, so +python_day lands on that weekday.
        date = dt.date(2026, 1, 5) + dt.timedelta(days=python_day)
        assert JobQueue._CRON_MAPPING[ptb_day] == date.strftime("%a").lower()


# --- previous_due --------------------------------------------------------------


def _at(year: int, month: int, day: int, hour: int, minute: int = 0, tz=SGT) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=tz)


def test_previous_due_daily_before_the_time_is_yesterday() -> None:
    when = S.parse_when(["daily", "07:30"])
    assert S.previous_due(when, _at(2026, 7, 31, 7, 0)) == _at(2026, 7, 30, 7, 30)


def test_previous_due_daily_after_the_time_is_today() -> None:
    when = S.parse_when(["daily", "07:30"])
    assert S.previous_due(when, _at(2026, 7, 31, 8, 0)) == _at(2026, 7, 31, 7, 30)


def test_previous_due_at_exactly_the_due_minute_is_now() -> None:
    when = S.parse_when(["daily", "07:30"])
    assert S.previous_due(when, _at(2026, 7, 31, 7, 30)) == _at(2026, 7, 31, 7, 30)


def test_previous_due_weekdays_skips_back_over_the_weekend() -> None:
    when = S.parse_when(["weekdays", "09:00"])
    # 2026-08-02 is a Sunday; the last weekday run was Friday the 31st.
    assert S.previous_due(when, _at(2026, 8, 2, 12, 0)) == _at(2026, 7, 31, 9, 0)


def test_previous_due_single_weekday_goes_back_a_full_week() -> None:
    when = S.parse_when(["fri", "17:00"])
    # Friday 2026-07-31 at noon: today's 17:00 hasn't happened, so last week's did.
    assert S.previous_due(when, _at(2026, 7, 31, 12, 0)) == _at(2026, 7, 24, 17, 0)


def test_previous_due_follows_the_wall_clock_across_a_dst_change() -> None:
    # Singapore has no DST, but the tz-aware path must not assume that: a 07:30
    # schedule stays 07:30 local, so its UTC offset changes at the transition.
    ny = ZoneInfo("America/New_York")
    when = S.parse_when(["daily", "07:30"])
    # 2026-03-08 is spring-forward in the US.
    before = S.previous_due(when, _at(2026, 3, 7, 12, 0, tz=ny))
    after = S.previous_due(when, _at(2026, 3, 9, 12, 0, tz=ny))
    assert (before.hour, before.minute) == (7, 30)
    assert (after.hour, after.minute) == (7, 30)
    assert before.utcoffset() == dt.timedelta(hours=-5)  # EST
    assert after.utcoffset() == dt.timedelta(hours=-4)  # EDT


# --- due_catch_up: what a restart should and shouldn't re-run ------------------


def _schedule(**overrides) -> S.Schedule:
    base = dict(
        id=1,
        chat_id=SUPERGROUP,
        context="chaska",
        prompt="plan my day",
        kind="daily",
        hour=7,
        minute=30,
        days=(),
        enabled=True,
        created_at=0,
        last_run_at=None,
    )
    base.update(overrides)
    return S.Schedule(**base)  # type: ignore[arg-type]


def _ms(when: dt.datetime) -> int:
    return int(when.timestamp() * 1000)


def test_catch_up_fires_a_run_missed_minutes_ago() -> None:
    # The failure this exists for: a restart at 07:29 losing the 07:30 brief.
    now = _at(2026, 7, 31, 7, 45)
    assert S.due_catch_up(_schedule(), now) == _at(2026, 7, 31, 7, 30)


def test_catch_up_skips_a_run_already_stamped() -> None:
    due = _at(2026, 7, 31, 7, 30)
    schedule = _schedule(last_run_at=_ms(due))
    assert S.due_catch_up(schedule, _at(2026, 7, 31, 7, 45)) is None


def test_catch_up_still_fires_when_the_stamp_predates_this_occurrence() -> None:
    # Yesterday's run must not suppress today's.
    schedule = _schedule(last_run_at=_ms(_at(2026, 7, 30, 7, 30)))
    assert S.due_catch_up(schedule, _at(2026, 7, 31, 7, 45)) == _at(2026, 7, 31, 7, 30)


def test_catch_up_skips_outside_the_window() -> None:
    # A VM that was off for a week must not produce seven topics at boot.
    now = _at(2026, 7, 31, 20, 0)  # 12.5h after the 07:30 due time
    assert S.due_catch_up(_schedule(), now) is None


def test_catch_up_window_edge_is_inclusive() -> None:
    now = _at(2026, 7, 31, 7, 30) + S.CATCH_UP_WINDOW
    assert S.due_catch_up(_schedule(), now) is not None
    assert S.due_catch_up(_schedule(), now + dt.timedelta(seconds=1)) is None


def test_catch_up_ignores_a_disabled_schedule() -> None:
    assert S.due_catch_up(_schedule(enabled=False), _at(2026, 7, 31, 7, 45)) is None


# --- JobQueue registration -----------------------------------------------------


def _store_with(*schedules_kwargs: dict) -> SessionStore:
    store = SessionStore(":memory:")
    for kwargs in schedules_kwargs:
        base = {
            "chat_id": SUPERGROUP,
            "context": "chaska",
            "prompt": "plan my day",
            "kind": "daily",
            "hour": 7,
            "minute": 30,
            "days": None,
            "created_at": 0,
        }
        base.update(kwargs)
        store.add_schedule(**base)  # type: ignore[arg-type]
    return store


def test_register_one_names_the_job_after_the_schedule() -> None:
    queue = JobQueue()
    S.register_one(queue, _schedule(id=7), SGT)
    assert [job.name for job in queue.get_jobs_by_name("schedule:7")] == ["schedule:7"]


def test_re_registering_replaces_rather_than_duplicates() -> None:
    # An edit must not leave the old timer behind, or the prompt fires twice.
    queue = JobQueue()
    S.register_one(queue, _schedule(id=7), SGT)
    S.register_one(queue, _schedule(id=7, hour=9), SGT)
    assert len(queue.get_jobs_by_name("schedule:7")) == 1


def test_register_one_skips_a_disabled_schedule() -> None:
    queue = JobQueue()
    S.register_one(queue, _schedule(id=7, enabled=False), SGT)
    assert queue.get_jobs_by_name("schedule:7") == ()


def test_unregister_removes_the_timer_and_reports_how_many() -> None:
    queue = JobQueue()
    S.register_one(queue, _schedule(id=7), SGT)
    assert S.unregister(queue, 7) == 1
    assert queue.get_jobs_by_name("schedule:7") == ()
    assert S.unregister(queue, 7) == 0


def test_register_all_registers_only_the_enabled_ones() -> None:
    store = _store_with({}, {"hour": 9}, {})
    store.set_schedule_enabled(2, False)
    queue = JobQueue()
    assert S.register_all(queue, store, SGT) == 2
    assert queue.get_jobs_by_name("schedule:2") == ()
    assert len(queue.get_jobs_by_name("schedule:1")) == 1
    assert len(queue.get_jobs_by_name("schedule:3")) == 1


def test_registered_job_carries_the_configured_timezone() -> None:
    queue = JobQueue()
    S.register_one(queue, _schedule(id=7), SGT)
    (job,) = queue.get_jobs_by_name("schedule:7")
    assert job.job.trigger.timezone.key == "Asia/Singapore"


# --- firing: the integration seam ----------------------------------------------


class _FakeOpenCode:
    def __init__(self) -> None:
        self._n = 0

    async def create_session(self, title: str, **_: object) -> str:
        self._n += 1
        return f"ses_{self._n}"

    async def session_exists(self, session_id: str, **_: object) -> bool:
        return True

    async def update_session_permission(self, session_id: str, **_: object) -> None:
        return None


class _FakeBot:
    def __init__(self, *, new_thread_id: int = 555) -> None:
        self.id = 12345
        self._new_thread_id = new_thread_id
        self.created_topics: list[tuple[int, str]] = []
        self.sent: list[tuple[int, str, int | None]] = []

    async def create_forum_topic(self, *, chat_id: int, name: str) -> SimpleNamespace:
        self.created_topics.append((chat_id, name))
        return SimpleNamespace(message_thread_id=self._new_thread_id, name=name)

    async def send_message(
        self, *, chat_id: int, text: str, message_thread_id: int | None = None, **_: object
    ) -> None:
        self.sent.append((chat_id, text, message_thread_id))


class _FailingTopicBot(_FakeBot):
    async def create_forum_topic(self, *, chat_id: int, name: str):
        raise RuntimeError("not a forum supergroup")


def _fire_env(bot: _FakeBot | None = None, store: SessionStore | None = None):
    """A (context, store, bot, turns) tuple wired the way a job callback sees it."""
    bot = bot or _FakeBot()
    store = store or _store_with({})
    contexts = ContextsConfig(
        default_context="chaska",
        contexts={"chaska": ContextConfig(directory="/work/chaska", description="Chaska")},
    )
    router = Router(store, _FakeOpenCode(), contexts)
    turns = TurnRegistry()
    queue = JobQueue()
    application = SimpleNamespace(
        bot_data={
            "router": router,
            "store": store,
            "turns": turns,
            "backend": OpenCodeBackend(_FakeOpenCode()),
            "pending": PendingApprovals(),
            "config": SimpleNamespace(timezone=SGT, tool_stream="collapsed", rich_messages=True),
        },
        job_queue=queue,
    )
    context = SimpleNamespace(application=application, bot=bot)
    return context, store, bot, turns


async def test_run_schedule_opens_a_topic_and_starts_an_unattended_turn(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_stream_reply(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("balam.turns.stream_reply", fake_stream_reply)

    context, store, bot, turns = _fire_env()
    assert await S.run_schedule(context, S.Schedule.from_row(store.get_schedule(1))) is True

    turn = turns.get(SUPERGROUP, 555)
    assert turn is not None
    await turn.task

    # A fresh topic, named after the prompt exactly as /new <ctx> <prompt> names it.
    assert bot.created_topics == [(SUPERGROUP, "chaska: plan my day")]
    # It is bound to the schedule's context, and the prompt ran there.
    assert (
        context.application.bot_data["router"].current_context_name(TopicRef(SUPERGROUP, 555, "t"))
        == "chaska"
    )
    assert captured["prompt"] == "plan my day"
    assert captured["thread_id"] == 555
    assert captured["directory"] == "/work/chaska"
    # The whole point of §6: nobody is watching this turn.
    assert captured["unattended"] is True


async def test_run_schedule_stamps_the_run_before_starting_the_turn(monkeypatch) -> None:
    # Written at the start, not the end: a crash mid-turn must not make catch-up
    # re-fire the whole thing on restart. Read the stamp from *inside* the turn.
    context, store, _bot, turns = _fire_env()
    seen: dict[str, object] = {}

    async def fake_stream_reply(**_: object) -> None:
        seen["last_run_at"] = store.get_schedule(1).last_run_at  # type: ignore[union-attr]
        raise RuntimeError("turn dies here")

    monkeypatch.setattr("balam.turns.stream_reply", fake_stream_reply)
    await S.run_schedule(context, S.Schedule.from_row(store.get_schedule(1)))
    await turns.get(SUPERGROUP, 555).task  # type: ignore[union-attr]

    assert seen["last_run_at"] is not None
    # And it survives the failed turn, so the restart won't repeat it.
    assert store.get_schedule(1).last_run_at is not None  # type: ignore[union-attr]


async def test_run_schedule_parks_a_schedule_whose_context_vanished() -> None:
    # config.yaml can lose a context under a schedule that names it. Raising into
    # the JobQueue's error handler would hide that from the owner forever.
    context, store, bot, turns = _fire_env()
    store.add_schedule(
        chat_id=SUPERGROUP,
        context="gone",
        prompt="p",
        kind="daily",
        hour=7,
        minute=30,
        days=None,
        created_at=0,
    )
    schedule = S.Schedule.from_row(store.get_schedule(2))
    S.register_one(context.application.job_queue, schedule, SGT)

    assert await S.run_schedule(context, schedule) is False

    assert store.get_schedule(2).enabled == 0  # type: ignore[union-attr]
    assert context.application.job_queue.get_jobs_by_name("schedule:2") == ()
    assert any("parked" in text for _chat, text, _thread in bot.sent)
    assert bot.created_topics == []
    assert turns.get(SUPERGROUP, 555) is None


async def test_run_schedule_reports_a_topic_it_could_not_open() -> None:
    context, store, _bot, turns = _fire_env(bot=_FailingTopicBot())
    bot = context.bot
    assert await S.run_schedule(context, S.Schedule.from_row(store.get_schedule(1))) is False
    assert any("Schedule #1" in text for _chat, text, _thread in bot.sent)
    assert turns.get(SUPERGROUP, 555) is None


async def test_run_schedule_marks_a_catch_up_run_as_late(monkeypatch) -> None:
    async def fake_stream_reply(**_: object) -> None:
        return None

    monkeypatch.setattr("balam.turns.stream_reply", fake_stream_reply)
    context, store, bot, turns = _fire_env()
    await S.run_schedule(
        context,
        S.Schedule.from_row(store.get_schedule(1)),
        late_for=_at(2026, 7, 31, 7, 30),
    )
    await turns.get(SUPERGROUP, 555).task  # type: ignore[union-attr]

    # The note lands in the new topic, so a 09:12 brief doesn't read as 09:12 work.
    assert any("Late run" in text and thread == 555 for _chat, text, thread in bot.sent)


async def test_fire_skips_a_deleted_schedule() -> None:
    context, store, bot, _turns = _fire_env()
    store.delete_schedule(1)
    context.job = SimpleNamespace(data=1)
    await S.fire(context)
    assert bot.created_topics == []


async def test_fire_skips_a_disabled_schedule() -> None:
    context, store, bot, _turns = _fire_env()
    store.set_schedule_enabled(1, False)
    context.job = SimpleNamespace(data=1)
    await S.fire(context)
    assert bot.created_topics == []


async def test_fire_does_not_double_run_an_occurrence_catch_up_already_took() -> None:
    # Boot inside the due minute is the one window where the timer and catch-up
    # can both claim the same occurrence.
    context, store, bot, _turns = _fire_env()
    due = S.previous_due(S.parse_when(["daily", "07:30"]), dt.datetime.now(SGT))
    store.mark_schedule_run(1, _ms(due))
    context.job = SimpleNamespace(data=1)
    await S.fire(context)
    assert bot.created_topics == []


async def test_catch_up_runs_only_what_is_due(monkeypatch) -> None:
    async def fake_stream_reply(**_: object) -> None:
        return None

    monkeypatch.setattr("balam.turns.stream_reply", fake_stream_reply)

    now = dt.datetime.now(SGT).replace(microsecond=0)
    due_recently = (now - dt.timedelta(minutes=15)).timetz()
    long_past = (now - dt.timedelta(hours=10)).timetz()
    store = _store_with(
        {"hour": due_recently.hour, "minute": due_recently.minute},
        {"hour": long_past.hour, "minute": long_past.minute},
    )
    context, store, bot, _turns = _fire_env(store=store)

    assert await S.catch_up(context, now=now) == 1
    assert len(bot.created_topics) == 1
    # Only the recent one is stamped; the stale one is left alone for its timer.
    assert store.get_schedule(1).last_run_at is not None  # type: ignore[union-attr]
    assert store.get_schedule(2).last_run_at is None  # type: ignore[union-attr]

    # Idempotent: a second boot in the same window doesn't repeat the run.
    assert await S.catch_up(context, now=now) == 0


async def test_catch_up_survives_one_broken_schedule(monkeypatch) -> None:
    # One bad schedule must not abort boot or block the others.
    async def fake_stream_reply(**_: object) -> None:
        return None

    monkeypatch.setattr("balam.turns.stream_reply", fake_stream_reply)

    now = dt.datetime.now(SGT).replace(microsecond=0)
    recent = (now - dt.timedelta(minutes=5)).timetz()
    store = _store_with(
        {"hour": recent.hour, "minute": recent.minute},
        {"hour": recent.hour, "minute": recent.minute},
    )
    context, store, bot, _turns = _fire_env(store=store)

    original = S.run_schedule

    async def exploding(ctx, schedule, **kwargs):
        if schedule.id == 1:
            raise RuntimeError("boom")
        return await original(ctx, schedule, **kwargs)

    monkeypatch.setattr(S, "run_schedule", exploding)
    assert await S.catch_up(context, now=now) == 1
    assert len(bot.created_topics) == 1


def test_summarize_collapses_and_truncates() -> None:
    assert S.summarize("plan  my\nday") == "plan my day"
    assert S.summarize("x" * 100, limit=10) == "x" * 9 + "…"
