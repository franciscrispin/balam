import sqlite3

from balam.store import GENERAL_THREAD_ID, SessionStore


def fresh_store() -> SessionStore:
    # Each test gets its own in-memory DB — no file, no cross-test bleed.
    return SessionStore(":memory:")


def test_returns_none_for_unmapped_topic() -> None:
    store = fresh_store()
    assert store.get_row(100, 5) is None


def test_round_trips_a_mapping() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1_700_000_000_000)
    assert store.get_row(100, 5) == ("ses_abc", None)


def test_keys_are_scoped_per_chat_and_thread() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_a", 1)
    store.set(100, 6, "ses_b", 2)
    store.set(200, 5, "ses_c", 3)
    assert store.get_row(100, 5)[0] == "ses_a"
    assert store.get_row(100, 6)[0] == "ses_b"
    assert store.get_row(200, 5)[0] == "ses_c"


def test_set_overwrites_existing_mapping() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_old", 1)
    store.set(100, 5, "ses_new", 2)
    assert store.get_row(100, 5)[0] == "ses_new"


def test_delete_removes_mapping() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1)
    store.delete(100, 5)
    assert store.get_row(100, 5) is None


def test_general_topic_maps_to_catch_all_key() -> None:
    store = fresh_store()
    store.set(100, None, "ses_general", 1)
    assert store.get_row(100, None) == ("ses_general", None)
    assert store.get_row(100, GENERAL_THREAD_ID) == ("ses_general", None)


def test_thread_key_normalizes_none() -> None:
    assert SessionStore.thread_key(None) == GENERAL_THREAD_ID
    assert SessionStore.thread_key(7) == 7


def test_get_row_returns_session_and_context() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1, context="balam")
    assert store.get_row(100, 5) == ("ses_abc", "balam")


def test_get_row_is_none_for_unmapped() -> None:
    assert fresh_store().get_row(100, 5) is None


def test_context_defaults_to_none_when_omitted() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1)  # legacy 4-arg call
    assert store.get_row(100, 5) == ("ses_abc", None)


def test_set_overwrites_context() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1, context="balam")
    store.set(100, 5, "ses_def", 2, context="scratch")
    assert store.get_row(100, 5) == ("ses_def", "scratch")


def test_auto_named_marker_can_precede_session() -> None:
    store = fresh_store()

    store.mark_auto_named(100, 5)
    store.set(100, 5, "ses_abc", 1, context="balam")

    assert store.is_auto_named(100, 5) is True


def test_auto_named_survives_session_recreation() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_old", 1, context="balam")
    store.mark_auto_named(100, 5)

    # A vanished session is cleared and recreated; the name must carry across.
    store.delete(100, 5)
    store.set(100, 5, "ses_new", 2, context="balam")

    assert store.is_auto_named(100, 5) is True


def test_migrates_legacy_auto_named_column(tmp_path) -> None:
    # A DB written by the earlier schema, with auto-naming on a topic_sessions
    # column, is carried over into the marker table for the named rows only.
    path = str(tmp_path / "legacy.db")
    db = sqlite3.connect(path)
    db.execute(
        """
        CREATE TABLE topic_sessions (
            chat_id INTEGER, thread_id INTEGER, session_id TEXT, created_at INTEGER,
            context TEXT, auto_named INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (chat_id, thread_id)
        )
        """
    )
    db.execute("INSERT INTO topic_sessions VALUES (100, 5, 'ses_named', 1, 'balam', 1)")
    db.execute("INSERT INTO topic_sessions VALUES (100, 6, 'ses_fresh', 1, 'balam', 0)")
    db.commit()
    db.close()

    store = SessionStore(path)
    assert store.is_auto_named(100, 5) is True
    assert store.is_auto_named(100, 6) is False


def test_migrates_pre_auto_naming_schema(tmp_path) -> None:
    # A DB predating auto-naming has no column; every existing topic is treated
    # as already named so the upgrade doesn't unexpectedly retitle it.
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute(
        """
        CREATE TABLE topic_sessions (
            chat_id INTEGER, thread_id INTEGER, session_id TEXT, created_at INTEGER,
            context TEXT, PRIMARY KEY (chat_id, thread_id)
        )
        """
    )
    db.execute("INSERT INTO topic_sessions VALUES (100, 5, 'ses_old', 1, 'balam')")
    db.commit()
    db.close()

    store = SessionStore(path)
    assert store.is_auto_named(100, 5) is True

    # The backfill is one-time: a topic later marked nameable stays nameable
    # across a reopen (the migration does not re-run and re-mark it).
    store.close()
    store = SessionStore(path)
    assert store.is_auto_named(100, 6) is False


# --- plan mode (/plan) ----------------------------------------------------------
# --- model/effort overrides ----------------------------------------------------


def test_overrides_default_to_unset() -> None:
    store = fresh_store()
    assert store.get_overrides(1, 7) == (None, None, None)


def test_model_override_round_trips_and_resets() -> None:
    store = fresh_store()

    store.set_model_override(1, 7, "anthropic", "claude-sonnet-4")
    assert store.get_overrides(1, 7) == ("anthropic", "claude-sonnet-4", None)

    store.reset_model_override(1, 7)
    assert store.get_overrides(1, 7) == (None, None, None)


def test_effort_override_round_trips_and_resets() -> None:
    store = fresh_store()

    store.set_effort_override(1, 7, "high")
    assert store.get_overrides(1, 7) == (None, None, "high")

    store.reset_effort_override(1, 7)
    assert store.get_overrides(1, 7) == (None, None, None)


def test_overrides_are_scoped_per_chat_and_thread() -> None:
    store = fresh_store()

    store.set_model_override(1, 7, "anthropic", "claude-sonnet-4")
    store.set_effort_override(1, 8, "low")
    store.set_effort_override(2, 7, "max")

    assert store.get_overrides(1, 7) == ("anthropic", "claude-sonnet-4", None)
    assert store.get_overrides(1, 8) == (None, None, "low")
    assert store.get_overrides(2, 7) == (None, None, "max")


def test_overrides_survive_session_recreation() -> None:
    store = fresh_store()
    store.set(1, 7, "ses_old", 1, context="balam")
    store.set_model_override(1, 7, "anthropic", "claude-sonnet-4")
    store.set_effort_override(1, 7, "medium")

    store.delete(1, 7)
    store.set(1, 7, "ses_new", 2, context="balam")

    assert store.get_overrides(1, 7) == ("anthropic", "claude-sonnet-4", "medium")


def test_override_reset_is_idempotent() -> None:
    store = fresh_store()
    store.reset_model_override(1, 7)
    store.reset_effort_override(1, 7)
    assert store.get_overrides(1, 7) == (None, None, None)


def test_overrides_normalize_general_thread() -> None:
    store = fresh_store()
    store.set_model_override(1, None, "anthropic", "claude-sonnet-4")
    store.set_effort_override(1, None, "xhigh")

    assert store.get_overrides(1, GENERAL_THREAD_ID) == (
        "anthropic",
        "claude-sonnet-4",
        "xhigh",
    )


# --- titles + /delete picker ---------------------------------------------------


def _titles(store: SessionStore, chat_id: int) -> dict[int, str | None]:
    return {thread_id: title for thread_id, title, _ in store.list_topics(chat_id)}


def test_set_persists_title() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1, context="balam", title="My topic")
    assert _titles(store, 100) == {5: "My topic"}


def test_set_without_title_preserves_existing() -> None:
    # persist_session re-saves the row without a title; the stored one must stay.
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1, context="balam", title="My topic")
    store.set(100, 5, "ses_def", 2, context="balam")
    assert _titles(store, 100) == {5: "My topic"}


def test_set_title_updates_in_place() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1, context="balam", title="Old")
    store.set_title(100, 5, "New")
    assert _titles(store, 100) == {5: "New"}


def test_set_title_noop_for_unmapped_topic() -> None:
    store = fresh_store()
    store.set_title(100, 5, "Whatever")  # no row yet — must not raise or create one
    assert store.list_topics(100) == []


def test_list_topics_excludes_general_and_orders_newest_first() -> None:
    store = fresh_store()
    store.set(100, None, "ses_general", 1, context="balam", title="General")
    store.set(100, 7, "ses_b", 3, context="scratch", title="Second")
    store.set(100, 5, "ses_a", 2, context="balam", title="First")
    assert store.list_topics(100) == [(7, "Second", "scratch"), (5, "First", "balam")]


def test_list_topics_is_scoped_per_chat() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_a", 1, context="balam", title="A")
    store.set(200, 6, "ses_b", 1, context="balam", title="B")
    assert store.list_topics(100) == [(5, "A", "balam")]


def test_purge_clears_every_per_topic_table() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1, context="balam", title="Doomed")
    store.mark_auto_named(100, 5)
    store.set_effort_override(100, 5, "high")

    store.purge(100, 5)

    assert store.get_row(100, 5) is None
    assert store.is_auto_named(100, 5) is False
    assert store.get_overrides(100, 5) == (None, None, None)
    assert store.list_topics(100) == []


def test_migrates_schema_without_title_column(tmp_path) -> None:
    # A DB predating the title column gains it on open, with NULL for old rows.
    path = str(tmp_path / "no_title.db")
    db = sqlite3.connect(path)
    db.execute(
        """
        CREATE TABLE topic_sessions (
            chat_id INTEGER, thread_id INTEGER, session_id TEXT, created_at INTEGER,
            context TEXT, PRIMARY KEY (chat_id, thread_id)
        )
        """
    )
    db.execute("INSERT INTO topic_sessions VALUES (100, 5, 'ses_old', 1, 'balam')")
    db.commit()
    db.close()

    store = SessionStore(path)
    assert store.list_topics(100) == [(5, None, "balam")]
    store.set_title(100, 5, "Backfilled")
    assert _titles(store, 100) == {5: "Backfilled"}


# --- schedules (/schedule, ADR-0016) ------------------------------------------


def _add(store: SessionStore, **overrides) -> int:
    base = {
        "chat_id": 100,
        "context": "chaska",
        "prompt": "plan my day",
        "kind": "daily",
        "hour": 7,
        "minute": 30,
        "days": None,
        "created_at": 1_700_000_000_000,
    }
    base.update(overrides)
    return store.add_schedule(**base)  # type: ignore[arg-type]


def test_add_schedule_round_trips() -> None:
    store = fresh_store()
    schedule_id = _add(store)
    row = store.get_schedule(schedule_id)
    assert row is not None
    assert (row.chat_id, row.context, row.prompt) == (100, "chaska", "plan my day")
    assert (row.kind, row.hour, row.minute, row.days) == ("daily", 7, 30, None)
    # New schedules are on, and have never run.
    assert row.enabled == 1
    assert row.last_run_at is None


def test_schedule_days_are_stored_as_csv() -> None:
    store = fresh_store()
    row = store.get_schedule(_add(store, kind="weekdays", days="0,1,2,3,4"))
    assert row is not None and row.days == "0,1,2,3,4"


def test_list_schedules_is_scoped_per_chat_and_ordered_by_id() -> None:
    store = fresh_store()
    first = _add(store)
    second = _add(store, hour=9)
    _add(store, chat_id=200)
    assert [row.id for row in store.list_schedules(100)] == [first, second]
    assert [row.chat_id for row in store.list_schedules(200)] == [200]


def test_all_schedules_spans_chats() -> None:
    # What boot registration walks: the JobQueue is global, not per chat.
    store = fresh_store()
    _add(store)
    _add(store, chat_id=200)
    assert {row.chat_id for row in store.all_schedules()} == {100, 200}


def test_get_schedule_is_none_when_unknown() -> None:
    assert fresh_store().get_schedule(999) is None


def test_delete_schedule_reports_whether_it_existed() -> None:
    store = fresh_store()
    schedule_id = _add(store)
    assert store.delete_schedule(schedule_id) is True
    assert store.delete_schedule(schedule_id) is False
    assert store.get_schedule(schedule_id) is None


def test_set_schedule_enabled_toggles_without_losing_the_row() -> None:
    store = fresh_store()
    schedule_id = _add(store)
    assert store.set_schedule_enabled(schedule_id, False) is True
    assert store.get_schedule(schedule_id).enabled == 0  # type: ignore[union-attr]
    store.set_schedule_enabled(schedule_id, True)
    assert store.get_schedule(schedule_id).enabled == 1  # type: ignore[union-attr]
    assert store.set_schedule_enabled(999, False) is False


def test_mark_schedule_run_stamps_the_row() -> None:
    store = fresh_store()
    schedule_id = _add(store)
    store.mark_schedule_run(schedule_id, 1_700_000_123_000)
    assert store.get_schedule(schedule_id).last_run_at == 1_700_000_123_000  # type: ignore[union-attr]


def test_fresh_db_lands_on_schema_version_3() -> None:
    store = fresh_store()
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_migrates_v2_database_to_schedules(tmp_path) -> None:
    # An existing DB at the title-column level (v2) gains the schedules table
    # without touching the rows it already holds.
    path = str(tmp_path / "v2.db")
    db = sqlite3.connect(path)
    db.execute(
        """
        CREATE TABLE topic_sessions (
            chat_id INTEGER, thread_id INTEGER, session_id TEXT, created_at INTEGER,
            context TEXT, title TEXT, PRIMARY KEY (chat_id, thread_id)
        )
        """
    )
    db.execute("INSERT INTO topic_sessions VALUES (100, 5, 'ses_old', 1, 'balam', 'Old')")
    db.execute("PRAGMA user_version = 2")
    db.commit()
    db.close()

    store = SessionStore(path)
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert store.list_topics(100) == [(5, "Old", "balam")]
    assert store.all_schedules() == []
    schedule_id = _add(store)
    assert store.get_schedule(schedule_id) is not None
