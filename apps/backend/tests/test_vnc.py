"""Tests for the live browser view bridge (balam.vnc, ADR-0006).

The fake VNC server is a *threaded* TCP echo server, not an asyncio one:
FastAPI's TestClient runs the app on its own thread/event loop, so a server
living on the pytest-asyncio loop would deadlock under the bridged connection.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from balam.config import Config
from balam.router import Router
from balam.server import create_app
from balam.vnc import WS_CLOSE_UNAUTHORIZED, WS_CLOSE_VNC_UNAVAILABLE, probe_vnc
from conftest import make_init_data


@pytest.fixture
def echo_server() -> Iterator[tuple[str, int]]:
    """A threaded TCP server echoing every byte back — a stand-in for x11vnc."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen()
    host, port = server.getsockname()
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except OSError:  # server socket closed on teardown
                return
            with conn:
                while True:
                    try:
                        data = conn.recv(65536)
                    except OSError:
                        break
                    if not data:
                        break
                    conn.sendall(data)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield host, port
    stop.set()
    server.close()
    thread.join(timeout=2)


@pytest.fixture
def free_port() -> int:
    """A localhost port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _client(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    *,
    vnc_host: str = "127.0.0.1",
    vnc_port: int,
) -> TestClient:
    config = make_config(balam_vnc_host=vnc_host, balam_vnc_port=vnc_port)
    return TestClient(create_app(config, router_with(git_repo)))


async def test_probe_vnc_up() -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await probe_vnc("127.0.0.1", port) is True
    finally:
        server.close()
        await server.wait_closed()


async def test_probe_vnc_down(free_port: int) -> None:
    assert await probe_vnc("127.0.0.1", free_port) is False


def test_browser_status_requires_auth(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    free_port: int,
) -> None:
    client = _client(make_config, router_with, git_repo, vnc_port=free_port)
    assert client.get("/api/browser/status").status_code == 401


def test_browser_status_running(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    echo_server: tuple[str, int],
) -> None:
    host, port = echo_server
    client = _client(make_config, router_with, git_repo, vnc_host=host, vnc_port=port)
    resp = client.get("/api/browser/status", headers={"Authorization": f"tma {make_init_data()}"})
    assert resp.status_code == 200
    assert resp.json() == {"running": True}


def test_browser_status_down(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    free_port: int,
) -> None:
    client = _client(make_config, router_with, git_repo, vnc_port=free_port)
    resp = client.get("/api/browser/status", headers={"Authorization": f"tma {make_init_data()}"})
    assert resp.status_code == 200
    assert resp.json() == {"running": False}


# Auth is the FIRST text frame, never the URL (a ?token= query param would land
# verbatim in uvicorn's WebSocket accept log) — these helpers mirror the frontend.


def _assert_ws_closes(client: TestClient, first_frame: str | bytes, code: int) -> None:
    with client.websocket_connect("/api/vnc/ws") as ws:
        if isinstance(first_frame, bytes):
            ws.send_bytes(first_frame)
        else:
            ws.send_text(first_frame)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_bytes()
    assert exc.value.code == code


def test_vnc_ws_rejects_empty_token(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    echo_server: tuple[str, int],
) -> None:
    host, port = echo_server
    client = _client(make_config, router_with, git_repo, vnc_host=host, vnc_port=port)
    _assert_ws_closes(client, "", WS_CLOSE_UNAUTHORIZED)


def test_vnc_ws_rejects_bad_token(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    echo_server: tuple[str, int],
) -> None:
    host, port = echo_server
    client = _client(make_config, router_with, git_repo, vnc_host=host, vnc_port=port)
    _assert_ws_closes(client, "garbage", WS_CLOSE_UNAUTHORIZED)


def test_vnc_ws_rejects_wrong_user(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    echo_server: tuple[str, int],
) -> None:
    host, port = echo_server
    client = _client(make_config, router_with, git_repo, vnc_host=host, vnc_port=port)
    _assert_ws_closes(client, make_init_data(user_id=999), WS_CLOSE_UNAUTHORIZED)


def test_vnc_ws_rejects_binary_first_frame(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    echo_server: tuple[str, int],
) -> None:
    # A stock RFB client pointed straight at the endpoint speaks binary first.
    host, port = echo_server
    client = _client(make_config, router_with, git_repo, vnc_host=host, vnc_port=port)
    _assert_ws_closes(client, b"RFB 003.008\n", WS_CLOSE_UNAUTHORIZED)


def test_vnc_ws_vnc_down_closes(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    free_port: int,
) -> None:
    client = _client(make_config, router_with, git_repo, vnc_port=free_port)
    _assert_ws_closes(client, make_init_data(), WS_CLOSE_VNC_UNAVAILABLE)


def test_vnc_ws_bridges_bytes(
    make_config: Callable[..., Config],
    router_with: Callable[[Path], Router],
    git_repo: Path,
    echo_server: tuple[str, int],
) -> None:
    host, port = echo_server
    client = _client(make_config, router_with, git_repo, vnc_host=host, vnc_port=port)
    with client.websocket_connect("/api/vnc/ws") as ws:
        ws.send_text(make_init_data())
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"RFB 003.008\n"
        # A second round-trip proves the bridge stays up, not a one-shot pump.
        ws.send_bytes(b"\x01\x02\x03")
        assert ws.receive_bytes() == b"\x01\x02\x03"
