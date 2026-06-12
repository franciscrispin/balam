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

    session_id: str
    context_name: str
    directory: str
    provider: str | None
    model: str | None
    effort: str | None
    #: Extra directories the context grants access to, beyond ``directory``;
    #: forwarded to the approval layer's directory boundary (ADR-0012).
    additional_directories: list[str]


class Router:
    def __init__(
        self,
        store: SessionStore,
        opencode: OpenCode,
        contexts: ContextsConfig,
        *,
        tool_scopes: ToolScopes | None = None,
        mcp_base_url: str | None = None,
        qualify_chat: bool = False,
    ) -> None:
        self._store = store
        self._opencode = opencode
        self._contexts = contexts
        self._tool_scopes = tool_scopes
        self._mcp_base_url = mcp_base_url
        self._qualify_chat = qualify_chat

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
        """The OpenCode session a topic maps to, or ``None`` if it has none yet
        (no message has been sent in it). Used by ``/status``."""
        row = self._store.get_row(ref.chat_id, ref.thread_id)
        return row[0] if row else None

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
        mcp, permission, _ = self._session_setup(ctx.mcp, build_ruleset(ctx), chat_id, thread_id)
        session_id = await self._opencode.create_session(
            title, directory=ctx.directory, permission=permission, mcp=mcp
        )
        self._store.set(chat_id, thread_id, session_id, int(time.time() * 1000), context=name)
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
        mcp, permission, balam_server = self._session_setup(
            ctx.mcp, build_ruleset(ctx), ref.chat_id, ref.thread_id
        )

        existing = row[0] if row else None
        if existing and await self._opencode.session_exists(existing, directory=ctx.directory):
            session_id = existing
            await self._opencode.update_session_permission(
                session_id, directory=ctx.directory, permission=permission
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
        else:
            if existing:
                # Mapped session is gone server-side: clear the stale row, recreate.
                # The auto-naming marker is kept (it lives in its own table), so the
                # topic's name carries across the recreate.
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
            )

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
        )
