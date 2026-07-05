"""Shared parser for a context's ``mcp`` server entries (ADR-0012/0014).

Both agent backends register the same ``config.yaml`` MCP servers, but each
wants its own wire shape: OpenCode takes ``local``/``remote`` dicts
(:func:`balam.opencode.coerce_mcp_config`), the Claude Agent SDK takes
``stdio``/``http``/``sse`` TypedDicts
(:func:`balam.agent.claude_sdk_backend.coerce_sdk_mcp_config`). Parsing and
validation live here, once, so both backends accept and reject exactly the same
configs; the two coerce functions are thin serializers over the
:class:`McpServerSpec` this module produces.

Accepted spellings (the looser ``opencode.json`` forms, kept ``config.yaml``
friendly):

* shorthand — a bare ``command`` *string* plus optional ``args``/``env``
* ``type: local`` — ``command`` as a non-empty list of strings
* ``type: remote|http|sse`` — a ``url``, optional ``headers``/``oauth``

A malformed entry raises :class:`ValueError`, which the context loader turns
into a fatal, fail-fast boot error (so a bad server is caught at startup, never
silently mid-conversation).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """One normalized MCP server entry, before backend-specific serialization."""

    kind: Literal["local", "remote"]
    #: Local: the full command line (executable first). Empty for remote.
    command: tuple[str, ...] = ()
    #: Local: process environment, or None when the entry sets none.
    environment: dict[str, str] | None = None
    #: Remote: the server URL.
    url: str | None = None
    #: Remote: extra request headers, or None when the entry sets none.
    headers: dict[str, str] | None = None
    #: Remote: the transport as spelled in config (``remote`` implies http).
    transport: Literal["remote", "http", "sse"] | None = None
    #: Remote: OpenCode's oauth toggle (pass-through; the SDK shape drops it).
    oauth: bool | None = None
    #: The enabled toggle (OpenCode: pass-through; SDK: false skips registration).
    enabled: bool | None = None


def _parse_env(name: str, env: Any) -> dict[str, str] | None:
    if env is None:
        return None
    if not isinstance(env, dict):
        raise ValueError(f"MCP server {name!r} environment must be a mapping")
    return {str(k): str(v) for k, v in env.items()}


def parse_mcp_config(name: str, raw_config: Any) -> McpServerSpec:
    """Validate one context MCP server entry and normalize it to a spec."""
    if not isinstance(name, str) or not name:
        raise ValueError("MCP server name must be a non-empty string")
    if not isinstance(raw_config, dict):
        raise ValueError(f"MCP server {name!r} config must be a mapping")
    config = dict(raw_config)

    enabled = bool(config["enabled"]) if "enabled" in config else None

    # `command: "uvx"` + `args: [...]` shorthand → a single local command list.
    if "command" in config and config.get("type") not in {"local", "remote", "http", "sse"}:
        command = config["command"]
        if not isinstance(command, str) or not command:
            raise ValueError(f"MCP server {name!r} command must be a non-empty string")
        args = config.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError(f"MCP server {name!r} args must be a list of strings")
        env = _parse_env(name, config.get("env", config.get("environment")))
        return McpServerSpec(
            kind="local", command=(command, *args), environment=env, enabled=enabled
        )

    cfg_type = config.get("type")
    if cfg_type in {"remote", "http", "sse"}:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError(f"MCP server {name!r} remote config requires a url")
        headers: dict[str, str] | None = None
        if "headers" in config:
            raw_headers = config["headers"]
            if not isinstance(raw_headers, dict):
                raise ValueError(f"MCP server {name!r} headers must be a mapping")
            headers = {str(k): str(v) for k, v in raw_headers.items()}
        oauth = bool(config["oauth"]) if "oauth" in config else None
        return McpServerSpec(
            kind="remote",
            url=url,
            headers=headers,
            transport=cfg_type,
            oauth=oauth,
            enabled=enabled,
        )

    if cfg_type == "local":
        command = config.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) for arg in command)
        ):
            raise ValueError(
                f"MCP server {name!r} local command must be a non-empty list of strings"
            )
        env = _parse_env(name, config.get("environment", config.get("env")))
        return McpServerSpec(kind="local", command=tuple(command), environment=env, enabled=enabled)

    raise ValueError(
        f"MCP server {name!r} must be a local server (command) or a remote server "
        f"(type: remote/http/sse + url); got {raw_config!r}"
    )
