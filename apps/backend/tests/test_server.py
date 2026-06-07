"""Tests for the FastAPI Mini App server (balam.server)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from balam.config import Config
from balam.router import Router
from balam.server import create_app, openapi_schema
from conftest import make_init_data


def _client(config: Config, router: Router) -> TestClient:
    return TestClient(create_app(config, router))


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
