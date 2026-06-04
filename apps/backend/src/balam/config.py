"""Load and validate the backend's environment configuration (ADR-0008/0007).

A misconfigured trust boundary (ADR-0008) or a missing OpenCode endpoint
(ADR-0001/0007) must never boot half-working, so this fails fast with a single,
clear message listing every problem. Real environment variables (e.g. from the
systemd unit) take precedence over the repo-root ``.env`` used in local dev.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/backend/src/balam/config.py -> repo root is five parents up.
_REPO_ROOT = Path(__file__).resolve().parents[4]


class ConfigError(Exception):
    """Raised when one or more required settings are missing or invalid."""

    def __init__(self, problems: list[str]) -> None:
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            "Invalid configuration:\n"
            f"{body}\n"
            "Copy .env.example to .env and fill in the missing values."
        )


class Config(BaseSettings):
    """Validated settings, read from the environment / repo-root ``.env``."""

    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram trust boundary (ADR-0008) ---
    telegram_bot_token: str
    allowed_telegram_user_id: int

    # --- OpenCode server (ADR-0001/0002/0007) ---
    opencode_base_url: str = "http://127.0.0.1:4096"
    opencode_server_username: str = "opencode"
    opencode_server_password: str | None = None

    # --- Balam backend ---
    balam_workdir: str | None = None
    balam_db_path: str | None = None

    @field_validator("opencode_server_password", "balam_workdir", "balam_db_path", mode="before")
    @classmethod
    def _blank_to_default(cls, value: object) -> object:
        # Treat an empty/whitespace env value as "unset" so defaults apply.
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("allowed_telegram_user_id")
    @classmethod
    def _positive_user_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer (your numeric Telegram user ID)")
        return value

    @property
    def workdir(self) -> str:
        """Directory the agent acts on; defaults to the current directory."""
        return self.balam_workdir or os.getcwd()

    @property
    def db_path(self) -> str:
        """SQLite file backing the topic→session map (ADR-0009)."""
        return self.balam_db_path or "balam.sqlite"


def load_config() -> Config:
    """Build a validated :class:`Config`, or raise :class:`ConfigError` listing
    every problem at once so the operator fixes them in a single pass."""
    try:
        return Config()  # type: ignore[call-arg]  # values come from env/.env
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors():
            field = ".".join(str(part) for part in err["loc"]) or "(root)"
            problems.append(f"{field.upper()}: {err['msg']}")
        raise ConfigError(problems) from exc
