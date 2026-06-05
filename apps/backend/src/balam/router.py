"""Route a Telegram topic to its OpenCode session, within a context (ADR-0009).

One forum topic ↔ one session, scoped to one named context (a workspace
directory + model/effort, see :mod:`balam.contexts`). The first message in a
topic lazily creates a session in that context's directory; later messages
continue it. If a previously mapped session has vanished server-side (e.g. the
OpenCode server's data was wiped), recreate it rather than silently dropping the
message. The General topic is just thread key 0 — the catch-all session —
handled transparently by the store.

A topic's context is fixed for its lifetime: it is whatever was persisted with
its row when the topic was created; an unbound topic (e.g. General) uses
``default_context``. Switching context does not rebind a topic — ``/context
<name>`` creates a *new* topic bound to ``<name>`` (see :meth:`Router.create_topic_session`)
— so a topic's session always remembers its own conversation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from balam.contexts import ContextsConfig
from balam.opencode import OpenCode
from balam.store import SessionStore


@dataclass
class TopicRef:
    chat_id: int
    #: ``message_thread_id``, or ``None`` for the General topic.
    thread_id: int | None
    #: Title for a freshly created session (topic name, or a fallback).
    title: str


@dataclass
class ResolvedSession:
    """Everything the streamer needs to prompt a topic's session in context."""

    session_id: str
    directory: str
    provider: str | None
    model: str | None
    effort: str | None


class Router:
    def __init__(self, store: SessionStore, opencode: OpenCode, contexts: ContextsConfig) -> None:
        self._store = store
        self._opencode = opencode
        self._contexts = contexts

    @property
    def contexts(self) -> ContextsConfig:
        return self._contexts

    def current_context_name(self, ref: TopicRef) -> str:
        """The context name a topic is bound to (or the default if unbound)."""
        row = self._store.get_row(ref.chat_id, ref.thread_id)
        return self._contexts.resolve_name(row[1] if row else None)

    def current_session_id(self, ref: TopicRef) -> str | None:
        """The OpenCode session a topic maps to, or ``None`` if it has none yet
        (no message has been sent in it). Used by ``/status``."""
        row = self._store.get_row(ref.chat_id, ref.thread_id)
        return row[0] if row else None

    def clear_session(self, ref: TopicRef) -> None:
        """Drop a topic's session mapping so the next message lazily creates a
        fresh one in the same context (used by ``/new``). The old OpenCode session
        is left orphaned server-side but unreferenced — consistent with the
        lazy-create model in :meth:`resolve`."""
        self._store.delete(ref.chat_id, ref.thread_id)

    async def create_topic_session(
        self, chat_id: int, thread_id: int | None, title: str, name: str
    ) -> str:
        """Provision a fresh session for a *newly created* topic bound to context
        ``name``.

        Switching context never rebinds an existing topic; instead the bot
        creates a brand-new forum topic (a Telegram concern, so the caller does
        that) and asks the router to start its session here. One context per
        topic for the topic's whole life — so a topic's session always remembers
        its own history, and there are no orphaned sessions to clean up. The
        caller validates that ``name`` exists.
        """
        ctx = self._contexts.contexts[name]
        session_id = await self._opencode.create_session(title, directory=ctx.directory)
        self._store.set(chat_id, thread_id, session_id, int(time.time() * 1000), context=name)
        return session_id

    async def resolve(self, ref: TopicRef) -> ResolvedSession:
        """Resolve the session for a topic, creating or recreating as needed,
        and return it together with its context's directory and model/effort."""
        row = self._store.get_row(ref.chat_id, ref.thread_id)
        bound_name = row[1] if row else None
        ctx = self._contexts.get(bound_name)
        context_name = self._contexts.resolve_name(bound_name)

        existing = row[0] if row else None
        if existing and await self._opencode.session_exists(existing, directory=ctx.directory):
            session_id = existing
        else:
            if existing:
                # Mapped session is gone server-side: clear the stale row, recreate.
                self._store.delete(ref.chat_id, ref.thread_id)
            session_id = await self._opencode.create_session(ref.title, directory=ctx.directory)
            self._store.set(
                ref.chat_id,
                ref.thread_id,
                session_id,
                int(time.time() * 1000),
                context=context_name,
            )

        provider, model = ctx.provider_model
        return ResolvedSession(
            session_id=session_id,
            directory=ctx.directory,
            provider=provider,
            model=model,
            effort=ctx.effort,
        )
