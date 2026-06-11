"""Translate a context's ``allowed_tools`` + ``additional_directories`` into a
native OpenCode permission ruleset (ADR-0012).

This is the *opt-in* half of Balam's hybrid enforcement. Tools the user has
pre-approved in ``config.yaml`` become ``allow`` rules in the session ruleset, so
OpenCode runs them without ever raising a ``permission.asked`` event. Everything
the user did *not* pre-approve stays ``ask`` and falls through to Balam's local
approval layer (:mod:`balam.approvals`), which keeps the symlink-safe directory
boundary (``os.path.realpath``) and the human keyboard as the backstop. OpenCode
matches patterns against the *literal* path, so the local check — not a native
rule — is what stops a symlink inside the workspace from escaping it.

Wire format (``PermissionRuleset`` in the OpenCode OpenAPI): a list of
``{"permission", "pattern", "action"}``. OpenCode evaluates the **last matching
rule**, so order matters: an ask-all baseline goes first, blanket allows after.

Pattern formats differ per category — verified live against opencode v1.15.13;
getting one wrong degrades to "ask the human" (safe), never to over-allow:

  * **file-path** categories (``read``/``edit``/``glob``/``grep``/``list``) match
    the candidate path with its **leading slash stripped**, globbed with ``**`` —
    pattern ``"home/user/proj/**"``.
  * **external_directory** (the cross-workspace access gate) matches **with** a
    leading slash and a single-star directory glob — pattern ``"/home/user/proj/*"``.
  * **bash** patterns are command globs, used verbatim — e.g. ``"git *"``.

The file mutations ``edit``/``write``/``apply_patch`` all map to OpenCode's single
``edit`` permission category. A bare mutating entry (no pattern) is **scoped to
the workspace** — one rule per ``directory`` + ``additional_directories`` — so
listing ``Edit`` pre-approves writes *inside* the workspace without opening the
whole filesystem; out-of-workspace writes still prompt.
"""

from __future__ import annotations

from balam.contexts import ContextConfig
from balam.opencode_tools import Permission, Tool

#: Permissions that should never generate Balam approval noise. ``todowrite`` is
#: internal bookkeeping; ``question`` is OpenCode's own interactive question flow
#: and should be handled by OpenCode rather than Balam's tool-approval keyboard.
#: ``plan_enter``/``plan_exit`` are OpenCode's plan-mode switches — the server's
#: headless defaults deny them (hiding the tools from the model, exactly like
#: ``question`` before we allowed it), so without these rules plan mode is
#: unreachable from Telegram. Allowing them lets the build agent enter plan mode
#: autonomously; the human gate stays intact because ``plan_exit`` asks its
#: Yes/No "Build Agent" question through the question service either way.
#: ``task`` (spawning a subagent) is metadata-only — the subagent's own tool
#: calls still evaluate against the permission rules, so allowing the spawn
#: removes "Allow Task?" interruptions (constant during plan-mode exploration)
#: without widening what the agent can actually touch.
ALWAYS_ALLOWED_PERMS: tuple[Permission, ...] = (
    Permission.TODOWRITE,
    Permission.QUESTION,
    Permission.PLAN_ENTER,
    Permission.PLAN_EXIT,
    Permission.TASK,
)

#: ``allowed_tools`` names that mean "let the model edit files". OpenCode folds
#: the edit/write/apply_patch *tools* into one ``edit`` *permission* category, so
#: we normalize them all to that.
MUTATING_INPUT_NAMES = frozenset({Tool.EDIT, Tool.WRITE, Tool.APPLY_PATCH})

#: Categories whose pattern is a filesystem path (leading slash stripped, ``**``
#: glob). A bare entry for one of these is scoped to the workspace directories.
FILE_PATH_CATEGORIES = frozenset(
    {Permission.READ, Permission.EDIT, Permission.GLOB, Permission.GREP, Permission.LIST}
)


def _strip_leading_slash(path: str) -> str:
    return path[1:] if path.startswith("/") else path


def _file_path_pattern(directory: str) -> str:
    """Native ``read``/``edit`` pattern matching everything under *directory*."""
    return _strip_leading_slash(directory).rstrip("/") + "/**"


def _external_directory_pattern(directory: str) -> str:
    """Native ``external_directory`` pattern (leading slash, single-star glob)."""
    return "/" + _strip_leading_slash(directory).rstrip("/") + "/*"


def parse_allowed_tool(entry: str) -> tuple[str | None, str | None]:
    """Parse an ``allowed_tools`` entry into ``(permission, pattern)``.

    Accepts the capitalised hooks form (``LSP``, ``Bash(git *)``) and the
    OpenCode wire form (``lsp``, ``bash(git *)``), lowercasing to a native
    permission name. MCP qualified names (``mcp__server__tool``) collapse to
    OpenCode's ``server_tool`` form. Returns ``(None, None)`` for a blank entry.
    """
    text = entry.strip()
    if not text:
        return None, None
    pattern: str | None = None
    if "(" in text and text.endswith(")"):
        head, _, tail = text.partition("(")
        text = head.strip()
        pattern = tail[:-1].strip() or None
    if text.startswith("mcp__"):
        parts = text.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}_{parts[2]}", pattern
        return text, pattern
    if text.startswith("_"):  # already an OpenCode-internal permission name
        return text, pattern
    return text.lower(), pattern


def _allow(permission: str, pattern: str) -> dict[str, str]:
    # str() so an enum member serializes as a plain string in the wire payload.
    return {"permission": str(permission), "pattern": pattern, "action": "allow"}


def build_ruleset(ctx: ContextConfig) -> list[dict[str, str]]:
    """The native OpenCode permission ruleset for a context.

    Order is load-bearing (OpenCode uses the last matching rule): the ask-all
    baseline first, then the always-allowed internals, then the user's
    ``allowed_tools`` opt-ins, then ``external_directory`` grants for the extra
    directories. A context with no opt-ins reduces to the ask-everything baseline.
    """
    workspace_dirs = [ctx.directory, *ctx.additional_directories]

    rules: list[dict[str, str]] = [{"permission": "*", "pattern": "*", "action": "ask"}]
    for perm in ALWAYS_ALLOWED_PERMS:
        rules.append(_allow(perm, "*"))

    # Plan-mode plan files. OpenCode's plan agent allows edits to its own plan
    # file natively, but session rules are merged *after* agent rules and the
    # last matching rule wins — so without this, our ask-all baseline would make
    # every plan write prompt in Telegram. Edit asks carry the worktree-relative
    # path, and ``*`` crosses slashes, so this matches both the in-repo location
    # (.opencode/plans/*.md) and the upward-relative path to the global plans
    # dir used for non-git directories.
    rules.append(_allow(Permission.EDIT, "*opencode/plans/*.md"))

    for entry in ctx.allowed_tools:
        permission, pattern = parse_allowed_tool(entry)
        if permission is None:
            continue
        if permission in MUTATING_INPUT_NAMES:
            permission = Permission.EDIT
        if permission in FILE_PATH_CATEGORIES:
            if pattern is None:
                # Scope to the workspace (and any additional dirs), not the whole FS.
                for directory in workspace_dirs:
                    rules.append(_allow(permission, _file_path_pattern(directory)))
            else:
                rules.append(_allow(permission, _strip_leading_slash(pattern)))
        else:
            # Command/flag categories (bash, lsp, webfetch, …): pattern verbatim.
            rules.append(_allow(permission, pattern or "*"))

    # Grant cross-workspace access to the extra directories so OpenCode's
    # external_directory gate doesn't prompt for them; the read/edit within them
    # is still bounded locally (allowed_dirs in :mod:`balam.approvals`).
    for directory in ctx.additional_directories:
        rules.append(_allow(Permission.EXTERNAL_DIRECTORY, _external_directory_pattern(directory)))

    return rules


def send_file_rules(server_name: str) -> list[dict[str, str]]:
    """Session rules scoping Balam's per-topic ``send_file`` MCP tool.

    Every topic registers its own MCP server (``balam_t<thread>``), and OpenCode
    exposes *all* servers registered in a directory to *every* session there. A
    rule whose last match is a ``*``-pattern deny removes a tool from the model's
    tool list entirely, so the glob-deny hides the other topics' copies; the
    topic's own tool is then re-allowed (order is load-bearing: last match wins).
    The allow also pre-approves the call — ``send_file`` runs without the
    Telegram keyboard, like open-shrimp.
    """
    return [
        {"permission": "balam_*_send_file", "pattern": "*", "action": "deny"},
        {"permission": f"{server_name}_send_file", "pattern": "*", "action": "allow"},
    ]
