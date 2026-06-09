"""Interactive tool approval: the directory-boundary decision plus the registry
that bridges OpenCode permission requests to Telegram inline keyboards (ADR-0012,
adapted from the open-shrimp reference).

OpenCode is configured (see :data:`balam.opencode.ASK_ALL_PERMISSIONS`) to raise
a ``permission.asked`` SSE event before each tool call. The streamer runs
:func:`decide` on it and either replies to OpenCode directly (auto-allow) or
sends an inline keyboard and awaits the user's choice via :class:`PendingApprovals`.

Decisions key on OpenCode's **permission category** (the event's ``permission``
field — ``read``, ``edit``, ``bash``, …), not on tool names: OpenCode owns that
taxonomy, and the ``edit`` category covers *every* file mutation (edit, write,
and the multi-file ``apply_patch``), so we can't miss a mutating tool. Target
paths come from the request itself (``metadata`` for edits — authoritative for
apply_patch — and the tool input for reads).

This is the **boundary** half of the hybrid model: reads inside the workspace
auto-allow; mutations inside it auto-allow only once the user has chosen "accept
all edits" for the session; everything else — out-of-scope paths, Bash, network,
unknown categories — prompts. The *opt-in* half (translating ``allowed_tools`` /
``additional_directories`` into native OpenCode ``allow`` rules) lives in
:mod:`balam.permissions`; tools pre-approved there never reach this layer. Human
approval is the backstop for everything else.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any

from balam.opencode_tools import Permission

#: We classify a permission request by OpenCode's own **permission category** (the
#: ``permission`` field on the ``permission.asked`` event), *not* by tool name.
#: This is the authoritative axis: OpenCode maps every tool to one of these
#: categories (see :class:`balam.opencode_tools.Permission`), and crucially the
#: ``edit`` category covers *all* file mutations — the ``edit`` and ``write``
#: tools and the multi-file ``apply_patch`` — so we can't miss a mutating tool the
#: way a hand-maintained tool-name set does (verified live; see
#: ``docs/balam-tier1-implementation-plan.md``).

#: The single category covering edit/write/apply_patch — offers "accept all edits".
EDIT_CATEGORY = Permission.EDIT

#: Read-only categories Balam auto-allows inside the workspace.
READ_CATEGORIES = frozenset(
    {Permission.READ, Permission.GLOB, Permission.GREP, Permission.LIST, Permission.LSP}
)


def is_edit(category: str) -> bool:
    """Whether a permission category is a file mutation (so the prompt should
    offer "accept all edits")."""
    return category == EDIT_CATEGORY


def is_within(path: str, directories: list[str]) -> bool:
    """True if *path* resolves inside any of *directories*.

    Uses ``os.path.realpath`` on both sides so symlinks and ``..`` can't escape
    the boundary, and guards against prefix false positives (``/home/user2`` is
    not within ``/home/user``) by requiring an exact match or a trailing
    separator.
    """
    if not path:
        return False
    real = os.path.realpath(path)
    for directory in directories:
        if not directory:
            continue
        real_dir = os.path.realpath(directory)
        if real == real_dir or real.startswith(real_dir + os.sep):
            return True
    return False


class Verdict(Enum):
    """The outcome of :func:`decide`."""

    ALLOW = "allow"  # auto-approve without asking (reply "once")
    ASK = "ask"  # prompt the user with an inline keyboard


def _resolve(path: str, cwd: str | None) -> str:
    """Make a (possibly relative) path absolute against the workspace, so the
    boundary check resolves it against ``cwd`` — not the bot's process cwd."""
    return path if (os.path.isabs(path) or not cwd) else os.path.join(cwd, path)


def request_target_paths(
    category: str, metadata: dict[str, Any], tool_input: dict[str, Any], cwd: str | None
) -> list[str]:
    """The absolute paths a request touches, for the directory-boundary check.

    Prefers OpenCode's permission ``metadata`` — for an ``edit`` it lists every
    file in the (possibly multi-file ``apply_patch``) envelope as
    ``metadata["files"][i]["filePath"]``, which is authoritative and saves us
    re-parsing the patch. Reads fall back to the tool input's ``filePath`` /
    ``path`` (glob/grep/list default to the workspace). Returns ``[]`` when no
    path can be determined — the caller treats that as "ask", never auto-allow.
    """
    if category == EDIT_CATEGORY:
        files = metadata.get("files")
        if isinstance(files, list):
            paths = [
                f["filePath"]
                for f in files
                if isinstance(f, dict) and isinstance(f.get("filePath"), str)
            ]
            if paths:
                return [_resolve(p, cwd) for p in paths]
        filepath = metadata.get("filepath")
        if isinstance(filepath, str) and filepath:
            return [_resolve(filepath, cwd)]
        # Fall back to a plain edit/write tool input shape.
        file_path = tool_input.get("filePath")
        return [_resolve(str(file_path), cwd)] if file_path else []

    if category in READ_CATEGORIES:
        for key in ("filePath", "path"):
            value = tool_input.get(key)
            if value:
                return [_resolve(str(value), cwd)]
        # glob/grep/list without an explicit path run against the workspace.
        return [cwd] if cwd else []

    return []


def decide(
    category: str,
    target_paths: list[str],
    *,
    allowed_dirs: list[str],
    accept_all_edits: bool,
) -> Verdict:
    """The directory-boundary policy, keyed on OpenCode's permission *category*.

    Reads inside the workspace auto-allow; edits inside it auto-allow only once
    the user has chosen "accept all edits" for the session, and only when *every*
    target is in-workspace (one out-of-scope path still asks); Bash, network, and
    everything else always asks. ``target_paths`` are the absolute paths the
    request touches (see :func:`request_target_paths`).
    """
    if category in READ_CATEGORIES:
        if target_paths and all(is_within(p, allowed_dirs) for p in target_paths):
            return Verdict.ALLOW
        return Verdict.ASK

    if category == EDIT_CATEGORY:
        if not accept_all_edits:
            return Verdict.ASK
        if target_paths and all(is_within(p, allowed_dirs) for p in target_paths):
            return Verdict.ALLOW
        return Verdict.ASK

    # Bash, network, subagents, unknown/MCP categories: always ask.
    return Verdict.ASK


class Choice(StrEnum):
    """A user's answer to an approval prompt, carried in the callback token."""

    ALLOW = "allow"  # allow this one call (reply "once")
    ALL = "all"  # allow + auto-allow edits in-workspace for the session
    DENY = "deny"  # reject the call


@dataclass
class _Pending:
    future: asyncio.Future[Choice]
    session_id: str


class PendingApprovals:
    """App-level registry of outstanding approval prompts and per-session state.

    Maps a short callback token → an :class:`asyncio.Future` the callback handler
    resolves when the user taps a button, and tracks which sessions have switched
    into "accept all edits" so :func:`decide` can auto-allow in-workspace edits.
    Lives for the bot's lifetime (one instance in ``bot_data``) so the flag
    persists across turns within a session.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}
        self._accept_all_edits: set[str] = set()

    def is_accept_all_edits(self, session_id: str) -> bool:
        return session_id in self._accept_all_edits

    def register(self, session_id: str) -> tuple[str, asyncio.Future[Choice]]:
        """Create a pending prompt for ``session_id``; return its callback token
        and the future to await."""
        token = uuid.uuid4().hex[:16]
        future: asyncio.Future[Choice] = asyncio.get_event_loop().create_future()
        self._pending[token] = _Pending(future=future, session_id=session_id)
        return token, future

    def discard(self, token: str) -> None:
        """Forget a prompt once it has been resolved or abandoned."""
        self._pending.pop(token, None)

    def resolve(self, token: str, choice: Choice) -> bool:
        """Resolve a pending prompt from its callback token.

        Records the session's accept-all-edits flag when chosen. Returns ``False``
        if the token is unknown or already resolved (a stale/expired button).
        """
        pending = self._pending.get(token)
        if pending is None or pending.future.done():
            return False
        if choice is Choice.ALL:
            self._accept_all_edits.add(pending.session_id)
        pending.future.set_result(choice)
        return True


@dataclass
class _PendingQuestion:
    futures: list[asyncio.Future[list[str]]]
    labels: list[list[str]]
    session_id: str
    chat_id: int | None = None
    thread_id: int | None = None
    awaiting_custom: set[int] = field(default_factory=set)


class PendingQuestions:
    """Outstanding OpenCode question-tool prompts.

    OpenCode's ``question`` tool is not a permission approval. It emits a
    ``question.asked`` event and expects answers via ``/question/{id}/reply``.
    This registry lets Telegram inline button callbacks resolve each question's
    future while the streamer waits to reply to OpenCode.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingQuestion] = {}

    def register(
        self,
        session_id: str,
        questions: list[list[str]],
        *,
        chat_id: int | None = None,
        thread_id: int | None = None,
    ) -> tuple[str, list[asyncio.Future[list[str]]]]:
        token = uuid.uuid4().hex[:16]
        futures = [asyncio.get_event_loop().create_future() for _ in questions]
        self._pending[token] = _PendingQuestion(
            futures=futures,
            labels=questions,
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return token, futures

    def discard(self, token: str) -> None:
        self._pending.pop(token, None)

    def resolve(self, token: str, question_index: int, option_index: int) -> bool:
        pending = self._pending.get(token)
        if pending is None:
            return False
        if question_index < 0 or question_index >= len(pending.futures):
            return False
        labels = pending.labels[question_index]
        if option_index < 0 or option_index >= len(labels):
            return False
        future = pending.futures[question_index]
        if future.done():
            return False
        future.set_result([labels[option_index]])
        if all(f.done() for f in pending.futures):
            self.discard(token)
        return True

    def await_custom(
        self, token: str, question_index: int, chat_id: int, thread_id: int | None
    ) -> bool:
        pending = self._pending.get(token)
        if pending is None:
            return False
        if pending.chat_id is not None and pending.chat_id != chat_id:
            return False
        if pending.thread_id != thread_id:
            return False
        if question_index < 0 or question_index >= len(pending.futures):
            return False
        if pending.futures[question_index].done():
            return False
        pending.awaiting_custom.add(question_index)
        return True

    def resolve_custom(self, chat_id: int, thread_id: int | None, answer: str) -> bool:
        for token, pending in list(self._pending.items()):
            if pending.chat_id != chat_id or pending.thread_id != thread_id:
                continue
            for question_index in sorted(pending.awaiting_custom):
                future = pending.futures[question_index]
                if future.done():
                    pending.awaiting_custom.discard(question_index)
                    continue
                future.set_result([answer])
                pending.awaiting_custom.discard(question_index)
                if all(f.done() for f in pending.futures):
                    self.discard(token)
                return True
        return False
