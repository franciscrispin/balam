"""Tests for the agent-facing send_file tool and the MCP JSON-RPC layer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from balam.agent_tools import (
    MAX_DOCUMENT_SIZE,
    MAX_PHOTO_SIZE,
    AgentTool,
    ToolScopes,
    create_send_file_tool,
    handle_rpc,
    server_name,
)
from balam.config import Config
from balam.content_store import ContentStore


class FakeBot:
    def __init__(self) -> None:
        self.photos: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []

    async def send_photo(self, **kwargs: Any) -> None:
        self.photos.append(kwargs)

    async def send_document(self, **kwargs: Any) -> None:
        self.documents.append(kwargs)


def _tool(
    make_config: Callable[..., Config],
    bot: FakeBot,
    store: ContentStore | None = None,
    *,
    thread_id: int | None = 7,
    public: bool = True,
) -> AgentTool:
    overrides: dict[str, Any] = {}
    if public:
        overrides = {
            "balam_public_url": "https://balam.example.com",
            "balam_miniapp_shortname": "balamapp",
        }
    return create_send_file_tool(
        bot,
        chat_id=100,
        thread_id=thread_id,
        config=make_config(**overrides),
        bot_username="balam_bot",
        content_store=store if store is not None else ContentStore(),
    )


def _is_error(result: dict[str, Any]) -> bool:
    return result.get("isError", False)


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


# --- send_file handler ---


async def test_send_file_requires_file_path(make_config: Callable[..., Config]) -> None:
    tool = _tool(make_config, FakeBot())
    result = await tool.handler({})
    assert _is_error(result)
    assert "file_path is required" in _text(result)


async def test_send_file_missing_file(make_config: Callable[..., Config], tmp_path: Path) -> None:
    tool = _tool(make_config, FakeBot())
    result = await tool.handler({"file_path": str(tmp_path / "nope.txt")})
    assert _is_error(result)
    assert "File not found" in _text(result)


async def test_send_file_empty_file(make_config: Callable[..., Config], tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.touch()
    result = await _tool(make_config, FakeBot()).handler({"file_path": str(empty)})
    assert _is_error(result)
    assert "empty" in _text(result)


async def test_send_file_too_large(make_config: Callable[..., Config], tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    with open(big, "wb") as handle:  # sparse — no real disk usage
        handle.truncate(MAX_DOCUMENT_SIZE + 1)
    result = await _tool(make_config, FakeBot()).handler({"file_path": str(big)})
    assert _is_error(result)
    assert "too large" in _text(result)


async def test_send_file_photo_auto(make_config: Callable[..., Config], tmp_path: Path) -> None:
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"\xff\xd8fakejpeg")
    bot = FakeBot()
    result = await _tool(make_config, bot).handler({"file_path": str(img), "caption": "hi"})
    assert not _is_error(result)
    assert len(bot.photos) == 1 and not bot.documents
    assert bot.photos[0]["caption"] == "hi"
    assert bot.photos[0]["chat_id"] == 100
    assert bot.photos[0]["message_thread_id"] == 7


async def test_send_file_photo_as_document_override(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNGfake")
    bot = FakeBot()
    result = await _tool(make_config, bot).handler({"file_path": str(img), "type": "document"})
    assert not _is_error(result)
    assert len(bot.documents) == 1 and not bot.photos


async def test_send_file_explicit_photo_over_cap_errors(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    img = tmp_path / "huge.jpg"
    with open(img, "wb") as handle:
        handle.truncate(MAX_PHOTO_SIZE + 1)
    result = await _tool(make_config, FakeBot()).handler({"file_path": str(img), "type": "photo"})
    assert _is_error(result)
    assert "type='document'" in _text(result)


async def test_send_file_oversized_photo_auto_falls_back_to_document(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    img = tmp_path / "huge.jpg"
    with open(img, "wb") as handle:
        handle.truncate(MAX_PHOTO_SIZE + 1)
    bot = FakeBot()
    result = await _tool(make_config, bot).handler({"file_path": str(img)})
    assert not _is_error(result)
    assert len(bot.documents) == 1 and not bot.photos


async def test_send_file_markdown_gets_preview_button(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    doc = tmp_path / "report.md"
    doc.write_text("# Report\n\nFindings.")
    bot = FakeBot()
    store = ContentStore()
    result = await _tool(make_config, bot, store).handler({"file_path": str(doc)})
    assert not _is_error(result)
    markup = bot.documents[0]["reply_markup"]
    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert "startapp=markdown__c_" in button.url
    content_id = button.url.rsplit("markdown__c_", 1)[1]
    entry = store.get(content_id)
    assert entry is not None
    assert entry.title == "report.md"
    assert entry.content == "# Report\n\nFindings."


async def test_send_file_non_markdown_no_button(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    doc = tmp_path / "data.csv"
    doc.write_text("a,b\n1,2")
    bot = FakeBot()
    await _tool(make_config, bot).handler({"file_path": str(doc)})
    assert bot.documents[0]["reply_markup"] is None


async def test_send_file_markdown_without_public_url_sends_without_button(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    doc = tmp_path / "report.md"
    doc.write_text("# Report")
    bot = FakeBot()
    result = await _tool(make_config, bot, public=False).handler({"file_path": str(doc)})
    assert not _is_error(result)
    assert bot.documents[0]["reply_markup"] is None


async def test_send_file_general_topic_omits_thread_id(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    doc = tmp_path / "note.txt"
    doc.write_text("hi")
    bot = FakeBot()
    await _tool(make_config, bot, thread_id=None).handler({"file_path": str(doc)})
    assert "message_thread_id" not in bot.documents[0]


async def test_send_file_long_caption_truncated(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    doc = tmp_path / "note.txt"
    doc.write_text("hi")
    bot = FakeBot()
    await _tool(make_config, bot).handler({"file_path": str(doc), "caption": "x" * 2000})
    assert len(bot.documents[0]["caption"]) == 1024


async def test_send_file_telegram_failure_reports_error(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    doc = tmp_path / "note.txt"
    doc.write_text("hi")

    class ExplodingBot(FakeBot):
        async def send_document(self, **kwargs: Any) -> None:
            raise RuntimeError("flood limit")

    result = await _tool(make_config, ExplodingBot()).handler({"file_path": str(doc)})
    assert _is_error(result)
    assert "flood limit" in _text(result)


# --- ToolScopes ---


def test_tool_scopes_stable_per_topic() -> None:
    scopes = ToolScopes()
    first = scopes.register(100, 7)
    second = scopes.register(100, 7)
    assert first.token == second.token
    assert scopes.get(first.token) is first


def test_tool_scopes_distinct_per_topic() -> None:
    scopes = ToolScopes()
    a = scopes.register(100, 7)
    b = scopes.register(100, 8)
    assert a.token != b.token
    assert scopes.get("not-a-token") is None


def test_server_name_per_thread() -> None:
    scopes = ToolScopes()
    assert server_name(scopes.register(100, 7), qualify_chat=False) == "balam_t7"
    assert server_name(scopes.register(100, None), qualify_chat=False) == "balam_t0"
    assert server_name(scopes.register(-100123, 7), qualify_chat=True) == "balam_cn100123_t7"


# --- JSON-RPC dispatch ---


def _send_file_tools(make_config: Callable[..., Config], bot: FakeBot) -> list[AgentTool]:
    return [_tool(make_config, bot)]


async def test_rpc_initialize(make_config: Callable[..., Config]) -> None:
    reply = await handle_rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        _send_file_tools(make_config, FakeBot()),
    )
    assert reply is not None
    assert reply["id"] == 1
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["capabilities"] == {"tools": {}}


async def test_rpc_initialized_notification_is_silent(
    make_config: Callable[..., Config],
) -> None:
    reply = await handle_rpc(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _send_file_tools(make_config, FakeBot()),
    )
    assert reply is None


async def test_rpc_tools_list(make_config: Callable[..., Config]) -> None:
    reply = await handle_rpc(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        _send_file_tools(make_config, FakeBot()),
    )
    assert reply is not None
    (tool,) = reply["result"]["tools"]
    assert tool["name"] == "send_file"
    assert tool["annotations"] == {"readOnlyHint": True}
    assert tool["inputSchema"]["required"] == ["file_path"]


async def test_rpc_tools_call_routes_to_handler(
    make_config: Callable[..., Config], tmp_path: Path
) -> None:
    doc = tmp_path / "note.txt"
    doc.write_text("hi")
    bot = FakeBot()
    reply = await handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "send_file", "arguments": {"file_path": str(doc)}},
        },
        _send_file_tools(make_config, bot),
    )
    assert reply is not None
    assert "successfully" in reply["result"]["content"][0]["text"]
    assert len(bot.documents) == 1


async def test_rpc_unknown_tool(make_config: Callable[..., Config]) -> None:
    reply = await handle_rpc(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},
        _send_file_tools(make_config, FakeBot()),
    )
    assert reply is not None
    assert reply["error"]["code"] == -32602


async def test_rpc_unknown_method(make_config: Callable[..., Config]) -> None:
    reply = await handle_rpc(
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
        _send_file_tools(make_config, FakeBot()),
    )
    assert reply is not None
    assert reply["error"]["code"] == -32601


async def test_rpc_handler_exception_becomes_tool_error(
    make_config: Callable[..., Config],
) -> None:
    async def boom(args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("kaput")

    broken = AgentTool(
        name="send_file", description="", input_schema={}, read_only=True, handler=boom
    )
    reply = await handle_rpc(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "send_file"}},
        [broken],
    )
    assert reply is not None
    assert reply["result"]["isError"] is True
    assert "kaput" in reply["result"]["content"][0]["text"]


async def test_rpc_invalid_request(make_config: Callable[..., Config]) -> None:
    reply = await handle_rpc("not a dict", _send_file_tools(make_config, FakeBot()))
    assert reply is not None
    assert reply["error"]["code"] == -32600
