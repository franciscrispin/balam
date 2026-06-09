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

#: Sentinel thread id for the General topic (no ``message_thread_id``).
GENERAL_THREAD_ID = 0


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
        self._migrate_auto_named()
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
    ) -> None:
        """Map a topic to a session (and the context it was created in),
        overwriting any existing mapping — used both for first creation and for
        recreating a session that vanished server-side.

        Auto-naming state is tracked separately (:meth:`mark_auto_named`) and is
        deliberately untouched here, so recreating a vanished session keeps a
        topic's existing name."""
        self._db.execute(
            """
            INSERT INTO topic_sessions (chat_id, thread_id, session_id, created_at, context)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (chat_id, thread_id)
            DO UPDATE SET
                session_id = excluded.session_id,
                created_at = excluded.created_at,
                context = excluded.context
            """,
            (chat_id, self.thread_key(thread_id), session_id, created_at, context),
        )
        self._db.commit()

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

    def delete(self, chat_id: int, thread_id: int | None) -> None:
        """Drop a mapping, e.g. when its session no longer exists server-side."""
        self._db.execute(
            "DELETE FROM topic_sessions WHERE chat_id = ? AND thread_id = ?",
            (chat_id, self.thread_key(thread_id)),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
