"""Thin OpenCode HTTP/SSE client (ADR-0001/0002).

Balam is a client of a long-lived, localhost-bound OpenCode server. ADR-0002
makes the HTTP API the source of truth, and ADR-0011 has us call it directly
(no generated SDK). This wrapper owns three concerns:

  1. Authenticated transport — the server uses HTTP Basic auth; we inject the
     ``Authorization`` header on every request, including the SSE stream.
  2. Readiness — poll ``/doc`` and wait for the server before serving traffic.
  3. A minimal session surface — create a session, send a prompt, and expose the
     raw SSE event stream the streamer consumes.

Endpoints (from the OpenAPI spec at ``/doc``):
  GET  /doc                          health
  POST /session                      create a session
  GET  /session/{id}                 fetch a session (existence check)
  POST /session/{id}/prompt_async    send a message, return immediately
  GET  /event                        SSE stream of all server events
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OpenCode:
    def __init__(self, *, base_url: str, username: str, password: str | None) -> None:
        self._base_url = base_url.rstrip("/")
        headers: dict[str, str] = {}
        if password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        # read=None: the SSE stream is open-ended. connect/write are bounded so a
        # dead server surfaces quickly during the readiness poll.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def wait_for_ready(self, *, timeout: float = 30.0, interval: float = 0.5) -> None:
        """Poll ``/doc`` until the server answers, or raise after ``timeout``.

        Connection failures are retried (the server may still be starting); an
        auth rejection fails fast, since retrying bad credentials never works.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        last_error: str = "no response"
        while True:
            try:
                response = await self._client.get("/doc")
                if response.status_code == 200:
                    return
                if response.status_code in (401, 403):
                    raise RuntimeError(
                        f"OpenCode rejected credentials (HTTP {response.status_code}) at "
                        f"{self._base_url}. Check OPENCODE_SERVER_PASSWORD / "
                        "OPENCODE_SERVER_USERNAME match the server."
                    )
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                # Connection refused / DNS: likely still booting — retry.
                last_error = repr(exc)
            if loop.time() >= deadline:
                raise RuntimeError(
                    f"OpenCode server not ready at {self._base_url} after {timeout}s "
                    f"(last: {last_error}). Is `opencode serve` running?"
                )
            await asyncio.sleep(interval)

    async def create_session(self, title: str, *, directory: str) -> str:
        """Create a new session in ``directory`` (a context's workspace); return
        its id."""
        response = await self._client.post(
            "/session",
            params={"directory": directory},
            json={"title": title},
        )
        response.raise_for_status()
        return response.json()["id"]

    async def session_exists(self, session_id: str, *, directory: str) -> bool:
        """Whether a session still exists server-side (false after a wipe)."""
        response = await self._client.get(
            f"/session/{session_id}",
            params={"directory": directory},
        )
        return response.status_code == 200

    async def prompt(
        self,
        session_id: str,
        text: str,
        *,
        directory: str,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        """Send a user message and return immediately (``prompt_async``). The
        assistant's reply arrives over :meth:`events`, which is what lets us
        stream it back to Telegram as it is generated.

        ``provider``/``model`` select the context's model (OpenCode wants
        ``{providerID, modelID}``); ``effort`` maps to the prompt ``variant``.
        Each is omitted when unset so the server applies its own default.
        """
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if provider and model:
            body["model"] = {"providerID": provider, "modelID": model}
        if effort is not None:
            body["variant"] = effort
        response = await self._client.post(
            f"/session/{session_id}/prompt_async",
            params={"directory": directory},
            json=body,
        )
        response.raise_for_status()

    async def events(
        self, *, directory: str | None = None, ready: asyncio.Event | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded events from the server's SSE stream.

        ``directory`` is **load-bearing**: OpenCode scopes ``message.*`` /
        ``session.*`` events to a worktree, so a ``/event`` subscription opened
        *without* the prompt's ``directory`` receives only global ``server.*``
        events (``connected``/``heartbeat``) and never the session deltas or
        ``session.idle`` the streamer waits on — the reply then never finalizes.
        Pass the resolved context directory so those events flow. Consumers still
        filter by ``sessionID`` since one worktree may host several sessions.
        Breaking out of the ``async for`` closes the connection.

        If ``ready`` is given, it is set once the stream is established (after the
        response headers arrive). Callers prompt only after this fires, so the
        server's early ``message.updated`` / ``message.part.updated`` events
        aren't lost to a subscribe-after-prompt race.
        """
        params = {"directory": directory} if directory else None
        async with self._client.stream("GET", "/event", params=params) as response:
            response.raise_for_status()
            if ready is not None:
                ready.set()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    payload = line[len("data:") :].strip()
                    if payload:
                        yield json.loads(payload)
