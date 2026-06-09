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

from balam.attachments import PromptFile
from balam.opencode_tools import Permission

logger = logging.getLogger(__name__)

#: Fallback permission ruleset for sessions created without an explicit one: makes
#: OpenCode *ask* (raise a ``permission.asked`` SSE event) before every tool call
#: rather than run it unattended. The real per-context ruleset is built by
#: :func:`balam.permissions.build_ruleset` and passed to :meth:`OpenCode.create_session`;
#: this baseline is its zero-opt-in case. OpenCode evaluates the LAST matching
#: rule, so the ask-all baseline goes first and the always-allowed baseline tools
#: follow it. See ADR-0012 and the open-shrimp reference.
ASK_ALL_PERMISSIONS: list[dict[str, str]] = [
    {"permission": "*", "pattern": "*", "action": "ask"},
    {"permission": Permission.TODOWRITE, "pattern": "*", "action": "allow"},
    {"permission": Permission.QUESTION, "pattern": "*", "action": "allow"},
]


def coerce_mcp_config(name: str, raw_config: Any) -> dict[str, Any]:
    """Normalise one context MCP server entry into OpenCode's ``/mcp`` wire format.

    A *context*'s ``mcp`` map (see :mod:`balam.contexts`) is registered with the
    OpenCode server before its session is created (:meth:`OpenCode.register_mcp`).
    OpenCode wants one of two shapes:

      * **local** (stdio) — ``{"type": "local", "command": [...], "environment": {...}}``
      * **remote** (http/sse) — ``{"type": "remote", "url": ..., "headers": {...}}``

    To keep ``config.yaml`` friendly we also accept the looser ``opencode.json``
    spellings and convert them: a bare ``command`` *string* with ``args``/``env``
    becomes a local ``command`` list, and ``type: http``/``sse`` collapse to
    ``remote``. A malformed entry raises :class:`ValueError`, which the context
    loader turns into a fatal, fail-fast boot error (so a bad server is caught at
    startup, never silently mid-conversation). Adapted from the open-shrimp client.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("MCP server name must be a non-empty string")
    if not isinstance(raw_config, dict):
        raise ValueError(f"MCP server {name!r} config must be a mapping")
    config = dict(raw_config)

    # `command: "uvx"` + `args: [...]` shorthand → a single local command list.
    if "command" in config and config.get("type") not in {"local", "remote", "http", "sse"}:
        command = config.pop("command")
        if not isinstance(command, str) or not command:
            raise ValueError(f"MCP server {name!r} command must be a non-empty string")
        args = config.pop("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"MCP server {name!r} args must be a list of strings")
        env = config.pop("env", config.pop("environment", None))
        out: dict[str, Any] = {"type": "local", "command": [command, *args]}
        if env is not None:
            if not isinstance(env, dict):
                raise ValueError(f"MCP server {name!r} environment must be a mapping")
            out["environment"] = {str(k): str(v) for k, v in env.items()}
        if "enabled" in config:
            out["enabled"] = bool(config["enabled"])
        return out

    cfg_type = config.get("type")
    if cfg_type in {"remote", "http", "sse"}:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"MCP server {name!r} remote config requires a url")
        out = {"type": "remote", "url": url}
        if "headers" in config:
            headers = config["headers"]
            if not isinstance(headers, dict):
                raise ValueError(f"MCP server {name!r} headers must be a mapping")
            out["headers"] = {str(k): str(v) for k, v in headers.items()}
        if "oauth" in config:
            out["oauth"] = bool(config["oauth"])
        if "enabled" in config:
            out["enabled"] = bool(config["enabled"])
        return out

    if cfg_type == "local":
        command = config.get("command")
        if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
            raise ValueError(f"MCP server {name!r} local command must be a list of strings")
        out = {"type": "local", "command": command}
        env = config.get("environment", config.get("env"))
        if env is not None:
            if not isinstance(env, dict):
                raise ValueError(f"MCP server {name!r} environment must be a mapping")
            out["environment"] = {str(k): str(v) for k, v in env.items()}
        if "enabled" in config:
            out["enabled"] = bool(config["enabled"])
        return out

    raise ValueError(
        f"MCP server {name!r} must be a local server (command) or a remote server "
        f"(type: remote/http/sse + url); got {raw_config!r}"
    )


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

    async def register_mcp(self, name: str, config: dict[str, Any], *, directory: str) -> None:
        """Register one MCP server with the OpenCode server, scoped to ``directory``.

        OpenCode keys MCP servers by ``directory`` (the worktree), not by session,
        so registering the same context's servers again before each session in that
        directory is idempotent. ``config`` is the raw context entry; it is coerced
        to OpenCode's wire shape by :func:`coerce_mcp_config`.

        Best-effort, like :meth:`abort_session` / :meth:`reply_permission`: a
        registration failure (e.g. the server is briefly unreachable) is logged, not
        raised, so one bad server never tears down session creation. Config-shape
        errors are already caught at boot by the context loader, so they don't reach
        here.
        """
        try:
            coerced = coerce_mcp_config(name, config)
            response = await self._client.post(
                "/mcp",
                params={"directory": directory},
                json={"name": name, "config": coerced},
            )
            response.raise_for_status()
        except (httpx.HTTPError, ValueError):
            logger.warning("failed to register MCP server %r in %s", name, directory, exc_info=True)

    async def create_session(
        self,
        title: str,
        *,
        directory: str,
        permission: list[dict[str, str]] | None = None,
        mcp: dict[str, Any] | None = None,
    ) -> str:
        """Create a new session in ``directory`` (a context's workspace); return
        its id.

        ``permission`` is the session's native ruleset (see
        :mod:`balam.permissions`); it defaults to :data:`ASK_ALL_PERMISSIONS` so a
        caller that doesn't compute one still gets the ask-everything baseline.

        ``mcp`` is the context's MCP server map (:mod:`balam.contexts`); each server
        is registered with OpenCode (scoped to ``directory``) *before* the session
        is created, so its tools are available to the very first turn.
        """
        for server_name, server_config in (mcp or {}).items():
            await self.register_mcp(server_name, server_config, directory=directory)
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

    async def update_session_permission(
        self, session_id: str, *, directory: str, permission: list[dict[str, str]]
    ) -> None:
        """Replace an existing session's permission ruleset.

        Session permissions are stored by OpenCode at creation time. Syncing them
        on reuse makes ``config.yaml`` changes effective for existing topics too.

        Best-effort, like :meth:`register_mcp` / :meth:`abort_session`: this runs
        in :meth:`balam.router.Router.resolve`'s hot path on *every* message to a
        live session, so a transient PATCH failure must not abort the turn and
        drop the message. A failure is logged and the session keeps its existing
        (stale-but-working) ruleset.
        """
        try:
            response = await self._client.patch(
                f"/session/{session_id}",
                params={"directory": directory},
                json={"permission": permission},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning("failed to sync permissions for session %s", session_id, exc_info=True)

    async def prompt(
        self,
        session_id: str,
        text: str,
        *,
        directory: str,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        files: list[PromptFile] | None = None,
    ) -> None:
        """Send a user message and return immediately (``prompt_async``). The
        assistant's reply arrives over :meth:`events`, which is what lets us
        stream it back to Telegram as it is generated.

        ``provider``/``model`` select the context's model (OpenCode wants
        ``{providerID, modelID}``); ``effort`` maps to the prompt ``variant``.
        Each is omitted when unset so the server applies its own default.

        ``files`` become native OpenCode file parts (``FilePartInput``) appended
        to the message ``parts`` — the agent sees them directly (image vision, PDF,
        text) without a filesystem read (tier-1 plan §4).
        """
        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"type": "text", "text": text})
        for file in files or []:
            part: dict[str, Any] = {"type": "file", "mime": file.mime, "url": file.url}
            if file.filename:
                part["filename"] = file.filename
            parts.append(part)
        body: dict[str, Any] = {"parts": parts}
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

    async def reply_question(
        self,
        request_id: str,
        answers: list[list[str]],
        *,
        directory: str | None = None,
    ) -> None:
        """Answer a ``question.asked`` request (``POST /question/{id}/reply``)."""
        params = {"directory": directory} if directory else None
        response = await self._client.post(
            f"/question/{request_id}/reply",
            params=params,
            json={"answers": answers},
        )
        response.raise_for_status()

    async def reject_question(self, request_id: str, *, directory: str | None = None) -> None:
        """Reject a ``question.asked`` request (``POST /question/{id}/reject``)."""
        params = {"directory": directory} if directory else None
        try:
            response = await self._client.post(f"/question/{request_id}/reject", params=params)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning("failed to reject question %s", request_id, exc_info=True)

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
