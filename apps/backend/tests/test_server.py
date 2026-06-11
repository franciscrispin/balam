"""Tests for the FastAPI Mini App server (balam.server)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from balam.agent_tools import ToolScopes
from balam.config import Config
from balam.content_store import CONTENT_TTL_S, ContentStore
from balam.router import Router
from balam.server import create_app, openapi_schema
from conftest import make_init_data


def _client(config: Config, router: Router, **kwargs) -> TestClient:
    return TestClient(create_app(config, router, **kwargs))


def _auth() -> dict[str, str]:
    """A valid owner Authorization header (real HMAC over the test bot token)."""
    return {"Authorization": f"tma {make_init_data()}"}


def test_app_info_requires_auth(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    client = _client(make_config(), router_with(git_repo))
    assert client.get("/api/app-info").status_code == 401


def test_app_info_with_valid_init_data(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    client = _client(make_config(), router_with(git_repo))
    resp = client.get("/api/app-info", headers={"Authorization": f"tma {make_init_data()}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "balam"
    assert isinstance(body["version"], str)


def test_diff_returns_hunks_for_context(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    (git_repo / "hello.py").write_text("def hello():\n    return 99\n")
    client = _client(make_config(), router_with(git_repo))

    resp = client.get("/api/diff?context=balam", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["context"] == "balam"
    files = {h["file_path"] for h in body["hunks"]}
    assert "hello.py" in files


def test_diff_defaults_to_default_context(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    client = _client(make_config(), router_with(git_repo))
    resp = client.get("/api/diff", headers=_auth())  # no ?context
    assert resp.status_code == 200
    assert resp.json()["context"] == "balam"


def test_diff_unknown_context_is_404(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    client = _client(make_config(), router_with(git_repo))
    resp = client.get("/api/diff?context=nope", headers=_auth())
    assert resp.status_code == 404


def test_diff_requires_auth(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    client = _client(make_config(), router_with(git_repo))
    assert client.get("/api/diff?context=balam").status_code == 401


def test_markdown_content_requires_auth(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    client = _client(make_config(), router_with(git_repo))
    assert client.get("/api/markdown/content/deadbeef0000").status_code == 401


def test_markdown_content_round_trip(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    store = ContentStore()
    content_id = store.put("plan.md", "# The plan\n\n- step one")
    client = _client(make_config(), router_with(git_repo), content_store=store)
    resp = client.get(f"/api/markdown/content/{content_id}", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"title": "plan.md", "content": "# The plan\n\n- step one"}


def test_markdown_content_unknown_id_is_404(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    client = _client(make_config(), router_with(git_repo), content_store=ContentStore())
    resp = client.get("/api/markdown/content/deadbeef0000", headers=_auth())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "content not found or expired"


def test_markdown_content_expired_is_404(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    now = [1000.0]
    store = ContentStore(clock=lambda: now[0])
    content_id = store.put("plan.md", "stale")
    now[0] += CONTENT_TTL_S + 1
    client = _client(make_config(), router_with(git_repo), content_store=store)
    assert client.get(f"/api/markdown/content/{content_id}", headers=_auth()).status_code == 404


class _RecordingBot:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []

    async def send_document(self, **kwargs: Any) -> None:
        self.documents.append(kwargs)

    async def send_photo(self, **kwargs: Any) -> None:  # pragma: no cover - unused
        raise AssertionError("unexpected photo")


def _mcp_client(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    scopes: ToolScopes,
    bot: _RecordingBot,
) -> TestClient:
    return _client(
        make_config(), router_with(git_repo), tool_scopes=scopes, bot=bot, bot_username="balam_bot"
    )


def test_mcp_unknown_token_is_404(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    client = _mcp_client(make_config, router_with, git_repo, ToolScopes(), _RecordingBot())
    resp = client.post("/mcp/not-a-token", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 404


def test_mcp_tools_list_and_call_route_by_scope(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    tmp_path: Path,
) -> None:
    scopes = ToolScopes()
    scope_a = scopes.register(100, 7)
    scope_b = scopes.register(100, 8)
    bot = _RecordingBot()
    client = _mcp_client(make_config, router_with, git_repo, scopes, bot)

    resp = client.post(
        f"/mcp/{scope_a.token}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["tools"][0]["name"] == "send_file"

    doc = tmp_path / "note.txt"
    doc.write_text("hi")
    for scope, thread in ((scope_a, 7), (scope_b, 8)):
        resp = client.post(
            f"/mcp/{scope.token}",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "send_file", "arguments": {"file_path": str(doc)}},
            },
        )
        assert resp.status_code == 200
        assert bot.documents[-1]["message_thread_id"] == thread


def test_mcp_malformed_json_is_parse_error(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    scopes = ToolScopes()
    scope = scopes.register(100, 7)
    client = _mcp_client(make_config, router_with, git_repo, scopes, _RecordingBot())
    resp = client.post(
        f"/mcp/{scope.token}", content=b"{nope", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700


def test_mcp_notification_is_202(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    scopes = ToolScopes()
    scope = scopes.register(100, 7)
    client = _mcp_client(make_config, router_with, git_repo, scopes, _RecordingBot())
    resp = client.post(
        f"/mcp/{scope.token}", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert resp.status_code == 202


def test_mcp_rejects_tunnel_requests(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    # OpenCode dials 127.0.0.1 directly; anything that crossed the Cloudflare
    # tunnel carries cf-connecting-ip and must not reach the tools.
    scopes = ToolScopes()
    scope = scopes.register(100, 7)
    client = _mcp_client(make_config, router_with, git_repo, scopes, _RecordingBot())
    resp = client.post(
        f"/mcp/{scope.token}",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"cf-connecting-ip": "203.0.113.5"},
    )
    assert resp.status_code == 404


def test_mcp_route_absent_without_wiring(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    client = _client(make_config(), router_with(git_repo))  # no tool_scopes/bot
    resp = client.post("/mcp/whatever", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code in (404, 405)


def test_openapi_schema_exposes_diff_hunk() -> None:
    # The frontend's types are generated from this in-process schema (ADR-0003),
    # not the HTTP route — so assert on the source of truth directly.
    schemas = openapi_schema()["components"]["schemas"]
    assert "DiffHunk" in schemas
    assert "HunkLine" in schemas


def test_docs_and_openapi_route_not_served(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    # The app is internet-reachable (ADR-0013): no interactive docs, no HTTP schema.
    client = _client(make_config(), router_with(git_repo))
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_spa_served_when_dist_present(
    make_config: Callable[..., Config], router_with: Callable[[Path], Router], git_repo: Path
) -> None:
    # The repo's built Mini App (apps/frontend/dist) is mounted at "/". When it is
    # absent the mount is skipped, so only assert on serving when it exists.
    dist = Path(__file__).resolve().parents[3] / "apps" / "frontend" / "dist"
    client = _client(make_config(), router_with(git_repo))
    resp = client.get("/")
    if (dist / "index.html").is_file():
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
    else:
        assert resp.status_code == 404
