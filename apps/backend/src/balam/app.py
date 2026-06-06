"""Balam backend entry point (ADR-0011).

Boot sequence:
  1. Validate configuration — fail fast on a bad trust boundary (ADR-0008).
  2. Wait for the OpenCode server — we are its client (ADR-0001). Done in PTB's
     ``post_init`` hook, before polling starts.
  3. Open the SQLite topic→session map (ADR-0009).
  4. Start the bot via long polling (ADR-0007: no public URL).
  5. Close OpenCode + SQLite on shutdown (``post_shutdown``).

TODO(ADR-0003/0006): a later slice mounts the FastAPI Mini App server (serving
the Mini App, exposing the OpenAPI schema, reverse-proxying the noVNC WebSocket)
alongside the bot.
"""

from __future__ import annotations

import logging
import sys

from balam.bot import build_application, register_commands
from balam.config import ConfigError, load_config
from balam.contexts import ContextsConfigError, load_contexts
from balam.opencode import OpenCode
from balam.router import Router
from balam.store import SessionStore
from telegram.ext import Application

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
    router = Router(store, opencode, contexts)

    async def _post_init(application: Application) -> None:
        # Publish slash commands so /context is discoverable and routed to the
        # bot in the workspace group (clients dispatch group commands by the
        # registered list, not just by delivery).
        await register_commands(application.bot, config.allowed_telegram_chat_id)
        logger.info(
            "registered bot commands (chat scope %s)", config.allowed_telegram_chat_id
        )
        logger.info("waiting for OpenCode at %s ...", config.opencode_base_url)
        await opencode.wait_for_ready()
        logger.info("OpenCode is ready.")

    async def _post_shutdown(_application: Application) -> None:
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
