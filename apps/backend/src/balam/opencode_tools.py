"""OpenCode's tool and permission vocabularies, in one place.

Two distinct axes that happen to share spellings:

* :class:`Tool` — the lowercase wire *tool names* OpenCode reports on tool parts
  (the ``tool`` field). Used for display and per-tool argument summaries.
* :class:`Permission` — OpenCode's *permission categories* (the ``permission``
  field on a ``permission.asked`` event and the ``permission`` key of a session
  ruleset rule). Enforcement keys on these.

They diverge exactly where it matters: the ``write`` and ``apply_patch`` *tools*
are both gated by the single ``edit`` *permission*; ``external_directory`` /
``question`` are permissions with no matching tool; ``agent`` is a tool alias for
the ``task`` permission. Keeping the two enums separate keeps those distinctions
explicit instead of hiding them behind a shared string.

Both are :class:`~enum.StrEnum`, so members are drop-in strings: they compare
equal to the raw values OpenCode sends and work as dict keys against
plain-string lookups, so callers never have to coerce.
"""

from __future__ import annotations

from enum import StrEnum


class Tool(StrEnum):
    """OpenCode wire tool names (the ``tool`` field on a tool part)."""

    BASH = "bash"
    READ = "read"
    EDIT = "edit"
    WRITE = "write"
    APPLY_PATCH = "apply_patch"
    GLOB = "glob"
    GREP = "grep"
    LIST = "list"
    WEBFETCH = "webfetch"
    TODOWRITE = "todowrite"
    TASK = "task"
    AGENT = "agent"


class Permission(StrEnum):
    """OpenCode permission categories (the ``permission`` field on a request and
    the ``permission`` key of a ruleset rule).

    Not every member is referenced from code: ``allowed_tools`` entries flow
    through :func:`balam.permissions.parse_allowed_tool` as plain lowercased
    strings, so some members (``bash``, ``webfetch``, ``websearch``, ``skill``)
    exist purely to document OpenCode's vocabulary — they are matched by raw
    string, not enforced via the enum.
    """

    READ = "read"
    EDIT = "edit"
    GLOB = "glob"
    GREP = "grep"
    LIST = "list"
    BASH = "bash"
    LSP = "lsp"
    TASK = "task"
    WEBFETCH = "webfetch"
    WEBSEARCH = "websearch"
    QUESTION = "question"
    TODOWRITE = "todowrite"
    EXTERNAL_DIRECTORY = "external_directory"
    SKILL = "skill"
    PLAN_ENTER = "plan_enter"
    PLAN_EXIT = "plan_exit"
