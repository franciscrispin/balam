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
        self._db.commit()

    @staticmethod
    def thread_key(thread_id: int | None) -> int:
        """Normalize an optional ``message_thread_id`` to a concrete key."""
        return GENERAL_THREAD_ID if thread_id is None else thread_id

    def get(self, chat_id: int, thread_id: int | None) -> str | None:
        """Resolve the session for a topic, or ``None`` if none is mapped yet."""
        row = self._db.execute(
            "SELECT session_id FROM topic_sessions WHERE chat_id = ? AND thread_id = ?",
            (chat_id, self.thread_key(thread_id)),
        ).fetchone()
        return row[0] if row else None

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
        recreating a session that vanished server-side."""
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

    def delete(self, chat_id: int, thread_id: int | None) -> None:
        """Drop a mapping, e.g. when its session no longer exists server-side."""
        self._db.execute(
            "DELETE FROM topic_sessions WHERE chat_id = ? AND thread_id = ?",
            (chat_id, self.thread_key(thread_id)),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
