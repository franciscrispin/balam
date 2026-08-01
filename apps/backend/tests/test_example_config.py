"""The shipped example files must document every setting that exists.

`.env.example` and `config.example.yaml` are the only instructions for standing
Balam up, and both drift silently: adding a field to `Config` or `ContextConfig`
does not fail anything, it just leaves a setting nobody can discover. The tech
debt inventory listed this as an unchecked manual chore; this is that check,
automated, in the same spirit as the generated-API-types drift job in CI.

An optional setting may be documented as a *commented* example rather than an
active key (that is how `model`, `effort`, `mcp` and `additional_directories`
appear), so both files are scanned as text rather than parsed.
"""

from __future__ import annotations

import re
from pathlib import Path

from balam.config import Config
from balam.contexts import ContextConfig, ContextsConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CONFIG_EXAMPLE = REPO_ROOT / "config.example.yaml"


def _documented_env_keys(text: str) -> set[str]:
    """Env var names in `.env.example`, whether active or commented out."""
    return {m.group(1).lower() for m in re.finditer(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M)}


def _documented_yaml_keys(text: str) -> set[str]:
    """Mapping keys in the YAML example, whether active or commented out."""
    return {m.group(1) for m in re.finditer(r"^\s*#?\s*([a-z_][a-z0-9_]*):", text, re.M)}


def test_env_example_documents_every_config_field() -> None:
    documented = _documented_env_keys(ENV_EXAMPLE.read_text())
    missing = sorted(f for f in Config.model_fields if f not in documented)
    assert not missing, f"add these to .env.example: {missing}"


def test_env_example_has_no_keys_that_are_not_settings() -> None:
    """A stale key is worse than a missing one — it looks like it does something."""
    documented = _documented_env_keys(ENV_EXAMPLE.read_text())
    unknown = sorted(k for k in documented if k not in Config.model_fields)
    assert not unknown, f".env.example documents settings that no longer exist: {unknown}"


def test_config_example_documents_every_context_field() -> None:
    documented = _documented_yaml_keys(CONFIG_EXAMPLE.read_text())
    missing = sorted(f for f in ContextConfig.model_fields if f not in documented)
    assert not missing, f"add these to config.example.yaml: {missing}"


def test_config_example_documents_every_top_level_field() -> None:
    documented = _documented_yaml_keys(CONFIG_EXAMPLE.read_text())
    missing = sorted(f for f in ContextsConfig.model_fields if f not in documented)
    assert not missing, f"add these to config.example.yaml: {missing}"


def test_the_shipped_example_actually_loads() -> None:
    """Beyond field coverage: the example must be valid, or copying it fails."""
    import yaml

    raw = yaml.safe_load(CONFIG_EXAMPLE.read_text())
    parsed = ContextsConfig.model_validate(raw)
    assert parsed.default_context in parsed.contexts
