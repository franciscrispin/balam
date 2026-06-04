"""Route a Telegram topic to its OpenCode session (ADR-0009).

One forum topic ↔ one session. The first message in a topic lazily creates a
session; later messages continue it. If a previously mapped session has vanished
server-side (e.g. the OpenCode server's data was wiped), recreate it rather than
silently dropping the message. The General topic is just thread key 0 — the
catch-all session — handled transparently by the store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from balam.opencode import OpenCode
from balam.store import SessionStore


@dataclass
class TopicRef:
    chat_id: int
    #: ``message_thread_id``, or ``None`` for the General topic.
    thread_id: int | None
    #: Title for a freshly created session (topic name, or a fallback).
    title: str


class Router:
    def __init__(self, store: SessionStore, opencode: OpenCode) -> None:
        self._store = store
        self._opencode = opencode

    async def resolve(self, ref: TopicRef) -> str:
        """Resolve the session for a topic, creating or recreating as needed."""
        existing = self._store.get(ref.chat_id, ref.thread_id)
        if existing:
            if await self._opencode.session_exists(existing):
                return existing
            # Mapped session is gone server-side: clear the stale row, recreate.
            self._store.delete(ref.chat_id, ref.thread_id)

        session_id = await self._opencode.create_session(ref.title)
        self._store.set(ref.chat_id, ref.thread_id, session_id, int(time.time() * 1000))
        return session_id
