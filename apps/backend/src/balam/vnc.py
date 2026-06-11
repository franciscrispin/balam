"""Live browser view: WebSocket↔TCP bridge to x11vnc (ADR-0006, as amended).

The Mini App's noVNC client (RFB over WebSocket) connects to ``/api/vnc/ws``;
this module bridges raw bytes between that WebSocket and the x11vnc server on
``127.0.0.1:5900`` (the browser-use skill's display ``:99``). websockify and
the noVNC checkout on ``:6081`` are a dev convenience of the skill, never in
this serving path.

The backend never starts the VNC stack: when it is down, :func:`probe_vnc`
reports it (``GET /api/browser/status``) and the WebSocket closes with
:data:`WS_CLOSE_VNC_UNAVAILABLE`. Concurrent viewers are allowed (x11vnc runs
``-shared``). x11vnc is ``-nopw``, so the bridge is plain passthrough — no RFB
filtering or credential interception.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import WebSocket

from balam.webapp_auth import is_owner_init_data

logger = logging.getLogger(__name__)

#: WebSocket close codes (4000–4999 are application-defined); mirror HTTP semantics.
WS_CLOSE_UNAUTHORIZED = 4401  # missing/invalid token, or not the owner
WS_CLOSE_VNC_UNAVAILABLE = 4502  # TCP connect to the VNC server failed

_PROBE_TIMEOUT_S = 1.0
_TCP_CHUNK = 65536
#: How long the client has to send its auth frame after connecting.
_AUTH_TIMEOUT_S = 10.0


async def probe_vnc(host: str, port: int, *, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """True iff a TCP connection to ``host:port`` succeeds within ``timeout``."""
    try:
        async with asyncio.timeout(timeout):
            _reader, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def vnc_websocket(
    websocket: WebSocket,
    *,
    bot_token: str,
    allowed_user_id: int,
    host: str,
    port: int,
) -> None:
    """Accept, authenticate via a first-frame token, then bridge bytes to x11vnc.

    A browser cannot set an ``Authorization`` header on a WebSocket, and a
    ``?token=`` query param would land verbatim in uvicorn's accept log — so the
    client instead sends its ``initData`` as the **first (text) frame**, before
    any RFB bytes flow (same trust boundary as
    :class:`balam.webapp_auth.RequireOwner`, ADR-0008). That ordering is safe
    because in the RFB protocol the *server* speaks first; the real stream only
    starts once auth passes and the TCP side is connected. Accept comes first so
    the application close codes actually reach the client.
    """
    await websocket.accept()

    try:
        async with asyncio.timeout(_AUTH_TIMEOUT_S):
            message = await websocket.receive()
    except TimeoutError:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="auth timeout")
        return
    if message.get("type") == "websocket.disconnect":
        return
    # The token is a live credential (valid up to 24h) — never log it. A binary
    # first frame (e.g. a stock RFB client pointed here directly) is rejected.
    token = message.get("text") or ""
    if not is_owner_init_data(token, bot_token=bot_token, allowed_user_id=allowed_user_id):
        logger.info("vnc ws rejected: invalid or non-owner initData")
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="unauthorized")
        return

    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        logger.info("vnc ws: cannot reach VNC server at %s:%s (%s)", host, port, exc)
        await websocket.close(code=WS_CLOSE_VNC_UNAVAILABLE, reason="no live browser session")
        return

    logger.info("vnc ws connected -> %s:%s", host, port)
    try:
        await _bridge(websocket, reader, writer)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await websocket.close()
        logger.info("vnc ws disconnected")


async def _bridge(
    websocket: WebSocket, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Pump bytes both ways until either side ends, then cancel the other.

    ``FIRST_COMPLETED`` (rather than waiting on both pumps) is what closes the
    WebSocket promptly when the VNC server dies mid-session — a clean TCP EOF
    must not leave the bridge blocked on the still-open client side.
    """

    async def ws_to_tcp() -> None:
        while True:  # WebSocketDisconnect ends this pump
            data = await websocket.receive_bytes()
            writer.write(data)
            await writer.drain()

    async def tcp_to_ws() -> None:
        while True:
            data = await reader.read(_TCP_CHUNK)
            if not data:  # VNC server closed (EOF)
                return
            await websocket.send_bytes(data)

    ws_task = asyncio.create_task(ws_to_tcp())
    tcp_task = asyncio.create_task(tcp_to_ws())
    _done, pending = await asyncio.wait({ws_task, tcp_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    # Retrieve every result so no "exception was never retrieved" warning leaks;
    # disconnects and resets are the normal end of a viewing session.
    for task in (ws_task, tcp_task):
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task
