"""The FastAPI Mini App server (ADR-0003/0006).

Runs in the same process as the bot (mounted from :mod:`balam.app`), bound to
127.0.0.1 (ADR-0007). Three jobs:

  1. Serve the built Mini App (``apps/frontend/dist``) as a static SPA.
  2. Expose the Mini App API under ``/api`` — every route gated by the
     ``initData`` trust boundary (:class:`balam.webapp_auth.RequireOwner`,
     ADR-0008).
  3. Emit the OpenAPI schema (``/openapi.json``) the frontend generates its
     TypeScript types from (ADR-0003).

Ships the **git diff viewer** and **markdown viewer** endpoints, plus the
**noVNC live-Chrome bridge** (``/api/vnc/ws`` + ``/api/browser/status``,
ADR-0006 — the WebSocket↔TCP bridge itself lives in :mod:`balam.vnc`).
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from balam.agent_tools import ToolScopes, create_send_file_tool, handle_rpc
from balam.config import Config
from balam.content_store import ContentStore
from balam.git_diff import DiffHunk, NotAGitRepo, get_hunks
from balam.router import Router
from balam.vnc import probe_vnc, vnc_websocket
from balam.webapp_auth import RequireOwner

logger = logging.getLogger(__name__)

# server.py -> .../apps/backend/src/balam/server.py; repo root is five parents up,
# matching balam.config (the built Mini App lives at apps/frontend/dist).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DIST_DIR = _REPO_ROOT / "apps" / "frontend" / "dist"


def _app_version() -> str:
    try:
        return version("balam")
    except PackageNotFoundError:  # running from source without an install
        return "0.0.0"


class AppInfo(BaseModel):
    """Identity of the running backend — a cheap wiring + auth probe."""

    name: str
    version: str


class DiffResponse(BaseModel):
    """The working-tree diff of a context, as pre-parsed hunks."""

    context: str
    hunks: list[DiffHunk]


class MarkdownContentResponse(BaseModel):
    """An ephemeral markdown snapshot (plan text, a sent ``.md`` file)."""

    title: str
    content: str


class BrowserStatus(BaseModel):
    """Whether the live browser stack (x11vnc) is reachable on the VM (ADR-0006)."""

    running: bool


def create_app(
    config: Config,
    router: Router,
    *,
    content_store: ContentStore | None = None,
    tool_scopes: ToolScopes | None = None,
    bot: Any | None = None,
    bot_username: str | None = None,
) -> FastAPI:
    """Build the FastAPI app: auth-gated API routes plus the static Mini App."""
    # The app is reachable over the internet (ADR-0013), so don't serve the
    # interactive docs or the HTTP OpenAPI route — they need no auth and would
    # disclose the API shape. Type generation reads the schema in-process
    # (scripts/dump_openapi.py), so nothing is lost.
    app = FastAPI(
        title="Balam Mini App",
        version=_app_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    require_owner = RequireOwner(
        bot_token=config.telegram_bot_token,
        allowed_user_id=config.allowed_telegram_user_id,
    )
    store = content_store if content_store is not None else ContentStore()

    @app.get("/api/app-info", response_model=AppInfo)
    async def app_info(_owner: int = Depends(require_owner)) -> AppInfo:
        return AppInfo(name="balam", version=_app_version())

    @app.get("/api/diff", response_model=DiffResponse)
    async def diff(
        context: str | None = Query(default=None),
        _owner: int = Depends(require_owner),
    ) -> DiffResponse:
        name = context or router.contexts.default_context
        if name not in router.contexts.contexts:
            available = ", ".join(sorted(router.contexts.contexts))
            raise HTTPException(
                status_code=404, detail=f"unknown context {name!r}. Available: {available}"
            )
        ctx = router.contexts.contexts[name]
        try:
            hunks = await get_hunks(ctx.directory)
        except NotAGitRepo as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return DiffResponse(context=name, hunks=hunks)

    @app.get("/api/markdown/content/{content_id}", response_model=MarkdownContentResponse)
    async def markdown_content(
        content_id: str,
        _owner: int = Depends(require_owner),
    ) -> MarkdownContentResponse:
        entry = store.get(content_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="content not found or expired")
        return MarkdownContentResponse(title=entry.title, content=entry.content)

    @app.get("/api/browser/status", response_model=BrowserStatus)
    async def browser_status(_owner: int = Depends(require_owner)) -> BrowserStatus:
        return BrowserStatus(running=await probe_vnc(config.balam_vnc_host, config.balam_vnc_port))

    # The RFB byte stream for the live browser view. Not in the OpenAPI schema
    # (WebSocket routes never are): the contract is the client's initData as the
    # first text frame, then raw RFB bytes (a browser can't set an Authorization
    # header on a WebSocket, and a query param would leak into uvicorn's log).
    @app.websocket("/api/vnc/ws")
    async def vnc_ws(websocket: WebSocket) -> None:
        await vnc_websocket(
            websocket,
            bot_token=config.telegram_bot_token,
            allowed_user_id=config.allowed_telegram_user_id,
            host=config.balam_vnc_host,
            port=config.balam_vnc_port,
        )

    if tool_scopes is not None and bot is not None:
        # Balam's own MCP server (balam.agent_tools): OpenCode calls this over
        # localhost. Out of the schema — it is server-to-server, not part of the
        # Mini App contract the TS types are generated from.
        @app.post("/mcp/{scope_token}", include_in_schema=False)
        async def mcp(scope_token: str, request: Request) -> Response:
            # The unguessable token is the auth (OpenCode dials 127.0.0.1
            # directly); requests that crossed the Cloudflare tunnel are not it.
            if "cf-connecting-ip" in request.headers:
                raise HTTPException(status_code=404, detail="not found")
            scope = tool_scopes.get(scope_token)
            if scope is None:
                raise HTTPException(status_code=404, detail="unknown tool scope")
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                    status_code=400,
                )
            tools = [
                create_send_file_tool(
                    bot,
                    scope.chat_id,
                    scope.thread_id,
                    config=config,
                    bot_username=bot_username,
                    content_store=store,
                )
            ]
            reply = await handle_rpc(body, tools)
            if reply is None:
                return Response(status_code=202)
            return JSONResponse(reply)

    # Serve the built SPA last so the API routes above win. html=True serves
    # index.html for "/" and 404s unknown asset paths.
    if _DIST_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_DIST_DIR), html=True), name="mini-app")
    else:
        logger.warning(
            "Mini App build not found at %s — API is up but the SPA is not served. "
            "Run `bun run build` from the repo root.",
            _DIST_DIR,
        )

    return app


def openapi_schema() -> dict:
    """The app's OpenAPI schema, built with placeholder config/router.

    The Mini App contract is the FastAPI-emitted OpenAPI schema (ADR-0003); the
    frontend's TypeScript types are generated from this (see ``scripts/dump_openapi.py``
    and the root ``gen:api`` script). The placeholders never serve a request, so
    no real bot token or workspace is needed.
    """
    from balam.contexts import ContextConfig, ContextsConfig
    from balam.store import SessionStore

    config = Config.model_construct(telegram_bot_token="placeholder", allowed_telegram_user_id=0)
    contexts = ContextsConfig(
        default_context="default",
        contexts={"default": ContextConfig(directory=".", description="placeholder")},
    )
    router = Router(SessionStore(":memory:"), None, contexts)  # type: ignore[arg-type]
    return create_app(config, router).openapi()
