"""Translate between the Claude Agent SDK's vocabulary and Balam's.

The SDK and OpenCode describe the same actions differently: tool names are
capitalised (``LS`` vs ``list``), input keys differ (``file_path`` vs
``filePath``), and MCP servers are configured in another shape. The streamer,
the permission layer and the approval keyboard are all written against the
OpenCode vocabulary (ADR-0014), so everything crossing that boundary is
converted here rather than inside the backend's message loop.

Tool spellings themselves come from the canonical registry in
:mod:`balam.tools`; this module only applies them.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from balam.mcp_config import parse_mcp_config
from balam.permissions import collapse_mcp_name
from balam.tools import CATEGORY_BY_SDK_NAME, WIRE_BY_SDK_NAME

logger = logging.getLogger(__name__)


def _wire_tool(name: str) -> str:
    """SDK tool name → OpenCode wire name, for display/rendering.

    Aligns with the streamer's renderer, which special-cases ``bash`` etc. by the
    OpenCode vocabulary. Unknown names (MCP tools) fall through unchanged.
    """
    return WIRE_BY_SDK_NAME.get(name, name)


def _category(name: str) -> str:
    """SDK tool name → the permission category :func:`balam.approvals.decide` keys on.

    Unknown tools keep their own name, so the boundary policy treats them as "ask".
    """
    # MCP tools evaluate against the same ruleset OpenCode ships, which keys them
    # by the collapsed ``server_tool`` form. The SDK hands us the qualified
    # ``mcp__server__tool`` name, so collapse it the same way (shared with
    # :func:`balam.permissions.parse_allowed_tool`) or no ``allowed_tools`` MCP
    # entry could ever match (it would always fall through to "ask").
    return collapse_mcp_name(name) or CATEGORY_BY_SDK_NAME.get(name, name)


def _normalize_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Bridge the SDK's input keys to the OpenCode shape the streamer expects.

    The streamer's path/boundary logic reads ``filePath`` (OpenCode's camelCase);
    the SDK uses ``file_path``. Mirror it so reads/edits resolve and render.
    """
    if "file_path" in tool_input and "filePath" not in tool_input:
        out = dict(tool_input)
        out["filePath"] = out["file_path"]
        return out
    return tool_input


def coerce_sdk_mcp_config(name: str, raw_config: Any) -> dict[str, Any] | None:
    """Normalise one context MCP server entry into the SDK's ``mcp_servers`` shape.

    Parsing/validation lives in :func:`balam.mcp_config.parse_mcp_config` (shared
    with :func:`balam.opencode.coerce_mcp_config`); this projects the spec onto
    the SDK's TypedDicts: stdio ``{"type":"stdio","command","args","env"}`` and
    remote ``{"type":"sse"|"http","url","headers"}``. ``type: remote`` defaults
    to http; OpenCode's ``oauth`` toggle has no SDK counterpart. ``enabled: false``
    returns None — the SDK has no wire toggle, so the disable is honored by not
    registering the server at all.
    """
    spec = parse_mcp_config(name, raw_config)
    if spec.enabled is False:
        return None
    out: dict[str, Any]
    if spec.kind == "local":
        out = {"type": "stdio", "command": spec.command[0]}
        if len(spec.command) > 1:
            out["args"] = list(spec.command[1:])
        if spec.environment:
            out["env"] = spec.environment
        return out
    out = {"type": "sse" if spec.transport == "sse" else "http", "url": spec.url}
    if spec.headers:
        out["headers"] = spec.headers
    return out


def _content_blocks(prompt: str, files: list[Any]) -> str | list[dict[str, Any]]:
    """The user message content: a plain string, or text + attachment blocks.

    ``PromptFile.url`` is a ``data:<mime>;base64,…`` URL; split it into an
    Anthropic image/document source block so the SDK forwards the bytes to the
    model (vision/PDF) without a filesystem read, mirroring OpenCode file parts.
    """
    if not files:
        return prompt
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}] if prompt else []
    for file in files:
        data = file.url.split("base64,", 1)[-1]
        kind = "image" if file.mime.startswith("image/") else "document"
        blocks.append(
            {
                "type": kind,
                "source": {"type": "base64", "media_type": file.mime, "data": data},
            }
        )
    return blocks


def _is_resumable(session_id: str | None) -> bool:
    """Whether ``session_id`` can be passed to the SDK's ``--resume``.

    SDK sessions are UUIDs; the CLI hard-errors on anything else. A topic carried
    over from the OpenCode backend has a ``ses_…`` id, so we must NOT resume it —
    omitting resume starts a fresh SDK session, and the streamer persists the new
    id, transparently rebinding the topic on its first turn after a backend switch.
    """
    if not session_id:
        return False
    try:
        uuid.UUID(session_id)
    except ValueError:
        return False
    return True


def _eval_target(category: str, tool_input: dict[str, Any]) -> str:
    """The resource a tool call acts on, for :func:`evaluate` (leading slash
    stripped to match ``build_ruleset``'s file-path patterns)."""
    if category == "bash":
        return tool_input.get("command") or "*"
    path = tool_input.get("filePath") or tool_input.get("path")
    if isinstance(path, str) and path:
        return path[1:] if path.startswith("/") else path
    return "*"
