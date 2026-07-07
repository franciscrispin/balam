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
