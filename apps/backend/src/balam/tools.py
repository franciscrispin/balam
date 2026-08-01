"""The canonical tool registry: what tools exist and how each layer spells them.

Balam speaks about the same tool in four places, and each one used to keep its
own copy of the vocabulary — OpenCode's wire names, Balam's permission
categories, the Telegram display labels, and the Claude Agent SDK's spellings.
Adding or renaming a tool meant editing four files, and the SDK↔OpenCode name
mapping existed in exactly one of them, so the others could not be checked
against it.

:data:`REGISTRY` is now the single source of truth. Every consumer derives its
own lookup from it:

===========================================  ==========================
Consumer                                     Derived from
===========================================  ==========================
:mod:`balam.streamer` (display labels)       :data:`DISPLAY_BY_WIRE`
:mod:`balam.agent.claude_sdk_backend`        :data:`WIRE_BY_SDK_NAME`,
                                             :data:`CATEGORY_BY_SDK_NAME`
:mod:`balam.permissions` (ruleset building)  :data:`MUTATING_TOOLS`,
                                             :data:`FILE_PATH_CATEGORIES`
===========================================  ==========================

Two axes happen to share spellings and are kept separate on purpose:

* :class:`Tool` — the lowercase wire *tool names* OpenCode reports on tool parts
  (the ``tool`` field). Used for display and per-tool argument summaries.
* :class:`Permission` — OpenCode's *permission categories* (the ``permission``
  field on a ``permission.asked`` event and the ``permission`` key of a session
  ruleset rule). Enforcement keys on these.

They diverge exactly where it matters: the ``write`` and ``apply_patch`` *tools*
are both gated by the single ``edit`` *permission*; ``external_directory`` /
``question`` are permissions with no matching tool; ``agent`` is a tool alias for
the ``task`` permission. A shared string would hide those distinctions.

Both are :class:`~enum.StrEnum`, so members are drop-in strings: they compare
equal to the raw values OpenCode sends and work as dict keys against
plain-string lookups, so callers never have to coerce.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    WEBSEARCH = "websearch"
    TODOWRITE = "todowrite"
    TASK = "task"
    AGENT = "agent"


class Permission(StrEnum):
    """OpenCode permission categories (the ``permission`` field on a request and
    the ``permission`` key of a ruleset rule).

    Not every member is referenced from code: ``allowed_tools`` entries flow
    through :func:`balam.permissions.parse_allowed_tool` as plain lowercased
    strings, so some members (``lsp``, ``skill``) exist purely to document
    OpenCode's vocabulary — they are matched by raw string, not via the enum.
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


@dataclass(frozen=True)
class ToolSpec:
    """One tool, in every spelling Balam needs.

    ``permission`` is deliberately *not* always ``Tool``-shaped: several distinct
    tools collapse onto one permission category (all three file mutations gate on
    ``edit``), which is the asymmetry this registry exists to record.
    """

    #: OpenCode's wire name — what arrives on a tool part's ``tool`` field.
    wire: Tool
    #: Label shown in the Telegram tool stream.
    display: str
    #: Permission category enforcement keys on.
    permission: Permission
    #: Claude Agent SDK spellings that mean this tool. Several map to one entry
    #: (``MultiEdit``/``NotebookEdit`` are both ``edit``); an empty tuple means
    #: the SDK has no equivalent and the tool is OpenCode-only.
    sdk_names: tuple[str, ...] = ()


#: Every tool Balam knows about. Order is presentational only.
REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(Tool.BASH, "Bash", Permission.BASH, ("Bash",)),
    ToolSpec(Tool.READ, "Read", Permission.READ, ("Read",)),
    ToolSpec(Tool.EDIT, "Edit", Permission.EDIT, ("Edit", "MultiEdit", "NotebookEdit")),
    ToolSpec(Tool.WRITE, "Write", Permission.EDIT, ("Write",)),
    # OpenCode-only: the SDK has no apply_patch, but OpenCode reports it and it
    # gates on the same edit permission as the other two mutations.
    ToolSpec(Tool.APPLY_PATCH, "ApplyPatch", Permission.EDIT),
    ToolSpec(Tool.GLOB, "Glob", Permission.GLOB, ("Glob",)),
    ToolSpec(Tool.GREP, "Grep", Permission.GREP, ("Grep",)),
    ToolSpec(Tool.LIST, "LS", Permission.LIST, ("LS",)),
    ToolSpec(Tool.WEBFETCH, "WebFetch", Permission.WEBFETCH, ("WebFetch",)),
    ToolSpec(Tool.WEBSEARCH, "WebSearch", Permission.WEBSEARCH, ("WebSearch",)),
    ToolSpec(Tool.TODOWRITE, "TodoWrite", Permission.TODOWRITE, ("TodoWrite",)),
    ToolSpec(Tool.TASK, "Task", Permission.TASK, ("Task",)),
    # OpenCode's alias for spawning a subagent; gates on the task permission.
    ToolSpec(Tool.AGENT, "Agent", Permission.TASK),
)

#: Wire name → Telegram display label.
DISPLAY_BY_WIRE: dict[str, str] = {spec.wire: spec.display for spec in REGISTRY}

#: Claude Agent SDK tool name → OpenCode wire name.
WIRE_BY_SDK_NAME: dict[str, str] = {
    sdk_name: spec.wire for spec in REGISTRY for sdk_name in spec.sdk_names
}

#: Claude Agent SDK tool name → permission category. Unknown tools keep their own
#: name so the boundary policy treats them as "ask".
CATEGORY_BY_SDK_NAME: dict[str, str] = {
    sdk_name: spec.permission for spec in REGISTRY for sdk_name in spec.sdk_names
}

#: Tools that mean "let the model change files". OpenCode folds all of them into
#: the single ``edit`` permission, so an ``allowed_tools`` entry naming any one of
#: them normalizes to that category.
MUTATING_TOOLS: frozenset[Tool] = frozenset(
    spec.wire for spec in REGISTRY if spec.permission is Permission.EDIT
)

#: Categories whose rule pattern is a filesystem path (leading slash stripped,
#: ``**`` glob). A bare ``allowed_tools`` entry for one of these is scoped to the
#: workspace directories rather than the whole filesystem. This is a property of
#: the *permission*, not of any one tool, so it is listed rather than derived.
FILE_PATH_CATEGORIES: frozenset[Permission] = frozenset(
    {Permission.READ, Permission.EDIT, Permission.GLOB, Permission.GREP, Permission.LIST}
)
