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
from typing import Any

from balam.agent_tools import ToolScopes, server_name
from balam.contexts import ContextsConfig
from balam.opencode import OpenCode
from balam.permissions import build_ruleset, send_file_rules
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

    #: ``None`` when a lazily-minted backend (the Claude Agent SDK) hasn't created
    #: the session yet — the streamer persists the real id on its first turn.
    session_id: str | None
    context_name: str
    directory: str
    provider: str | None
    model: str | None
    effort: str | None
    #: Extra directories the context grants access to, beyond ``directory``;
    #: forwarded to the approval layer's directory boundary (ADR-0012).
    additional_directories: list[str]
    #: The context's tool opt-ins and MCP servers, forwarded to the turn so a
    #: stateless backend (SDK) can configure each query; the OpenCode backend
    #: already applied these at session creation and ignores them per turn.
    allowed_tools: list[str]
    mcp: dict[str, Any]


class Router:
    def __init__(
        self,
        store: SessionStore,
        opencode: OpenCode | None,
        contexts: ContextsConfig,
        *,
        tool_scopes: ToolScopes | None = None,
        mcp_base_url: str | None = None,
        qualify_chat: bool = False,
    ) -> None:
        self._store = store
        # ``None`` for the Claude Agent SDK backend, which mints sessions lazily
        # per turn rather than eagerly here; the router then only maps rows.
        self._opencode = opencode
        self._contexts = contexts
        self._tool_scopes = tool_scopes
        self._mcp_base_url = mcp_base_url
        self._qualify_chat = qualify_chat

    @property
    def _eager(self) -> bool:
        """Whether the backend creates sessions up front (OpenCode) vs lazily
        on the first turn (the SDK)."""
        return self._opencode is not None

    @property
    def contexts(self) -> ContextsConfig:
        return self._contexts

    def _balam_tool_server(
        self, chat_id: int, thread_id: int | None
    ) -> tuple[str, dict[str, Any]] | None:
        """This topic's own MCP server (Balam's ``send_file``), or ``None`` unwired.

        Per-topic server names + secret token URLs: OpenCode's MCP registry is
        name-keyed per directory (re-registration overwrites), gives servers no
        session identity, and exposes every registered server to every session in
        the directory — so each topic gets its own name, and
        :func:`balam.permissions.send_file_rules` hides the other topics' copies.
        """
        if self._tool_scopes is None or self._mcp_base_url is None:
            return None
        scope = self._tool_scopes.register(chat_id, thread_id)
        name = server_name(scope, qualify_chat=self._qualify_chat)
        return name, {"type": "remote", "url": f"{self._mcp_base_url}/mcp/{scope.token}"}

    def _session_setup(
        self,
        ctx_mcp: dict[str, Any],
        permission: list[dict[str, str]],
        chat_id: int,
        thread_id: int | None,
    ) -> tuple[dict[str, Any], list[dict[str, str]], tuple[str, dict[str, Any]] | None]:
        """The session's MCP map + ruleset, with Balam's tool server merged in."""
        balam_server = self._balam_tool_server(chat_id, thread_id)
        if balam_server is None:
            return ctx_mcp, permission, None
        name, config = balam_server
        return {**ctx_mcp, name: config}, permission + send_file_rules(name), balam_server

    def current_context_name(self, ref: TopicRef) -> str:
        """The context name a topic is bound to (or the default if unbound)."""
        row = self._store.get_row(ref.chat_id, ref.thread_id)
        return self._contexts.resolve_name(row[1] if row else None)

    def current_session_id(self, ref: TopicRef) -> str | None:
        """The agent session a topic maps to, or ``None`` if it has none yet (no
        message sent, or an SDK topic still awaiting its first turn). Used by
        ``/status``. The empty-string placeholder (an SDK topic created by
        ``/context`` before any turn) reads as ``None``."""
        row = self._store.get_row(ref.chat_id, ref.thread_id)
        return (row[0] or None) if row else None

    def persist_session(self, chat_id: int, thread_id: int | None, session_id: str) -> None:
        """Record the real session id a lazily-minted backend (SDK) returned on a
        topic's first turn, keeping the topic's bound context."""
        row = self._store.get_row(chat_id, thread_id)
        context = self._contexts.resolve_name(row[1] if row else None)
        self._store.set(chat_id, thread_id, session_id, int(time.time() * 1000), context=context)

    def plan_mode(self, chat_id: int, thread_id: int | None) -> bool:
        """Whether the topic's prompts should run OpenCode's plan agent (/plan)."""
        return self._store.is_plan_mode(chat_id, thread_id)

    def set_plan_mode(self, chat_id: int, thread_id: int | None, enabled: bool) -> None:
        """Flip a topic's plan mode — set by ``/plan``, cleared by ``/plan off`` or
        by the plan_exit question being answered "Yes" (the agent then builds)."""
        self._store.set_plan_mode(chat_id, thread_id, enabled)

    def model_override(self, chat_id: int, thread_id: int | None) -> tuple[str | None, str | None]:
        """The topic's explicit model override, or ``(None, None)`` when unset."""
        provider, model, _ = self._store.get_overrides(chat_id, thread_id)
        return provider, model

    def set_model_override(
        self, chat_id: int, thread_id: int | None, provider: str, model: str
    ) -> None:
        """Set this topic's model override."""
        self._store.set_model_override(chat_id, thread_id, provider, model)

    def reset_model_override(self, chat_id: int, thread_id: int | None) -> None:
        """Clear this topic's model override."""
        self._store.reset_model_override(chat_id, thread_id)

    def effort_override(self, chat_id: int, thread_id: int | None) -> str | None:
        """The topic's explicit effort override, or ``None`` when unset."""
        _, _, effort = self._store.get_overrides(chat_id, thread_id)
        return effort

    def set_effort_override(self, chat_id: int, thread_id: int | None, effort: str) -> None:
        """Set this topic's effort override."""
        self._store.set_effort_override(chat_id, thread_id, effort)

    def reset_effort_override(self, chat_id: int, thread_id: int | None) -> None:
        """Clear this topic's effort override."""
        self._store.reset_effort_override(chat_id, thread_id)

    def topic_auto_named(self, ref: TopicRef) -> bool:
        """Whether the topic has already been auto-named, or manually renamed."""
        return self._store.is_auto_named(ref.chat_id, ref.thread_id)

    def mark_topic_auto_named(self, ref: TopicRef) -> None:
        """Prevent future first-message auto-renames for this topic."""
        self._store.mark_auto_named(ref.chat_id, ref.thread_id)

    def set_topic_title(self, chat_id: int, thread_id: int | None, title: str) -> None:
        """Record a topic's current Telegram title (for the ``/delete`` picker)."""
        self._store.set_title(chat_id, thread_id, title)

    def list_topics(self, chat_id: int) -> list[tuple[int, str | None, str | None]]:
        """Mapped topics in a chat as ``(thread_id, title, context)`` (no General)."""
        return self._store.list_topics(chat_id)

    def purge_topic(self, chat_id: int, thread_id: int | None) -> None:
        """Forget a deleted topic across every per-topic table."""
        self._store.purge(chat_id, thread_id)

    async def create_topic_session(
        self,
        chat_id: int,
        thread_id: int | None,
        title: str,
        name: str,
        *,
        auto_named: bool = False,
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
        if self._eager:
            assert self._opencode is not None
            mcp, permission, _ = self._session_setup(
                ctx.mcp, build_ruleset(ctx), chat_id, thread_id
            )
            session_id = await self._opencode.create_session(
                title, directory=ctx.directory, permission=permission, mcp=mcp
            )
        else:
            # The SDK mints the session on the topic's first turn; persist an
            # empty placeholder now so the context binding survives until then.
            session_id = ""
        self._store.set(
            chat_id, thread_id, session_id, int(time.time() * 1000), context=name, title=title
        )
        if auto_named:
            # The topic is created already carrying its name, so its first message
            # must not trigger a first-message auto-rename.
            self._store.mark_auto_named(chat_id, thread_id)
        return session_id

    async def resolve(self, ref: TopicRef) -> ResolvedSession:
        """Resolve the session for a topic, creating or recreating as needed,
        and return it together with its context's directory and model/effort."""
        row = self._store.get_row(ref.chat_id, ref.thread_id)
        bound_name = row[1] if row else None
        ctx = self._contexts.get(bound_name)
        context_name = self._contexts.resolve_name(bound_name)
        existing = (row[0] or None) if row else None

        if self._eager:
            session_id: str | None = await self._resolve_opencode(ref, ctx, context_name, existing)
        else:
            # The SDK mints/resumes the session itself per turn; just forward the
            # persisted id (or None for a topic awaiting its first turn). The
            # streamer persists the real id via persist_session on SessionStarted.
            session_id = existing

        provider, model = ctx.provider_model
        override_provider, override_model, override_effort = self._store.get_overrides(
            ref.chat_id, ref.thread_id
        )
        return ResolvedSession(
            session_id=session_id,
            context_name=context_name,
            directory=ctx.directory,
            provider=override_provider or provider,
            model=override_model or model,
            effort=override_effort or ctx.effort,
            additional_directories=list(ctx.additional_directories),
            allowed_tools=list(ctx.allowed_tools),
            mcp=dict(ctx.mcp),
        )

    async def _resolve_opencode(
        self, ref: TopicRef, ctx: Any, context_name: str, existing: str | None
    ) -> str:
        """Eagerly create or reuse the topic's OpenCode session (and its MCP /
        permission setup), returning the live session id."""
        assert self._opencode is not None
        mcp, permission, balam_server = self._session_setup(
            ctx.mcp, build_ruleset(ctx), ref.chat_id, ref.thread_id
        )
        if existing and await self._opencode.session_exists(existing, directory=ctx.directory):
            await self._opencode.update_session_permission(
                existing, directory=ctx.directory, permission=permission
            )
            if balam_server is not None:
                # OpenCode's MCP registry is in-memory while sessions persist on
                # disk, so a reused session may have lost its tool server to an
                # OpenCode restart. The name is deterministic and the token stable,
                # so re-registering is an idempotent overwrite (cheap: a remote
                # client handshake against our own localhost endpoint). Best-effort
                # like update_session_permission — never blocks the turn.
                name, config = balam_server
                await self._opencode.register_mcp(name, config, directory=ctx.directory)
            return existing
        if existing:
            # Mapped session is gone server-side: clear the stale row, recreate.
            # The auto-naming marker is kept (its own table), so the name carries.
            self._store.delete(ref.chat_id, ref.thread_id)
        session_id = await self._opencode.create_session(
            ref.title, directory=ctx.directory, permission=permission, mcp=mcp
        )
        self._store.set(
            ref.chat_id,
            ref.thread_id,
            session_id,
            int(time.time() * 1000),
            context=context_name,
            title=ref.title,
        )
        return session_id
