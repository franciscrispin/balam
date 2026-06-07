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

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

#: Thinking-effort levels OpenCode accepts (sent as the ``variant`` on a prompt).
EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def split_provider_model(model: str | None) -> tuple[str | None, str | None]:
    """Split a context's ``model`` (``provider/model``) into its parts.

    Returns ``(None, None)`` when no model is set (OpenCode then uses its own
    default). A non-empty value must be provider-qualified.
    """
    if not model:
        return None, None
    if "/" not in model:
        raise ValueError(
            f"model {model!r} must be 'provider/model' (e.g. 'anthropic/claude-opus-4-8')"
        )
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
