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


#: Anthropic's ``base64`` document source accepts **only** ``application/pdf`` —
#: a CSV sent that way is rejected by the API before the turn starts. Text files
#: travel as a ``text`` source instead, whose media type is always this.
_TEXT_DOCUMENT_MEDIA_TYPE = "text/plain"

#: Cap on how much of a text attachment is inlined. Telegram hands the bot
#: documents up to 20 MB; inlined verbatim that is millions of tokens, so the
#: turn would die on the context window instead of the media type. The prefix is
#: still useful for a CSV (headers + a sample), and the truncation is announced
#: inside the document so the agent knows it is not reading the whole file.
_MAX_TEXT_DOCUMENT_CHARS = 256 * 1024


def _as_text(data: bytes) -> str | None:
    """The attachment's bytes as text, or ``None`` if it is not a text file.

    Sniffing beats trusting the MIME type: Telegram reports whatever the sending
    client guessed, so one ``.csv`` arrives as ``text/csv``, another as
    ``application/vnd.ms-excel``, and a third as ``application/octet-stream``. A
    NUL byte is the cheap binary tell; UTF-8 decoding is the rest of the test.
    """
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


#: The only image types the Anthropic API decodes. Matching ``image/*`` instead
#: would 400 the whole turn on the formats it rejects — HEIC above all, which is
#: exactly what an iPhone sends when a photo is attached as a file.
_INLINE_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def _human_size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"


def _attachment_block(file: Any) -> tuple[dict[str, Any] | None, str]:
    """One ``PromptFile`` as ``(content block, manifest line)``.

    The block is what the *model* consumes directly and only exists for the three
    shapes the API decodes — a supported image, a PDF, or text. The manifest line
    exists for every attachment, because everything else (a video, a spreadsheet,
    an archive) is reachable only as a file the agent opens with its own tools,
    and it cannot open a file it was never told about.

    Nothing unsupported is ever emitted as a block: one bad source fails the
    whole turn at the API's schema check, before the agent sees the message.
    """
    name = file.filename or "attachment"
    raw = file.data
    where = f" — saved at {file.path}" if file.path else ""

    if file.error:
        return None, f"- {name} ({file.mime}) — NOT AVAILABLE: {file.error}"

    if file.mime in _INLINE_IMAGE_MIMES:
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": file.mime, "data": _payload(file)},
        }
        return block, f"- {name} ({file.mime}, {_human_size(len(raw))}) — shown above{where}"

    if file.mime == "application/pdf":
        source = {"type": "base64", "media_type": "application/pdf", "data": _payload(file)}
        return _document(source, name), (
            f"- {name} ({file.mime}, {_human_size(len(raw))}) — attached above{where}"
        )

    text = _as_text(raw)
    # An empty document source is not worth risking on the API's validator, and a
    # zero-byte file has nothing to show anyway — point at it and move on.
    if not text:
        logger.info("attachment %r (%s, %d bytes) is not inlineable", name, file.mime, len(raw))
        reach = f"read it from {file.path}" if file.path else "it could not be saved to disk"
        return None, f"- {name} ({file.mime}, {_human_size(len(raw))}) — {reach}"

    whole = len(text)
    if whole > _MAX_TEXT_DOCUMENT_CHARS:
        text = (
            text[:_MAX_TEXT_DOCUMENT_CHARS]
            + f"\n\n[Truncated: {whole - _MAX_TEXT_DOCUMENT_CHARS} of {whole} characters omitted.]"
        )
        shown = f"first {_MAX_TEXT_DOCUMENT_CHARS} of {whole} characters attached above"
    else:
        shown = "attached above in full"
    source = {"type": "text", "media_type": _TEXT_DOCUMENT_MEDIA_TYPE, "data": text}
    return _document(
        source, name
    ), f"- {name} ({file.mime}, {_human_size(len(raw))}) — {shown}{where}"


def _payload(file: Any) -> str:
    """The base64 half of the attachment's ``data:`` URL."""
    return file.url.split("base64,", 1)[-1]


def _document(source: dict[str, Any], name: str) -> dict[str, Any]:
    """A ``document`` block titled with the attachment's filename, so the agent
    can refer to it by the name the user sent."""
    return {"type": "document", "source": source, "title": name}


def _content_blocks(prompt: str, files: list[Any]) -> str | list[dict[str, Any]]:
    """The user message content: a plain string, or text + attachment blocks.

    What the API can decode is inlined so the model sees it without spending a
    tool call. Everything else is listed in a closing manifest naming the path it
    was saved to, which is what lets the agent handle types it cannot be *shown* —
    spreadsheets, archives, audio, video — by opening them with its own tools.
    Per-file shape: :func:`_attachment_block`.
    """
    if not files:
        return prompt
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}] if prompt else []
    manifest: list[str] = []
    for file in files:
        block, line = _attachment_block(file)
        if block is not None:
            blocks.append(block)
        manifest.append(line)
    blocks.append(
        {"type": "text", "text": "[Attachments in this message:\n" + "\n".join(manifest) + "]"}
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
