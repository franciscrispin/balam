"""Backend-selection + SDK auth settings (ADR-0013)."""

import pytest
from pydantic import ValidationError

from balam.config import Config

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


# --- the allowlist: owner plus ADDITIONAL_TELEGRAM_USER_IDS (ADR-0008) --------


def test_allowed_user_ids_defaults_to_just_the_owner() -> None:
    assert Config(**_BASE).allowed_user_ids == (1,)  # type: ignore[arg-type]


def test_allowed_user_ids_appends_additional_users() -> None:
    cfg = Config(**_BASE, additional_telegram_user_ids="222,333")  # type: ignore[arg-type]
    assert cfg.allowed_user_ids == (1, 222, 333)


def test_allowed_user_ids_tolerates_spaces_and_blanks() -> None:
    cfg = Config(**_BASE, additional_telegram_user_ids=" 222 , 333, ")  # type: ignore[arg-type]
    assert cfg.allowed_user_ids == (1, 222, 333)
    assert Config(**_BASE, additional_telegram_user_ids="  ").allowed_user_ids == (1,)  # type: ignore[arg-type]


def test_allowed_user_ids_keeps_the_owner_first_and_unique() -> None:
    # Listing the owner again must not duplicate them in the filter.
    cfg = Config(**_BASE, additional_telegram_user_ids="1,222")  # type: ignore[arg-type]
    assert cfg.allowed_user_ids == (1, 222)


def test_additional_user_ids_reject_non_numeric() -> None:
    with pytest.raises(ValidationError):
        Config(**_BASE, additional_telegram_user_ids="222,@bob")  # type: ignore[arg-type]


def test_additional_user_ids_reject_non_positive() -> None:
    # A negative id is a chat id pasted into the wrong variable.
    with pytest.raises(ValidationError):
        Config(**_BASE, additional_telegram_user_ids="-1001234567890")  # type: ignore[arg-type]
