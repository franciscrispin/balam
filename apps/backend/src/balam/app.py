"""Balam backend entry point (ADR-0011).

Boot sequence:
  1. Validate configuration — fail fast on a bad trust boundary (ADR-0008).
  2. Wait for the OpenCode server — we are its client (ADR-0001). Done in PTB's
     ``post_init`` hook, before polling starts.
  3. Open the SQLite topic→session map (ADR-0009).
  4. Start the FastAPI Mini App server (ADR-0003) as an asyncio task alongside
     the bot, bound to 127.0.0.1 (ADR-0007). Done in ``post_init``.
  5. Start the bot via long polling (ADR-0007: no public URL).
  6. Stop the Mini App server, close OpenCode + SQLite on shutdown
     (``post_shutdown``).

The same FastAPI server also bridges the noVNC WebSocket for the live-Chrome
view (ADR-0006, :mod:`balam.vnc`).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn
from telegram.ext import Application

from balam.agent_tools import ToolScopes
from balam.bot import build_application, register_commands
from balam.config import ConfigError, load_config
from balam.content_store import ContentStore
from balam.contexts import ContextsConfigError, load_contexts
from balam.opencode import OpenCode
from balam.router import Router
from balam.server import create_app
from balam.store import SessionStore

logger = logging.getLogger("balam")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    try:
        config = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        contexts = load_contexts(config.config_path)
    except ContextsConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    opencode = OpenCode(
        base_url=config.opencode_base_url,
        username=config.opencode_server_username,
        password=config.opencode_server_password,
    )
    store = SessionStore(config.db_path)
    # The viewer snapshots + per-topic tool scopes feed both the bot (buttons,
    # send_file delivery) and the FastAPI server (the /api + /mcp routes).
    content_store = ContentStore()
    tool_scopes = ToolScopes()
    router = Router(
        store,
        opencode,
        contexts,
        tool_scopes=tool_scopes,
        # OpenCode dials Balam's own Mini App server for agent tools (ADR-0007:
        # both live on this VM, loopback only).
        mcp_base_url=f"http://127.0.0.1:{config.balam_port}",
        qualify_chat=config.allowed_telegram_chat_id is None,
    )

    # The Mini App server runs in the bot's event loop (one asyncio task), so the
    # bot, OpenCode SSE, and HTTP share a process (ADR-0007). Created in
    # post_init (inside the loop), torn down in post_shutdown.
    server: uvicorn.Server | None = None

    async def _post_init(application: Application) -> None:
        nonlocal server
        # Publish slash commands so /context is discoverable and routed to the
        # bot in the workspace group (clients dispatch group commands by the
        # registered list, not just by delivery).
        await register_commands(application.bot, config.allowed_telegram_chat_id)
        logger.info("registered bot commands (chat scope %s)", config.allowed_telegram_chat_id)
        logger.info("waiting for OpenCode at %s ...", config.opencode_base_url)
        await opencode.wait_for_ready()
        logger.info("OpenCode is ready.")

        # The bot is initialized by post_init time, so its username is available
        # for t.me Mini App links; bot_data carries the store to the streamer's
        # plan-view button.
        application.bot_data["content_store"] = content_store
        api = create_app(
            config,
            router,
            content_store=content_store,
            tool_scopes=tool_scopes,
            bot=application.bot,
            bot_username=application.bot.username,
        )
        server = uvicorn.Server(
            uvicorn.Config(api, host="127.0.0.1", port=config.balam_port, log_level="info")
        )
        # serve() runs until server.should_exit; keep the task so post_shutdown
        # can stop it (and the loop's weak task ref can't GC it mid-flight).
        application.bot_data["uvicorn_task"] = asyncio.create_task(server.serve())
        logger.info("Mini App server listening on http://127.0.0.1:%s", config.balam_port)

    async def _post_shutdown(application: Application) -> None:
        try:
            if server is not None:
                server.should_exit = True
                task = application.bot_data.get("uvicorn_task")
                if task is not None:
                    await task
        finally:
            # Always release process resources, even if the server task raised
            # (e.g. a bind failure surfacing here) — its exception must not skip
            # OpenCode/SQLite teardown.
            await opencode.aclose()
            store.close()

    app = build_application(
        config, opencode, router, post_init=_post_init, post_shutdown=_post_shutdown
    )

    logger.info(
        "starting bot (owner %s, chat %s, contexts %s, default %s) ...",
        config.allowed_telegram_user_id,
        config.allowed_telegram_chat_id or "any",
        sorted(contexts.contexts),
        contexts.default_context,
    )
    # run_polling blocks, manages the event loop, and runs post_init/post_shutdown
    # plus graceful shutdown on SIGINT/SIGTERM. We need both ``message`` (the
    # round-trip) and ``callback_query`` (taps on the tool-approval inline
    # keyboard, ADR-0012) delivered — Telegram omits any update type not listed.
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
