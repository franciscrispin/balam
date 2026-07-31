"""Persistent topic→session map (ADR-0009).

Each Telegram forum topic maps to exactly one OpenCode session. We persist the
mapping in SQLite so threads survive a backend restart while the sessions stay
warm in the long-lived OpenCode server (ADR-0001). The stdlib ``sqlite3`` module
keeps this dependency-free.

Telegram's "General" topic carries no ``message_thread_id``; we normalize that
absence to thread id ``0`` (the catch-all session, ADR-0009) so the key is
always a concrete integer pair.
"""

from __future__ import annotations

import sqlite3
from typing import NamedTuple

#: Sentinel thread id for the General topic (no ``message_thread_id``).
GENERAL_THREAD_ID = 0


class ScheduleRow(NamedTuple):
    """One row of the ``schedules`` table, exactly as stored (ADR-0016).

    Deliberately raw — ``days`` is still CSV and ``enabled`` still an integer.
    :class:`balam.schedules.Schedule` is the parsed form the rest of the code
    uses; keeping the split means the store depends on nothing above it.
    """

    id: int
    chat_id: int
    context: str
    prompt: str
    kind: str
    hour: int
    minute: int
    #: CSV of Python weekday numbers (``Mon=0`` … ``Sun=6``), or ``None`` for daily.
    days: str | None
    enabled: int
    created_at: int
    #: Epoch ms of the last run's *start*, or ``None`` until it first fires.
    last_run_at: int | None


_SCHEDULE_COLUMNS = ", ".join(ScheduleRow._fields)


def _schedule_row(row: tuple) -> ScheduleRow:
    return ScheduleRow(*row)


class SessionStore:
    def __init__(self, path: str) -> None:
        # check_same_thread=False: PTB's asyncio loop may touch this from the
        # main thread and job-queue workers; access is serialized by the GIL and
        # our short, committed statements.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode = WAL;")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_sessions (
                chat_id    INTEGER NOT NULL,
                thread_id  INTEGER NOT NULL,
                session_id TEXT    NOT NULL,
                created_at INTEGER NOT NULL,
                context    TEXT,
                title      TEXT,
                PRIMARY KEY (chat_id, thread_id)
            );
            """
        )
        # Auto-naming state lives in its own table — the single source of truth.
        # It is keyed independently of ``topic_sessions`` so a topic can be marked
        # (e.g. manually renamed via ``/rename``) before it has a session, and so
        # the flag survives a session being recreated (``delete`` leaves it alone).
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_auto_names (
                chat_id   INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, thread_id)
            );
            """
        )
        # Per-topic model/effort overrides. Kept separate from ``topic_sessions``
        # so they can be set before a session exists and survive session
        # recreation when a stale session row is deleted.
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_overrides (
                chat_id        INTEGER NOT NULL,
                thread_id      INTEGER NOT NULL,
                model_provider TEXT,
                model          TEXT,
                effort         TEXT,
                PRIMARY KEY (chat_id, thread_id)
            );
            """
        )
        # Saved ``(when, context, prompt)`` triples driving /schedule (ADR-0016).
        # Not keyed by topic: a schedule *opens* a topic when it fires, so it
        # belongs to the chat, and outlives every topic it has ever created.
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                context     TEXT    NOT NULL,
                prompt      TEXT    NOT NULL,
                kind        TEXT    NOT NULL,
                hour        INTEGER NOT NULL,
                minute      INTEGER NOT NULL,
                days        TEXT,
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  INTEGER NOT NULL,
                last_run_at INTEGER
            );
            """
        )
        self._migrate_auto_named()
        self._migrate_title()
        self._migrate_schedules()
        self._db.commit()

    def _migrate_auto_named(self) -> None:
        """One-time backfill of :class:`topic_auto_names` from earlier schemas.

        Older databases tracked auto-naming on a now-removed ``topic_sessions``
        column (or not at all). Run once, guarded by ``PRAGMA user_version``, to
        seed the marker table so the next message after an upgrade does not
        unexpectedly retitle topics that were already named:

          * a DB that predates auto-naming entirely (no column) → treat every
            existing topic as already named;
          * a DB with the legacy ``auto_named`` column → carry over the rows it
            marked named (``auto_named = 1``).
        """
        if self._db.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        columns = {
            row[1] for row in self._db.execute("PRAGMA table_info(topic_sessions)").fetchall()
        }
        select = "SELECT chat_id, thread_id FROM topic_sessions"
        if "auto_named" in columns:
            select += " WHERE auto_named = 1"
        self._db.execute(f"INSERT OR IGNORE INTO topic_auto_names (chat_id, thread_id) {select}")
        self._db.execute("PRAGMA user_version = 1")

    def _migrate_title(self) -> None:
        """Add the ``title`` column to :class:`topic_sessions` for databases created
        before topic titles were tracked. The column drives the ``/delete`` picker
        (:meth:`list_topics` labels each topic by it); older rows simply carry a
        ``NULL`` title until the topic is next (re)named."""
        if self._db.execute("PRAGMA user_version").fetchone()[0] >= 2:
            return
        columns = {
            row[1] for row in self._db.execute("PRAGMA table_info(topic_sessions)").fetchall()
        }
        if "title" not in columns:
            self._db.execute("ALTER TABLE topic_sessions ADD COLUMN title TEXT")
        self._db.execute("PRAGMA user_version = 2")

    def _migrate_schedules(self) -> None:
        """Record that the schema has reached the :class:`schedules` level (v3).

        The table itself is created idempotently in ``__init__`` — it is new, so
        unlike the two migrations above there is nothing to backfill. The version
        bump is what a later migration reads to know this rung of the ladder has
        been passed.
        """
        if self._db.execute("PRAGMA user_version").fetchone()[0] >= 3:
            return
        self._db.execute("PRAGMA user_version = 3")

    @staticmethod
    def thread_key(thread_id: int | None) -> int:
        """Normalize an optional ``message_thread_id`` to a concrete key."""
        return GENERAL_THREAD_ID if thread_id is None else thread_id

    def get_row(self, chat_id: int, thread_id: int | None) -> tuple[str, str | None] | None:
        """Resolve a topic to ``(session_id, context)``, or ``None`` if unmapped.

        ``context`` is the bound context name (``None`` for rows created before
        contexts existed, or never bound — the caller then uses the default)."""
        row = self._db.execute(
            "SELECT session_id, context FROM topic_sessions WHERE chat_id = ? AND thread_id = ?",
            (chat_id, self.thread_key(thread_id)),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def set(
        self,
        chat_id: int,
        thread_id: int | None,
        session_id: str,
        created_at: int,
        context: str | None = None,
        title: str | None = None,
    ) -> None:
        """Map a topic to a session (and the context it was created in),
        overwriting any existing mapping — used both for first creation and for
        recreating a session that vanished server-side.

        Auto-naming state is tracked separately (:meth:`mark_auto_named`) and is
        deliberately untouched here, so recreating a vanished session keeps a
        topic's existing name. ``title`` is likewise preserved when omitted: a
        title-less call (e.g. :meth:`balam.router.Router.persist_session`) keeps
        any title already stored, so only :meth:`set_title` and explicit
        creation/recreation titles change it."""
        self._db.execute(
            """
            INSERT INTO topic_sessions (chat_id, thread_id, session_id, created_at, context, title)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (chat_id, thread_id)
            DO UPDATE SET
                session_id = excluded.session_id,
                created_at = excluded.created_at,
                context = excluded.context,
                title = COALESCE(excluded.title, topic_sessions.title)
            """,
            (chat_id, self.thread_key(thread_id), session_id, created_at, context, title),
        )
        self._db.commit()

    def set_title(self, chat_id: int, thread_id: int | None, title: str) -> None:
        """Record a topic's current Telegram title (set on create, auto-name,
        ``/rename``, and manual ``forum_topic_edited`` updates) so the ``/delete``
        picker can label it. No-op for an unmapped topic."""
        self._db.execute(
            "UPDATE topic_sessions SET title = ? WHERE chat_id = ? AND thread_id = ?",
            (title, chat_id, self.thread_key(thread_id)),
        )
        self._db.commit()

    def list_topics(self, chat_id: int) -> list[tuple[int, str | None, str | None]]:
        """All mapped topics in a chat as ``(thread_id, title, context)``, newest
        first. The General topic (``GENERAL_THREAD_ID``) is excluded — it cannot be
        deleted via the Bot API, so it never appears in the picker. Newest-first
        ordering ensures the /delete picker's cap surfaces the topics most likely to
        need cleanup (recent throwaway ones) rather than the oldest, unreachable."""
        return [
            (row[0], row[1], row[2])
            for row in self._db.execute(
                """
                SELECT thread_id, title, context
                FROM topic_sessions
                WHERE chat_id = ? AND thread_id != ?
                ORDER BY created_at DESC
                """,
                (chat_id, GENERAL_THREAD_ID),
            ).fetchall()
        ]

    def is_auto_named(self, chat_id: int, thread_id: int | None) -> bool:
        """Whether automatic topic naming has already been applied or skipped."""
        marker = self._db.execute(
            "SELECT 1 FROM topic_auto_names WHERE chat_id = ? AND thread_id = ?",
            (chat_id, self.thread_key(thread_id)),
        ).fetchone()
        return marker is not None

    def mark_auto_named(self, chat_id: int, thread_id: int | None) -> None:
        """Record that this topic should not be auto-renamed again."""
        self._db.execute(
            """
            INSERT INTO topic_auto_names (chat_id, thread_id)
            VALUES (?, ?)
            ON CONFLICT (chat_id, thread_id) DO NOTHING
            """,
            (chat_id, self.thread_key(thread_id)),
        )
        self._db.commit()

    def get_overrides(
        self, chat_id: int, thread_id: int | None
    ) -> tuple[str | None, str | None, str | None]:
        """Return ``(provider, model, effort)`` overrides for a topic."""
        row = self._db.execute(
            """
            SELECT model_provider, model, effort
            FROM topic_overrides
            WHERE chat_id = ? AND thread_id = ?
            """,
            (chat_id, self.thread_key(thread_id)),
        ).fetchone()
        return (row[0], row[1], row[2]) if row else (None, None, None)

    def set_model_override(
        self, chat_id: int, thread_id: int | None, provider: str, model: str
    ) -> None:
        """Set this topic's model override."""
        self._db.execute(
            """
            INSERT INTO topic_overrides (chat_id, thread_id, model_provider, model)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (chat_id, thread_id)
            DO UPDATE SET
                model_provider = excluded.model_provider,
                model = excluded.model
            """,
            (chat_id, self.thread_key(thread_id), provider, model),
        )
        self._db.commit()

    def reset_model_override(self, chat_id: int, thread_id: int | None) -> None:
        """Clear this topic's model override."""
        self._db.execute(
            """
            UPDATE topic_overrides
            SET model_provider = NULL, model = NULL
            WHERE chat_id = ? AND thread_id = ?
            """,
            (chat_id, self.thread_key(thread_id)),
        )
        self._delete_empty_override(chat_id, thread_id)
        self._db.commit()

    def set_effort_override(self, chat_id: int, thread_id: int | None, effort: str) -> None:
        """Set this topic's effort override."""
        self._db.execute(
            """
            INSERT INTO topic_overrides (chat_id, thread_id, effort)
            VALUES (?, ?, ?)
            ON CONFLICT (chat_id, thread_id)
            DO UPDATE SET effort = excluded.effort
            """,
            (chat_id, self.thread_key(thread_id), effort),
        )
        self._db.commit()

    def reset_effort_override(self, chat_id: int, thread_id: int | None) -> None:
        """Clear this topic's effort override."""
        self._db.execute(
            """
            UPDATE topic_overrides
            SET effort = NULL
            WHERE chat_id = ? AND thread_id = ?
            """,
            (chat_id, self.thread_key(thread_id)),
        )
        self._delete_empty_override(chat_id, thread_id)
        self._db.commit()

    def _delete_empty_override(self, chat_id: int, thread_id: int | None) -> None:
        self._db.execute(
            """
            DELETE FROM topic_overrides
            WHERE chat_id = ?
              AND thread_id = ?
              AND model_provider IS NULL
              AND model IS NULL
              AND effort IS NULL
            """,
            (chat_id, self.thread_key(thread_id)),
        )

    def delete(self, chat_id: int, thread_id: int | None) -> None:
        """Drop a mapping, e.g. when its session no longer exists server-side.

        Touches only ``topic_sessions`` — the auto-naming / plan-mode / override
        tables are left intact so a recreated session keeps its name and settings.
        For deleting a topic outright, use :meth:`purge`."""
        self._db.execute(
            "DELETE FROM topic_sessions WHERE chat_id = ? AND thread_id = ?",
            (chat_id, self.thread_key(thread_id)),
        )
        self._db.commit()

    def purge(self, chat_id: int, thread_id: int | None) -> None:
        """Forget a topic entirely — used when the Telegram forum topic itself is
        deleted (``/delete`` picker), so every trace is removed from all three
        per-topic tables."""
        key = self.thread_key(thread_id)
        for table in ("topic_sessions", "topic_auto_names", "topic_overrides"):
            self._db.execute(
                f"DELETE FROM {table} WHERE chat_id = ? AND thread_id = ?",
                (chat_id, key),
            )
        self._db.commit()

    # --- schedules (ADR-0016) -------------------------------------------------

    def add_schedule(
        self,
        *,
        chat_id: int,
        context: str,
        prompt: str,
        kind: str,
        hour: int,
        minute: int,
        days: str | None,
        created_at: int,
    ) -> int:
        """Save a new schedule; return its id. ``days`` is a CSV of Python
        weekday numbers (``Mon=0`` … ``Sun=6``, i.e. :meth:`datetime.date.weekday`)
        or ``None`` for a daily schedule."""
        cursor = self._db.execute(
            """
            INSERT INTO schedules
                (chat_id, context, prompt, kind, hour, minute, days, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (chat_id, context, prompt, kind, hour, minute, days, created_at),
        )
        self._db.commit()
        return int(cursor.lastrowid or 0)

    def list_schedules(self, chat_id: int) -> list[ScheduleRow]:
        """Every schedule in a chat, oldest first (so ids read in order)."""
        return [
            _schedule_row(row)
            for row in self._db.execute(
                f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
        ]

    def all_schedules(self) -> list[ScheduleRow]:
        """Every schedule across every chat — what boot registration walks."""
        return [
            _schedule_row(row)
            for row in self._db.execute(
                f"SELECT {_SCHEDULE_COLUMNS} FROM schedules ORDER BY id"
            ).fetchall()
        ]

    def get_schedule(self, schedule_id: int) -> ScheduleRow | None:
        row = self._db.execute(
            f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        return _schedule_row(row) if row else None

    def delete_schedule(self, schedule_id: int) -> bool:
        """Drop a schedule; ``False`` if it was already gone."""
        cursor = self._db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> bool:
        """Pause or resume a schedule without losing it; ``False`` if unknown."""
        cursor = self._db.execute(
            "UPDATE schedules SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, schedule_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def mark_schedule_run(self, schedule_id: int, when_ms: int) -> None:
        """Stamp a run. Written when the run *starts*, not when its turn ends, so
        a crash mid-turn can't make the missed-run catch-up re-fire the whole
        thing on restart."""
        self._db.execute(
            "UPDATE schedules SET last_run_at = ? WHERE id = ?", (when_ms, schedule_id)
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
