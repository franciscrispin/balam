"""Per-topic registry of in-flight agent turns (for ``/cancel``) and the queue of
messages waiting behind them.

``stream_reply`` is launched as a background task per incoming message; we record
that handle here, keyed by ``(chat_id, thread_key)``, so ``/cancel`` can find and
abort the turn running in a topic, and ``/status`` can report whether one is in
flight. A topic maps to one session and runs at most one turn at a time
(ADR-0009), so a single running slot per key is enough.

Because OpenCode runs one turn per session, a message that arrives while a turn
is still streaming must **not** fire a second prompt at the same session — that
collides and silently drops the message. Instead the message is parked in the
topic's FIFO queue (:class:`TurnJob`) and run when the current turn finishes.

Keying mirrors :class:`balam.store.SessionStore`: the General topic's absent
``message_thread_id`` normalizes to thread id ``0`` so the key is always a
concrete integer pair.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from balam.attachments import PromptFile
from balam.store import SessionStore


@dataclass
class Turn:
    """A running turn: its streaming task plus what ``/cancel`` needs to abort it
    server-side (the OpenCode session and the context directory scoping it)."""

    task: asyncio.Task[None]
    session_id: str
    directory: str


@dataclass
class TurnJob:
    """A queued message waiting to run as a turn. Everything ``stream_reply`` needs
    is captured at enqueue time (the session is already resolved) so draining the
    queue is synchronous — the running slot is handed to the next job without an
    ``await`` in between, leaving no window for a concurrent message to slip a
    second turn onto the same session.

    Deliberately *not* captured: the plan-agent choice. ``_start_turn`` derives it
    from the topic's plan-mode flag when the job actually runs, so a message
    queued behind a turn respects a plan approval or ``/plan off`` that happened
    while it waited."""

    prompt: str
    session_id: str
    directory: str
    provider: str | None
    model: str | None
    effort: str | None
    allowed_dirs: list[str]
    files: list[PromptFile]


class TurnRegistry:
    def __init__(self) -> None:
        self._turns: dict[tuple[int, int], Turn] = {}
        self._queues: dict[tuple[int, int], deque[TurnJob]] = {}

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

    def enqueue(self, chat_id: int, thread_id: int | None, job: TurnJob) -> int:
        """Append ``job`` to the topic's queue; return its 1-based position."""
        queue = self._queues.setdefault(self._key(chat_id, thread_id), deque())
        queue.append(job)
        return len(queue)

    def pop_next(self, chat_id: int, thread_id: int | None) -> TurnJob | None:
        """Remove and return the topic's next queued job, or ``None`` if empty."""
        key = self._key(chat_id, thread_id)
        queue = self._queues.get(key)
        if not queue:
            return None
        job = queue.popleft()
        if not queue:
            del self._queues[key]
        return job

    def queue_len(self, chat_id: int, thread_id: int | None) -> int:
        """How many messages are queued behind the topic's running turn."""
        queue = self._queues.get(self._key(chat_id, thread_id))
        return len(queue) if queue else 0

    def clear_queue(self, chat_id: int, thread_id: int | None) -> int:
        """Drop every queued job for a topic; return how many were dropped."""
        queue = self._queues.pop(self._key(chat_id, thread_id), None)
        return len(queue) if queue else 0
