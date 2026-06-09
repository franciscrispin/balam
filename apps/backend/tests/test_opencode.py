from types import SimpleNamespace
from typing import Any

import pytest

from balam.attachments import PromptFile
from balam.opencode import OpenCode, coerce_mcp_config


class _FakePostClient:
    """Captures the body posted to prompt_async; no-op raise_for_status."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, params: Any = None, json: Any = None) -> Any:
        self.calls.append({"url": url, "params": params, "json": json})
        return SimpleNamespace(raise_for_status=lambda: None)


def _client() -> tuple[OpenCode, _FakePostClient]:
    oc = OpenCode(base_url="http://x", username="u", password=None)
    fake = _FakePostClient()
    oc._client = fake  # type: ignore[assignment]
    return oc, fake


async def test_prompt_appends_file_parts_after_text() -> None:
    oc, fake = _client()
    files = [
        PromptFile(mime="image/jpeg", url="data:image/jpeg;base64,AAAA"),
        PromptFile(
            mime="application/pdf", url="data:application/pdf;base64,BBBB", filename="r.pdf"
        ),
    ]

    await oc.prompt("ses_1", "look at these", directory="/work", files=files)

    parts = fake.calls[0]["json"]["parts"]
    assert parts[0] == {"type": "text", "text": "look at these"}
    assert parts[1] == {"type": "file", "mime": "image/jpeg", "url": "data:image/jpeg;base64,AAAA"}
    assert parts[2] == {
        "type": "file",
        "mime": "application/pdf",
        "url": "data:application/pdf;base64,BBBB",
        "filename": "r.pdf",
    }


async def test_prompt_omits_empty_text_part_for_attachment_only() -> None:
    oc, fake = _client()

    await oc.prompt(
        "ses_1",
        "",
        directory="/work",
        files=[PromptFile(mime="image/jpeg", url="data:image/jpeg;base64,AAAA")],
    )

    parts = fake.calls[0]["json"]["parts"]
    assert parts == [{"type": "file", "mime": "image/jpeg", "url": "data:image/jpeg;base64,AAAA"}]


async def test_prompt_text_only_unchanged() -> None:
    oc, fake = _client()

    await oc.prompt("ses_1", "hello", directory="/work")

    assert fake.calls[0]["json"]["parts"] == [{"type": "text", "text": "hello"}]


# --- MCP config coercion ---


def test_coerce_local_command_shorthand() -> None:
    out = coerce_mcp_config(
        "db",
        {"command": "postgres-mcp", "args": ["--restricted"], "env": {"URI": "x"}},
    )
    assert out == {
        "type": "local",
        "command": ["postgres-mcp", "--restricted"],
        "environment": {"URI": "x"},
    }


def test_coerce_local_explicit_type() -> None:
    out = coerce_mcp_config(
        "db",
        {"type": "local", "command": ["postgres-mcp", "-x"], "enabled": True},
    )
    assert out == {"type": "local", "command": ["postgres-mcp", "-x"], "enabled": True}


def test_coerce_remote_http_collapses_to_remote() -> None:
    out = coerce_mcp_config(
        "api",
        {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer t"}},
    )
    assert out == {
        "type": "remote",
        "url": "https://x/mcp",
        "headers": {"Authorization": "Bearer t"},
    }


def test_coerce_rejects_unknown_shape() -> None:
    with pytest.raises(ValueError):
        coerce_mcp_config("bad", {"nonsense": True})
    with pytest.raises(ValueError):
        coerce_mcp_config("bad", {"type": "remote"})  # missing url


# --- MCP registration ---


async def test_create_session_registers_mcp_before_session() -> None:
    oc, fake = _client()
    oc._client.get = lambda *a, **k: None  # type: ignore[attr-defined]

    # create_session reads the session id from the POST /session response.
    async def post(url: str, *, params: Any = None, json: Any = None) -> Any:
        fake.calls.append({"url": url, "params": params, "json": json})
        body = {"id": "ses_9"} if url == "/session" else {}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    fake.post = post  # type: ignore[assignment]

    sid = await oc.create_session(
        "t",
        directory="/work",
        permission=[{"permission": "*", "pattern": "*", "action": "ask"}],
        mcp={
            "db": {"type": "local", "command": ["postgres-mcp"]},
            "api": {"type": "remote", "url": "https://x/mcp"},
        },
    )

    assert sid == "ses_9"
    # Two /mcp registrations, then the /session create — order matters.
    assert [c["url"] for c in fake.calls] == ["/mcp", "/mcp", "/session"]
    assert fake.calls[0]["params"] == {"directory": "/work"}
    assert fake.calls[0]["json"] == {
        "name": "db",
        "config": {"type": "local", "command": ["postgres-mcp"]},
    }
    assert fake.calls[1]["json"]["name"] == "api"


async def test_register_mcp_swallows_http_errors() -> None:
    import httpx

    oc, _ = _client()

    async def boom(*a: Any, **k: Any) -> Any:
        raise httpx.ConnectError("down")

    oc._client.post = boom  # type: ignore[assignment]

    # Best-effort: a registration failure must not raise.
    await oc.register_mcp("db", {"type": "local", "command": ["x"]}, directory="/work")
