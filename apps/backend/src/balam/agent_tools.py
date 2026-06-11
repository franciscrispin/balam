"""Balam's agent-facing tools, served to OpenCode as a remote MCP server.

OpenCode reaches these over plain HTTP JSON-RPC (the MCP "remote" transport):
:mod:`balam.server` exposes ``POST /mcp/{scope_token}`` and delegates to
:func:`handle_rpc`. Three pieces live here:

* :class:`AgentTool` + :func:`create_send_file_tool` — the tool definitions,
  transport-neutral. ``send_file`` delivers a local file to the Telegram topic;
  markdown files get a "📖 Preview" button opening the Mini App viewer on a
  stored snapshot. The tool description is the only prompting (open-shrimp
  parity — no system-prompt nudge).
* :class:`ToolScopes` — scope tokens binding an MCP URL to one ``(chat_id,
  thread_id)``. OpenCode gives MCP servers no session identity, and its
  registry is name-keyed per directory with overwrite-on-reregister, so each
  topic registers its **own** server (``balam_t<thread>``) pointing at its own
  token URL; the token doubles as the endpoint's auth (the FastAPI app is
  tunnel-exposed). Names are deterministic so a Balam restart's re-registration
  overwrites the stale entry instead of orphaning it. With the bot scoped to a
  single chat (``ALLOWED_TELEGRAM_CHAT_ID``, the documented norm) the thread id
  alone is unique; unscoped multi-chat use qualifies the name with the chat id.
* :func:`handle_rpc` — the minimal JSON-RPC 2.0 server: ``initialize``,
  ``notifications/initialized``, ``tools/list``, ``tools/call``.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardMarkup

from balam.config import Config
from balam.content_store import ContentStore
from balam.miniapp import markdown_button
from balam.telegram_utils import thread_kwargs

logger = logging.getLogger(__name__)

MAX_DOCUMENT_SIZE = 50 * 1024 * 1024  # Telegram bot-API upload cap
MAX_PHOTO_SIZE = 10 * 1024 * 1024
MAX_CAPTION_LEN = 1024
PHOTO_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

#: MCP protocol revision spoken by :func:`handle_rpc`.
PROTOCOL_VERSION = "2024-11-05"

SEND_FILE_DESCRIPTION = (
    "Send a file to the user via Telegram. The file must exist on the local "
    "filesystem. Images under 10 MB are sent as inline photos unless type is set "
    "to 'document'. Markdown files include a button that opens a rendered preview."
)

SEND_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "Absolute path to the file to send.",
        },
        "caption": {"type": "string", "description": "Optional caption."},
        "type": {"type": "string", "enum": ["auto", "photo", "document"]},
    },
    "required": ["file_path"],
}


@dataclass(frozen=True)
class AgentTool:
    """One tool definition: MCP wire fields plus its async handler."""

    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """An MCP ``tools/call`` result carrying one text block."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def create_send_file_tool(
    bot: Any,
    chat_id: int,
    thread_id: int | None,
    *,
    config: Config,
    bot_username: str | None,
    content_store: ContentStore,
) -> AgentTool:
    """The ``send_file`` tool bound to one Telegram topic."""

    async def send_file(args: dict[str, Any]) -> dict[str, Any]:
        file_path = args.get("file_path", "")
        caption = args.get("caption")
        send_type = args.get("type", "auto")
        if not file_path:
            return _text_result("Error: file_path is required.", is_error=True)
        if send_type not in ("auto", "photo", "document"):
            return _text_result(
                f"Error: type must be 'auto', 'photo', or 'document', not {send_type!r}.",
                is_error=True,
            )
        path = os.path.abspath(str(file_path))
        if not os.path.isfile(path):
            return _text_result(f"Error: File not found: {path}", is_error=True)
        size = os.path.getsize(path)
        if size == 0:
            return _text_result("Error: File is empty.", is_error=True)
        if size > MAX_DOCUMENT_SIZE:
            mb = size / (1024 * 1024)
            return _text_result(
                f"Error: File too large ({mb:.1f} MB). Telegram limit is 50 MB.",
                is_error=True,
            )

        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        use_photo = send_type == "photo" or (
            send_type == "auto" and mime in PHOTO_MIME_TYPES and size <= MAX_PHOTO_SIZE
        )
        if use_photo and size > MAX_PHOTO_SIZE:
            mb = size / (1024 * 1024)
            return _text_result(
                f"Error: Photo too large ({mb:.1f} MB). Telegram limit for photos is "
                "10 MB. Use type='document' for larger images.",
                is_error=True,
            )

        if isinstance(caption, str) and len(caption) > MAX_CAPTION_LEN:
            caption = caption[: MAX_CAPTION_LEN - 1] + "…"

        filename = os.path.basename(path)
        reply_markup = None
        if filename.lower().endswith(".md"):
            # Snapshot the content so the button outlives later edits/deletion
            # (within the store TTL); skip silently when no public URL exists.
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                logger.warning("send_file: could not read %s for preview", path)
            else:
                content_id = content_store.put(filename, text)
                button = markdown_button(config, bot_username, content_id, "📖 Preview")
                if button is not None:
                    reply_markup = InlineKeyboardMarkup([[button]])

        kwargs = thread_kwargs(thread_id)
        try:
            with open(path, "rb") as handle:
                if use_photo:
                    await bot.send_photo(chat_id=chat_id, photo=handle, caption=caption, **kwargs)
                else:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=handle,
                        filename=filename,
                        caption=caption,
                        reply_markup=reply_markup,
                        **kwargs,
                    )
        except Exception as exc:
            logger.exception("send_file: failed to send %s to chat %d", path, chat_id)
            return _text_result(f"Error sending file: {exc}", is_error=True)
        logger.info("send_file: sent %s to chat %d thread %s", path, chat_id, thread_id)
        return _text_result(f"File sent successfully: {filename}")

    return AgentTool(
        name="send_file",
        description=SEND_FILE_DESCRIPTION,
        input_schema=SEND_FILE_SCHEMA,
        read_only=True,
        handler=send_file,
    )


@dataclass(frozen=True)
class ToolScope:
    """One topic's MCP binding: the URL token and where its tools deliver."""

    token: str
    chat_id: int
    thread_id: int | None


class ToolScopes:
    """Scope-token registry: ``(chat_id, thread_id)`` → stable :class:`ToolScope`.

    ``register`` is idempotent per topic so re-resolving a session reuses the
    token already registered with OpenCode (re-registration with the same URL is
    then a harmless overwrite).
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[int, int | None], ToolScope] = {}
        self._by_token: dict[str, ToolScope] = {}

    def register(self, chat_id: int, thread_id: int | None) -> ToolScope:
        key = (chat_id, thread_id)
        scope = self._by_key.get(key)
        if scope is None:
            scope = ToolScope(token=secrets.token_urlsafe(32), chat_id=chat_id, thread_id=thread_id)
            self._by_key[key] = scope
            self._by_token[scope.token] = scope
        return scope

    def get(self, token: str) -> ToolScope | None:
        return self._by_token.get(token)


def server_name(scope: ToolScope, *, qualify_chat: bool) -> str:
    """The OpenCode MCP server name for a scope (→ tool ``<name>_send_file``).

    Deterministic per topic — see the module docstring. ``qualify_chat`` adds the
    chat id when the bot is not scoped to a single chat.
    """
    thread = scope.thread_id if scope.thread_id is not None else 0
    if qualify_chat:
        # Chat ids can be negative (supergroups); keep the name in [a-z0-9_].
        chat = str(scope.chat_id).replace("-", "n")
        return f"balam_c{chat}_t{thread}"
    return f"balam_t{thread}"


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def handle_rpc(body: Any, tools: list[AgentTool]) -> dict[str, Any] | None:
    """Answer one MCP JSON-RPC message; ``None`` means reply 202 (notification)."""
    if not isinstance(body, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    request_id = body.get("id")
    method = body.get("method")
    if not isinstance(method, str):
        return _rpc_error(request_id, -32600, "Invalid Request")

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _rpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "balam", "version": "0.1.0"},
            },
        )
    if method == "tools/list":
        return _rpc_result(
            request_id,
            {
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                        "annotations": {"readOnlyHint": tool.read_only},
                    }
                    for tool in tools
                ]
            },
        )
    if method == "tools/call":
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "Invalid params")
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _rpc_error(request_id, -32602, "Invalid params")
        tool = next((t for t in tools if t.name == name), None)
        if tool is None:
            return _rpc_error(request_id, -32602, f"Unknown tool: {name}")
        try:
            return _rpc_result(request_id, await tool.handler(args))
        except Exception as exc:
            logger.exception("agent tool %s failed", name)
            return _rpc_result(request_id, _text_result(f"Error: {exc}", is_error=True))
    return _rpc_error(request_id, -32601, f"Method not found: {method}")
