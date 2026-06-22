"""Load and validate the backend's environment configuration (ADR-0008/0007).

A misconfigured trust boundary (ADR-0008) or a missing OpenCode endpoint
(ADR-0001/0007) must never boot half-working, so this fails fast with a single,
clear message listing every problem. Real environment variables (e.g. from the
systemd unit) take precedence over the repo-root ``.env`` used in local dev.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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
    # Optional: scope the bot to a single forum supergroup (ADR-0010). When set,
    # handlers gate on this chat id in addition to the owner id; unset → the
    # legacy owner-anywhere behavior.
    allowed_telegram_chat_id: int | None = None

    # --- Agent backend (ADR-0013) ---
    # Which coding-agent runtime drives Balam: the OpenCode server (default) or
    # the in-process Claude Agent SDK. The OpenCode settings below matter only for
    # "opencode"; the SDK auth below matters only for "claude_sdk".
    agent_backend: Literal["opencode", "claude_sdk"] = "opencode"

    # --- OpenCode server (ADR-0001/0002/0007) ---
    opencode_base_url: str = "http://127.0.0.1:4096"
    opencode_server_username: str = "opencode"
    opencode_server_password: str | None = None

    # --- Claude Agent SDK (ADR-0013) ---
    # API key for the SDK's subprocess. Optional: if unset, the SDK falls back to
    # ANTHROPIC_API_KEY in the environment or an already-authenticated Claude CLI
    # (e.g. a subscription login), so we never hard-require it here.
    anthropic_api_key: str | None = None
    # Override the bundled `claude` CLI path the SDK spawns, if needed.
    claude_sdk_cli_path: str | None = None

    # --- Balam backend ---
    balam_db_path: str | None = None
    balam_config_path: str | None = None
    # Port the FastAPI Mini App server listens on (Mini App + API), bound to
    # 127.0.0.1 (ADR-0007). Mirrors BALAM_PORT in .env.example.
    balam_port: int = 3000
    # Public HTTPS base URL the Mini App is reachable at (e.g. a tunnel). When
    # set, /diff offers a native in-Telegram ``web_app`` button (Telegram requires
    # HTTPS); unset → /diff replies with the local 127.0.0.1 URL to open in a
    # browser (ADR-0007: no public URL by default). No trailing slash.
    balam_public_url: str | None = None
    # BotFather Mini App short name (ADR-0013). When set, /diff sends a direct
    # Mini App link ``t.me/<bot>/<shortname>?startapp=…`` that opens the app inside
    # Telegram's webview in ANY chat type (groups included) — unlike a ``web_app``
    # inline button, which Telegram permits only in private chats.
    balam_miniapp_shortname: str | None = None

    # --- noVNC live browser view (ADR-0006) ---
    # The x11vnc server exposing the agent's headed Chrome (started on demand by
    # the browser-use skill, .claude/skills/browser-use/headed-browser/). The
    # backend bridges /api/vnc/ws straight to this TCP endpoint; it never starts
    # the stack itself.
    balam_vnc_host: str = "127.0.0.1"
    balam_vnc_port: int = 5900

    @field_validator(
        "opencode_server_password",
        "anthropic_api_key",
        "claude_sdk_cli_path",
        "balam_db_path",
        "balam_config_path",
        "allowed_telegram_chat_id",
        "balam_public_url",
        "balam_miniapp_shortname",
        mode="before",
    )
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
    def db_path(self) -> str:
        """SQLite file backing the topic→session map (ADR-0009)."""
        return self.balam_db_path or "balam.sqlite"

    @property
    def config_path(self) -> str:
        """The (mandatory) ``config.yaml`` defining workspace contexts; repo-root
        by default. :func:`balam.contexts.load_contexts` fails fast if it is
        absent."""
        return self.balam_config_path or str(_REPO_ROOT / "config.yaml")


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
