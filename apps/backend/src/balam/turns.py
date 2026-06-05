"""Per-topic registry of in-flight agent turns (for ``/cancel``).

``stream_reply`` is launched as a background task per incoming message; we record
that handle here, keyed by ``(chat_id, thread_key)``, so ``/cancel`` can find and
abort the turn running in a topic, and ``/status`` can report whether one is in
flight. A topic maps to one session and runs at most one turn at a time
(ADR-0009), so a single slot per key is enough.

Keying mirrors :class:`balam.store.SessionStore`: the General topic's absent
``message_thread_id`` normalizes to thread id ``0`` so the key is always a
concrete integer pair.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from balam.store import SessionStore


@dataclass
class Turn:
    """A running turn: its streaming task plus what ``/cancel`` needs to abort it
    server-side (the OpenCode session and the context directory scoping it)."""

    task: asyncio.Task[None]
    session_id: str
    directory: str


class TurnRegistry:
    def __init__(self) -> None:
        self._turns: dict[tuple[int, int], Turn] = {}

    @staticmethod
    def _key(chat_id: int, thread_id: int | None) -> tuple[int, int]:
        return (chat_id, SessionStore.thread_key(thread_id))

    def register(
        self,
        chat_id: int,
        thread_id: int | None,
        task: asyncio.Task[None],
        session_id: str,
        directory: str,
    ) -> None:
        """Record the turn now running in a topic (overwriting any stale entry)."""
        self._turns[self._key(chat_id, thread_id)] = Turn(
            task=task, session_id=session_id, directory=directory
        )

    def get(self, chat_id: int, thread_id: int | None) -> Turn | None:
        """The turn currently running in a topic, or ``None`` if idle."""
        return self._turns.get(self._key(chat_id, thread_id))

    def clear(self, chat_id: int, thread_id: int | None, task: asyncio.Task[None]) -> None:
        """Drop the topic's entry once ``task`` finishes — but only if it is still
        the registered one, so a turn that started after this one isn't evicted by
        a late ``finally`` from the older turn."""
        key = self._key(chat_id, thread_id)
        existing = self._turns.get(key)
        if existing is not None and existing.task is task:
            del self._turns[key]
