"""Shared fixtures for the backend test suite.

Forging a valid ``initData`` (we hold the test bot token) lets us exercise the
*real* HMAC auth path, not a mock — the same check the trust boundary runs in
production (ADR-0008).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator
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


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate :class:`Config` from whatever the machine happens to export.

    ``Config`` is a pydantic-settings model, so it reads real environment
    variables *ahead of* the repo-root ``.env``. Balam itself runs under systemd
    with its whole deployment environment set, so an agent session started by the
    bot inherits it — without this, the suite fails 5 tests there that pass in a
    plain shell and in CI. That asymmetry is expensive to debug, so the isolation
    lives here rather than in individual tests.

    Note that ``_env_file=None`` alone does **not** achieve this: it only
    disables the file, and real environment variables bind at higher precedence.
    Both halves are needed, so this fixture does both for every test.

    Names come from the model itself, so a newly added setting is covered without
    touching this list.
    """
    for name in [key for key in os.environ if key.lower() in Config.model_fields]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Config.model_config, "env_file", None)
    yield


@pytest.fixture
def make_config() -> Callable[..., Config]:
    """Factory for a Config with the test trust boundary and sane overrides.

    Ambient environment and the repo-root ``.env`` are already neutralised by the
    autouse :func:`hermetic_settings` fixture, so the only values in play are the
    defaults below and whatever a caller overrides.
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
