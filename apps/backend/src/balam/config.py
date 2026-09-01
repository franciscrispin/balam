"""Load and validate the backend's environment configuration (ADR-0008/0007).

A misconfigured trust boundary (ADR-0008) or a missing OpenCode endpoint
(ADR-0001/0007) must never boot half-working, so this fails fast with a single,
clear message listing every problem. Real environment variables (e.g. from the
systemd unit) take precedence over the repo-root ``.env`` used in local dev.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Fallback when ``BALAM_TIMEZONE`` is unset or blank. The VM runs UTC but the
#: owner does not, and a schedule is written in the owner's wall clock.
DEFAULT_TIMEZONE = "Asia/Singapore"

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


def _parse_user_ids(raw: str) -> tuple[int, ...]:
    """Parse a comma- or space-separated list of Telegram user ids.

    Deliberately a ``str`` field parsed here rather than a ``list[int]`` field:
    pydantic-settings JSON-decodes complex types from the environment, so a
    ``list[int]`` would need ``[222,333]`` in ``.env`` and would fail obscurely on
    the ``222,333`` anyone would actually write.
    """
    ids: list[int] = []
    for token in raw.replace(",", " ").split():
        try:
            value = int(token)
        except ValueError as exc:
            raise ValueError(f"{token!r} is not a numeric Telegram user ID") from exc
        if value <= 0:
            raise ValueError(f"{value} must be a positive integer (a numeric Telegram user ID)")
        ids.append(value)
    return tuple(ids)


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
    # Optional: other Telegram user ids allowed to drive the bot, comma-separated
    # ("222333444,555666777"). They are inside the same trust boundary as the
    # owner (ADR-0008), not a lesser one: turns run as the same OS user, with the
    # owner's files, credentials and agent session, so add only people you trust
    # with this machine. Pair with ALLOWED_TELEGRAM_CHAT_ID so they are confined
    # to the workspace supergroup instead of also getting a private bot.
    additional_telegram_user_ids: str = ""

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
    # IANA timezone every ``/schedule`` is resolved against (ADR-0016). The VM's
    # clock is UTC and the owner's is not, so "daily 07:30" would otherwise mean
    # 07:30 UTC — a silent 8-hour error that only shows up at the wrong hour.
    # Validated at load (below) so a typo fails at boot, not at 07:30.
    balam_timezone: str = DEFAULT_TIMEZONE

    # --- Streaming verbosity ---
    # How tool calls render in the progress stream. "collapsed" folds a burst of
    # consecutive calls into one summary line ("Ran 3 commands, read a file")
    # with the per-call detail inside a tap-to-expand blockquote; "full" keeps
    # the legacy one-line-per-call stream. Mirrors TOOL_STREAM in .env.example.
    tool_stream: Literal["collapsed", "full"] = "collapsed"

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

    @field_validator("tool_stream", mode="before")
    @classmethod
    def _blank_tool_stream_to_default(cls, value: object) -> object:
        # A blank TOOL_STREAM means "unset"; the field is non-optional so the
        # generic blank→None validator above would fail its Literal check.
        if isinstance(value, str) and value.strip() == "":
            return "collapsed"
        return value

    @field_validator("balam_timezone", mode="before")
    @classmethod
    def _blank_timezone_to_default(cls, value: object) -> object:
        # Same reason as TOOL_STREAM: the field is a plain ``str``, so a blank
        # env value must fall back to the default rather than become ``None``.
        if isinstance(value, str) and value.strip() == "":
            return DEFAULT_TIMEZONE
        return value

    @field_validator("balam_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        # Fail fast next to the other trust-boundary checks: a typo'd zone would
        # otherwise surface as a schedule firing at the wrong hour, or not at all.
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r} (e.g. Asia/Singapore, UTC)") from exc
        return value

    @field_validator("allowed_telegram_user_id")
    @classmethod
    def _positive_user_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer (your numeric Telegram user ID)")
        return value

    @field_validator("additional_telegram_user_ids")
    @classmethod
    def _parseable_user_ids(cls, value: str) -> str:
        # Parse at load so a typo'd id is a boot error, not a person who silently
        # can't talk to the bot.
        _parse_user_ids(value)
        return value

    @property
    def allowed_user_ids(self) -> tuple[int, ...]:
        """Every Telegram user id allowed to drive the bot (ADR-0008): the owner
        first, then ``ADDITIONAL_TELEGRAM_USER_IDS``.

        The single list the message filter, the callback checks and the Mini App
        auth all gate on, so widening the allowlist happens in one place. The
        owner stays distinguishable because some things still mean *the owner*
        specifically — the account the agent runs as, and who needs no sender
        attribution in a prompt.
        """
        extra = tuple(
            uid
            for uid in _parse_user_ids(self.additional_telegram_user_ids)
            if uid != self.allowed_telegram_user_id
        )
        return (self.allowed_telegram_user_id, *extra)

    @property
    def db_path(self) -> str:
        """SQLite file backing the topic→session map (ADR-0009)."""
        return self.balam_db_path or "balam.sqlite"

    @property
    def timezone(self) -> ZoneInfo:
        """The zone ``/schedule`` times are written in (ADR-0016). Validated at
        load, so constructing it here cannot fail."""
        return ZoneInfo(self.balam_timezone)

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
