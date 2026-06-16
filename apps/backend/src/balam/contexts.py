"""Named workspace contexts, loaded from ``config.yaml`` (adapted from OpenShrimp).

A *context* bundles a working directory with an optional model/effort and a tool
permission profile, so one bot can drive several projects. ``config.yaml`` is
mandatory — Balam will not boot without at least one context defined. Secrets and
infra connection stay in ``.env`` (:mod:`balam.config`); only this structured map
lives in YAML, where it is expressed far more naturally than in flat environment
variables.

Each Telegram topic binds to one context, persisted alongside its session in the
topic→session map (:mod:`balam.store`); a topic with no binding uses
``default_context``.

``allowed_tools`` and ``additional_directories`` are translated into a native
OpenCode permission ruleset by :mod:`balam.permissions` (the opt-in half of the
hybrid enforcement model): pre-approved tools become ``allow`` rules so OpenCode
runs them without prompting, and extra directories get ``external_directory``
grants. Everything not pre-approved stays ``ask`` and falls through to the local,
symlink-safe approval layer (:mod:`balam.approvals`).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from balam.opencode import coerce_mcp_config

#: Thinking-effort levels OpenCode accepts (sent as the ``variant`` on a prompt).
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}

#: ``${VAR}`` references expanded inside ``mcp`` config values, so secrets (DB
#: URIs, bearer tokens) live in ``.env`` and never in ``config.yaml`` — matching
#: Balam's secrets-in-``.env`` rule (:mod:`balam.config`). Names follow the usual
#: env-var spelling.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Context names travel inside Telegram Mini App ``start_param`` tokens
#: (``"<view>__<context>"``, :mod:`balam.miniapp`), which Telegram caps at 64
#: chars of ``[A-Za-z0-9_-]`` — anything else is silently dropped and the Mini
#: App opens on the default view/context. Hence: charset restricted, no ``__``
#: (the view/param separator), nothing shaped like the ``c_<hex>`` content-id
#: marker the frontend's ``resolveLaunch`` strips, and short enough to fit the
#: budget behind the longest view prefix (``markdown__``).
_CONTEXT_NAME = re.compile(r"[A-Za-z0-9_-]+")
_CONTENT_ID_MARKER = re.compile(r"c_[0-9a-f]{6,}")
_CONTEXT_NAME_MAX = 64 - len("markdown__")


def _expand_env(value: Any, *, where: str) -> Any:
    """Recursively expand ``${VAR}`` references in ``value`` from the environment.

    Strings have every ``${VAR}`` replaced with ``os.environ[VAR]``; dicts and
    lists are walked; everything else is returned unchanged. An unset variable
    raises :class:`ValueError` so a missing secret fails fast at boot rather than
    silently registering a broken MCP server. Under systemd the backend's ``.env``
    is loaded into the environment via ``EnvironmentFile``, so these resolve there.
    """
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            try:
                return os.environ[name]
            except KeyError:
                raise ValueError(
                    f"{where}: environment variable ${{{name}}} is not set (define it in .env)"
                ) from None

        return _ENV_REF.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, where=where) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, where=where) for v in value]
    return value


def split_provider_model(model: str | None) -> tuple[str | None, str | None]:
    """Split a context's ``model`` into ``(provider, model)``.

    OpenCode wants ``provider/model`` (e.g. ``anthropic/claude-opus-4-8``); the
    Claude Agent SDK takes a bare Claude id/alias (e.g. ``claude-opus-4-8`` or
    ``opus``). A bare value is therefore allowed and returns ``(None, model)`` —
    OpenCode only sends a model when both parts are present (so a bare value falls
    back to its default), while the SDK uses the bare id directly.
    """
    if not model:
        return None, None
    if "/" not in model:
        return None, model
    provider, _, rest = model.partition("/")
    return provider, rest


class ContextConfig(BaseModel):
    """One named workspace the agent can act in."""

    model_config = ConfigDict(extra="forbid")

    directory: str
    description: str
    allowed_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    additional_directories: list[str] = Field(default_factory=list)
    #: MCP servers exposed to this context's sessions, keyed by server name.
    #: Each value is a local (stdio) or remote (http/sse) server config in
    #: OpenCode's shape; ``${VAR}`` references in values are filled from the
    #: environment. Registered with OpenCode before each session is created
    #: (:meth:`balam.opencode.OpenCode.register_mcp`).
    mcp: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mcp", mode="after")
    @classmethod
    def _mcp_resolved_and_valid(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Expand ${VAR} secrets first, then validate the resolved shape against
        # OpenCode's wire format so a malformed server fails fast at boot.
        expanded = _expand_env(value, where="mcp")
        for name, server in expanded.items():
            coerce_mcp_config(name, server)  # raises ValueError on a bad entry
        return expanded

    @field_validator("model")
    @classmethod
    def _model_is_provider_qualified(cls, value: str | None) -> str | None:
        split_provider_model(value)  # raises on a bad value
        return value

    @field_validator("effort")
    @classmethod
    def _effort_is_known(cls, value: str | None) -> str | None:
        if value is not None and value not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {sorted(EFFORT_LEVELS)}, got {value!r}")
        return value

    @property
    def provider_model(self) -> tuple[str | None, str | None]:
        """``(provider, model)`` for the OpenCode prompt body, or ``(None, None)``."""
        return split_provider_model(self.model)


class ContextsConfig(BaseModel):
    """The whole ``config.yaml``: a set of contexts plus the default."""

    model_config = ConfigDict(extra="forbid")

    default_context: str
    contexts: dict[str, ContextConfig]

    @field_validator("contexts")
    @classmethod
    def _names_fit_start_param(cls, value: dict[str, ContextConfig]) -> dict[str, ContextConfig]:
        for name in value:
            if not _CONTEXT_NAME.fullmatch(name):
                raise ValueError(
                    f"context name {name!r} must use only letters, digits, '-' and '_' "
                    "(it travels in a Telegram Mini App start_param, which silently "
                    "drops anything else)"
                )
            if "__" in name:
                raise ValueError(
                    f"context name {name!r} must not contain '__' (the Mini App "
                    "start_param separator)"
                )
            if _CONTENT_ID_MARKER.fullmatch(name):
                raise ValueError(
                    f"context name {name!r} collides with the Mini App 'c_<hex>' "
                    "content-id marker — pick a name that isn't 'c_' plus hex digits"
                )
            if len(name) > _CONTEXT_NAME_MAX:
                raise ValueError(
                    f"context name {name!r} is too long ({len(name)} > "
                    f"{_CONTEXT_NAME_MAX}): it must fit Telegram's 64-char "
                    "start_param budget"
                )
        return value

    @model_validator(mode="after")
    def _check(self) -> ContextsConfig:
        if not self.contexts:
            raise ValueError("contexts must define at least one context")
        if self.default_context not in self.contexts:
            raise ValueError(
                f"default_context {self.default_context!r} not found in contexts: "
                f"{sorted(self.contexts)}"
            )
        return self

    def get(self, name: str | None) -> ContextConfig:
        """Resolve a context by name, falling back to ``default_context`` when
        the name is unset or no longer defined (e.g. a binding to a context that
        has since been removed from the file)."""
        if name and name in self.contexts:
            return self.contexts[name]
        return self.contexts[self.default_context]

    def resolve_name(self, name: str | None) -> str:
        """The context name :meth:`get` would use — for persisting the binding."""
        return name if (name and name in self.contexts) else self.default_context


class ContextsConfigError(Exception):
    """Raised when ``config.yaml`` is missing or malformed."""

    @classmethod
    def missing(cls, path: Path) -> ContextsConfigError:
        return cls(
            f"No contexts config found at {path}. config.yaml is required: copy "
            "config.example.yaml to config.yaml (or point BALAM_CONFIG_PATH at it) "
            "and define at least one context."
        )

    @classmethod
    def invalid(cls, path: Path, error: ValidationError) -> ContextsConfigError:
        problems = []
        for err in error.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "(root)"
            problems.append(f"  - {loc}: {err['msg']}")
        body = "\n".join(problems)
        return cls(
            f"Invalid contexts config at {path}:\n{body}\n"
            "See config.example.yaml for the expected shape."
        )


def load_contexts(path: str | Path | None) -> ContextsConfig:
    """Load and validate the mandatory ``config.yaml``.

    The file is required: there is no fallback workspace. A missing file or an
    invalid one raises :class:`ContextsConfigError`, which the boot sequence turns
    into a fatal, single-message exit.
    """
    p = Path(path) if path else None
    if p is None or not p.exists():
        raise ContextsConfigError.missing(p or Path("config.yaml"))

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    try:
        return ContextsConfig.model_validate(raw)
    except ValidationError as exc:
        raise ContextsConfigError.invalid(p, exc) from exc
