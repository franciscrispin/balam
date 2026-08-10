"""Backend-selection + SDK auth settings (ADR-0013)."""

import logging

import pytest
from pydantic import ValidationError

from balam.config import Config, load_config

# _env_file=None keeps these hermetic — the real repo-root .env (which may set
# AGENT_BACKEND for a live run) must not leak into the defaults under test.
_BASE = {"telegram_bot_token": "t", "allowed_telegram_user_id": 1, "_env_file": None}


def test_agent_backend_defaults_to_opencode() -> None:
    cfg = Config(**_BASE)  # type: ignore[arg-type]
    assert cfg.agent_backend == "opencode"
    assert cfg.anthropic_api_key is None


def test_agent_backend_accepts_claude_sdk() -> None:
    cfg = Config(**_BASE, agent_backend="claude_sdk", anthropic_api_key="sk-x")  # type: ignore[arg-type]
    assert cfg.agent_backend == "claude_sdk"
    assert cfg.anthropic_api_key == "sk-x"


def test_agent_backend_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Config(**_BASE, agent_backend="bogus")  # type: ignore[arg-type]


def test_blank_sdk_auth_is_treated_as_unset() -> None:
    cfg = Config(**_BASE, anthropic_api_key="  ", claude_sdk_cli_path="")  # type: ignore[arg-type]
    assert cfg.anthropic_api_key is None
    assert cfg.claude_sdk_cli_path is None


def test_tool_stream_defaults_to_collapsed() -> None:
    cfg = Config(**_BASE)  # type: ignore[arg-type]
    assert cfg.tool_stream == "collapsed"


def test_tool_stream_accepts_full_and_blank_means_default() -> None:
    assert Config(**_BASE, tool_stream="full").tool_stream == "full"  # type: ignore[arg-type]
    assert Config(**_BASE, tool_stream=" ").tool_stream == "collapsed"  # type: ignore[arg-type]


def test_tool_stream_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Config(**_BASE, tool_stream="verbose")  # type: ignore[arg-type]


# --- BALAM_TIMEZONE: what a /schedule time means (ADR-0016) -------------------


def test_timezone_defaults_to_singapore() -> None:
    cfg = Config(**_BASE)  # type: ignore[arg-type]
    assert cfg.balam_timezone == "Asia/Singapore"
    assert cfg.timezone.key == "Asia/Singapore"


def test_timezone_accepts_another_zone_and_blank_means_default() -> None:
    assert Config(**_BASE, balam_timezone="UTC").timezone.key == "UTC"  # type: ignore[arg-type]
    assert Config(**_BASE, balam_timezone=" ").balam_timezone == "Asia/Singapore"  # type: ignore[arg-type]


def test_timezone_rejects_a_typo_at_load() -> None:
    # The whole point of validating here: a typo must fail at boot, next to the
    # other trust-boundary checks — not at 07:30 when the schedule doesn't fire.
    with pytest.raises(ValidationError):
        Config(**_BASE, balam_timezone="Asia/Singapura")  # type: ignore[arg-type]


# --- RICH_MESSAGES: deprecated — rich replies are the default -----------------


def test_rich_messages_defaults_to_true_and_blank_means_default() -> None:
    assert Config(**_BASE).rich_messages is True  # type: ignore[arg-type]
    # A stale "RICH_MESSAGES=" line in an old .env must mean "unset", not a
    # boot failure — the flag is deprecated, so old files still carry it.
    assert Config(**_BASE, rich_messages=" ").rich_messages is True  # type: ignore[arg-type]


def _load_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # load_config() reads the (hermetic_settings-stripped) environment, so the
    # required trust-boundary fields must come back as env vars.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "1")


def test_rich_messages_env_var_warns_deprecated(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _load_config_env(monkeypatch)
    monkeypatch.setenv("RICH_MESSAGES", "false")
    with caplog.at_level(logging.WARNING, logger="balam.config"):
        config = load_config()
    # The escape hatch still works, but boot says it is going away.
    assert config.rich_messages is False
    assert any("RICH_MESSAGES is deprecated" in r.message for r in caplog.records)


def test_rich_messages_unset_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _load_config_env(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="balam.config"):
        config = load_config()
    assert config.rich_messages is True
    assert not any("RICH_MESSAGES" in r.message for r in caplog.records)
