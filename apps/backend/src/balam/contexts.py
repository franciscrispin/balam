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
import string
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from balam.mcp_config import parse_mcp_config

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

#: How an auto-named topic is titled: the context name, then the first message
#: (:func:`balam.topics.topic_name` renders it). Overridable per context, so a
#: workspace whose topics all live in one place can drop the prefix with
#: ``topic_title: "{summary}"``.
DEFAULT_TOPIC_TITLE = "{context}: {summary}"

#: The only placeholders a ``topic_title`` may use.
_TOPIC_TITLE_FIELDS = ("context", "summary")


def _validated_topic_title(value: str) -> str:
    """Check a ``topic_title`` template at load time, so a typo is a boot error
    rather than a literal ``{summry}`` in every topic name from then on."""
    try:
        parsed = list(string.Formatter().parse(value))
    except ValueError as exc:
        raise ValueError(f"topic_title {value!r} is not a valid format string ({exc})") from None
    for _literal, field, _spec, _conv in parsed:
        if field is None:
            continue
        name = field.split(".")[0].split("[")[0]
        if name not in _TOPIC_TITLE_FIELDS:
            allowed = ", ".join(f"{{{f}}}" for f in _TOPIC_TITLE_FIELDS)
            raise ValueError(
                f"topic_title {value!r} uses unknown placeholder {{{field}}} "
                f"(only {allowed} are available)"
            )
    if not value.strip():
        raise ValueError("topic_title must not be blank (Telegram rejects an empty topic name)")
    return value


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
    #: When the agent answers in this context's topics. ``all`` (the default):
    #: every message is a turn. ``mentions``: only a message aimed at the bot — an
    #: ``@mention``, a reply to one of its messages, or a slash command — becomes a
    #: turn, and everything else is dropped before it reaches the agent
    #: (:func:`balam.message_text.addresses_bot`). That turns a topic into a place
    #: where people talk among themselves and call the agent in when they want it.
    #: Only allowlisted users are ever heard either way (ADR-0008).
    respond_to: Literal["all", "mentions"] = "all"
    #: How this context's topics are auto-named, overriding the file-level
    #: ``topic_title``. ``{context}`` and ``{summary}`` (the first message) are
    #: filled in; ``"{summary}"`` drops the context prefix entirely. ``None``
    #: means "use the file-level default".
    topic_title: str | None = None

    @field_validator("topic_title")
    @classmethod
    def _topic_title_is_renderable(cls, value: str | None) -> str | None:
        return None if value is None else _validated_topic_title(value)

    @field_validator("mcp", mode="after")
    @classmethod
    def _mcp_resolved_and_valid(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Expand ${VAR} secrets first, then validate the resolved shape (the
        # parser both backends serialize from) so a malformed server fails fast
        # at boot.
        expanded = _expand_env(value, where="mcp")
        for name, server in expanded.items():
            parse_mcp_config(name, server)  # raises ValueError on a bad entry
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


#: Integration names appear in a URL path (``/integrations/<name>/messages``) and
#: in log lines, so they keep to a plain charset.
_INTEGRATION_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")

#: Integrations are local services only for now (ADR-0018): a tap is delivered
#: over plain HTTP, which is only acceptable on the loopback interface.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

#: A shared secret short enough to guess defeats the point of having one.
_INTEGRATION_SECRET_MIN = 16

#: Telegram caps a forum topic name at 128 characters.
_TOPIC_NAME_MAX = 128


class IntegrationConfig(BaseModel):
    """A local service that posts messages with buttons through Balam and is told
    when an allowed person taps one (ADR-0018). Nothing about the service is
    loaded into Balam; this block is all Balam knows of it."""

    model_config = ConfigDict(extra="forbid")

    #: Where taps are delivered, e.g. ``http://127.0.0.1:8770/balam``. Loopback only.
    url: str
    #: Shared with the service: it presents this as a bearer token when posting,
    #: and Balam signs every tap delivery with it. Must be a ``${VAR}`` from .env.
    secret: SecretStr
    #: Optional: the Telegram user ids who may tap this integration's buttons. A
    #: subset of the allowlist (checked at boot); empty means everyone on it.
    tappers: list[int] = Field(default_factory=list)
    #: Optional: the forum topic messages go to when a post names none. Balam
    #: makes it the first time it is needed. Unset means General.
    topic: str | None = None

    @field_validator("url")
    @classmethod
    def _url_is_local(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or parts.hostname not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"url {value!r} must be an http(s) address on this machine "
                "(127.0.0.1, localhost or ::1); remote integrations are not supported yet"
            )
        return value

    @field_validator("secret", mode="before")
    @classmethod
    def _secret_from_env(cls, value: object) -> object:
        # The secret is the whole trust between Balam and the service, so it
        # lives in .env like every other secret, never in this file.
        if not isinstance(value, str) or not _ENV_REF.fullmatch(value.strip()):
            raise ValueError("secret must come from .env as a single ${VAR} reference")
        expanded = _expand_env(value.strip(), where="secret")
        if len(expanded) < _INTEGRATION_SECRET_MIN:
            raise ValueError(
                f"secret must be at least {_INTEGRATION_SECRET_MIN} characters "
                "(e.g. `openssl rand -hex 32`)"
            )
        return expanded

    @field_validator("tappers")
    @classmethod
    def _tappers_are_user_ids(cls, value: list[int]) -> list[int]:
        for uid in value:
            if uid <= 0:
                raise ValueError(f"tapper {uid} must be a positive Telegram user id")
        return value

    @field_validator("topic")
    @classmethod
    def _topic_is_a_name(cls, value: str | None) -> str | None:
        if value is not None and not (0 < len(value.strip()) <= _TOPIC_NAME_MAX):
            raise ValueError(f"topic must be 1 to {_TOPIC_NAME_MAX} characters")
        return value


class ContextsConfig(BaseModel):
    """The whole ``config.yaml``: a set of contexts plus the default."""

    model_config = ConfigDict(extra="forbid")

    default_context: str
    contexts: dict[str, ContextConfig]
    #: Default topic-name template for every context that doesn't set its own.
    topic_title: str = DEFAULT_TOPIC_TITLE
    #: Local services that post buttons through Balam (ADR-0018), keyed by name.
    integrations: dict[str, IntegrationConfig] = Field(default_factory=dict)

    @field_validator("integrations")
    @classmethod
    def _integration_names(
        cls, value: dict[str, IntegrationConfig]
    ) -> dict[str, IntegrationConfig]:
        for name in value:
            if not _INTEGRATION_NAME.fullmatch(name):
                raise ValueError(
                    f"integration name {name!r} must be 1 to 32 letters, digits, '-' or '_' "
                    "(it is part of the URL the service posts to)"
                )
        return value

    @field_validator("topic_title")
    @classmethod
    def _topic_title_is_renderable(cls, value: str) -> str:
        return _validated_topic_title(value)

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

    def topic_title_template(self, name: str | None) -> str:
        """The topic-name template for context ``name``: its own ``topic_title``
        if it sets one, else the file-level default. Unknown names resolve the
        way :meth:`get` does — through ``default_context``."""
        return self.get(name).topic_title or self.topic_title

    def resolve_name(self, name: str | None) -> str:
        """The context name :meth:`get` would use — for persisting the binding."""
        return name if (name and name in self.contexts) else self.default_context

    def match_name(self, name: str | None) -> str | None:
        """Canonical context key matching ``name`` case-insensitively, or ``None``.

        An exact match wins first, so case-colliding keys (``Balam`` and ``balam``
        both defined) stay individually addressable; otherwise the first key whose
        lowercase form matches is returned (``/new Balam`` → ``balam``)."""
        if not name:
            return None
        if name in self.contexts:
            return name
        lowered = name.lower()
        return next((key for key in self.contexts if key.lower() == lowered), None)


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
