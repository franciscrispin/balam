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
  POST /session/{id}/abort           cancel the running turn (best-effort)
  POST /permission/{id}/reply        answer a permission.asked request
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

from balam.opencode_tools import Permission

logger = logging.getLogger(__name__)

#: Fallback permission ruleset for sessions created without an explicit one: makes
#: OpenCode *ask* (raise a ``permission.asked`` SSE event) before every tool call
#: rather than run it unattended. The real per-context ruleset is built by
#: :func:`balam.permissions.build_ruleset` and passed to :meth:`OpenCode.create_session`;
#: this baseline is its zero-opt-in case. OpenCode evaluates the LAST matching
#: rule, so the ask-all baseline goes first and the ``todowrite`` allow (internal
#: bookkeeping, never user-visible) follows it. See ADR-0012 and the open-shrimp
#: reference.
ASK_ALL_PERMISSIONS: list[dict[str, str]] = [
    {"permission": "*", "pattern": "*", "action": "ask"},
    {"permission": Permission.TODOWRITE, "pattern": "*", "action": "allow"},
]


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

    async def create_session(
        self,
        title: str,
        *,
        directory: str,
        permission: list[dict[str, str]] | None = None,
    ) -> str:
        """Create a new session in ``directory`` (a context's workspace); return
        its id.

        ``permission`` is the session's native ruleset (see
        :mod:`balam.permissions`); it defaults to :data:`ASK_ALL_PERMISSIONS` so a
        caller that doesn't compute one still gets the ask-everything baseline.
        """
        response = await self._client.post(
            "/session",
            params={"directory": directory},
            json={
                "title": title,
                "permission": permission if permission is not None else ASK_ALL_PERMISSIONS,
            },
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

    async def abort_session(self, session_id: str, *, directory: str) -> None:
        """Cancel the turn running in ``session_id`` (``POST /session/{id}/abort``).

        Best-effort: ``/cancel`` also cancels the local streaming task, so the
        turn stops regardless of the server's answer. A failure here is logged,
        not raised — an already-idle session abort would otherwise surface a
        spurious error to the user.
        """
        try:
            response = await self._client.post(
                f"/session/{session_id}/abort",
                params={"directory": directory},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning("failed to abort session %s", session_id, exc_info=True)

    async def reply_permission(
        self,
        request_id: str,
        reply: str,
        *,
        directory: str | None = None,
        message: str | None = None,
    ) -> None:
        """Answer a ``permission.asked`` request (``POST /permission/{id}/reply``).

        ``reply`` is ``"once"`` / ``"always"`` (allow) or ``"reject"`` (deny,
        with an optional ``message`` surfaced to the agent). Best-effort: a
        failure — e.g. the request was already resolved (404), or the turn was
        aborted out from under us — is logged, not raised, so it never tears the
        stream down.
        """
        body: dict[str, Any] = {"reply": reply}
        if message is not None:
            body["message"] = message
        params = {"directory": directory} if directory else None
        try:
            response = await self._client.post(
                f"/permission/{request_id}/reply", params=params, json=body
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning("failed to reply to permission %s", request_id, exc_info=True)

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
                        # A malformed frame is a real protocol fault: let it
                        # propagate so the turn fails fast and visibly (the
                        # streamer surfaces it as an error reply) rather than
                        # silently skipping a frame the user would never notice.
                        yield json.loads(payload)
