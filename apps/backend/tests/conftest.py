"""Shared fixtures for the Mini App server tests.

Forging a valid ``initData`` (we hold the test bot token) lets us exercise the
*real* HMAC auth path, not a mock — the same check the trust boundary runs in
production (ADR-0008).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

import pytest

from balam.config import Config
from balam.contexts import ContextConfig, ContextsConfig
from balam.router import Router
from balam.store import SessionStore

BOT_TOKEN = "123456:TEST-bot-token-for-hmac"
OWNER_ID = 42


def make_init_data(
    *, bot_token: str = BOT_TOKEN, user_id: int = OWNER_ID, auth_date: int | None = None
) -> str:
    """Build a Telegram ``initData`` query string with a valid HMAC signature."""
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAabc",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def make_config() -> Callable[..., Config]:
    """Factory for a Config with the test trust boundary and sane overrides.

    Explicit init kwargs take precedence over the repo-root ``.env`` so tests are
    hermetic regardless of the developer's local environment.
    """

    def _make(**overrides: object) -> Config:
        base: dict[str, object] = {
            "telegram_bot_token": BOT_TOKEN,
            "allowed_telegram_user_id": OWNER_ID,
            "allowed_telegram_chat_id": None,
        }
        base.update(overrides)
        return Config(**base)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one committed file, isolated from global config."""
    env = {
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PATH": __import__("os").environ.get("PATH", ""),
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "hello.py").write_text("def hello():\n    return 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return tmp_path


@pytest.fixture
def router_with(make_config: Callable[..., Config]) -> Callable[[Path], Router]:
    """Factory: a Router whose default context points at ``directory``."""

    def _make(directory: Path) -> Router:
        contexts = ContextsConfig(
            default_context="balam",
            contexts={
                "balam": ContextConfig(directory=str(directory), description="Test"),
            },
        )
        # OpenCode is unused by the diff path; None keeps the fixture lightweight.
        return Router(SessionStore(":memory:"), None, contexts)  # type: ignore[arg-type]

    return _make
