"""The shared MCP entry parser (balam.mcp_config).

Serialization is covered by the backend-specific tests (test_opencode.py,
test_claude_sdk_backend.py); this pins the consolidated validation — both
backends must accept and reject exactly the same config.yaml entries.
"""

import pytest

from balam.agent.claude_sdk_backend import coerce_sdk_mcp_config
from balam.mcp_config import McpServerSpec, parse_mcp_config
from balam.opencode import coerce_mcp_config


def test_parse_shorthand_command() -> None:
    spec = parse_mcp_config("db", {"command": "postgres-mcp", "args": ["-x"], "env": {"K": "v"}})
    assert spec == McpServerSpec(
        kind="local", command=("postgres-mcp", "-x"), environment={"K": "v"}
    )


def test_parse_remote_keeps_transport_spelling() -> None:
    assert parse_mcp_config("api", {"type": "sse", "url": "http://h/sse"}).transport == "sse"
    assert parse_mcp_config("api", {"type": "remote", "url": "http://h"}).transport == "remote"


@pytest.mark.parametrize(
    "bad",
    [
        {"nonsense": True},
        {"type": "remote"},  # missing url
        {"type": "local", "command": []},  # empty command line
        {"type": "local", "command": ["srv", 1]},  # non-string argv
        {"command": "srv", "args": [1]},  # non-string args
        {"command": "srv", "env": "notamap"},
        {"type": "http", "url": "http://h", "headers": "notamap"},
    ],
)
def test_both_backends_reject_the_same_entries(bad: dict) -> None:
    with pytest.raises(ValueError):
        coerce_mcp_config("bad", bad)
    with pytest.raises(ValueError):
        coerce_sdk_mcp_config("bad", bad)
