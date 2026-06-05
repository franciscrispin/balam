import pytest

from balam.contexts import (
    ContextsConfig,
    ContextsConfigError,
    load_contexts,
    split_provider_model,
)

CONFIG = """
default_context: balam
contexts:
  balam:
    directory: /home/me/balam
    description: "AI assistant"
    model: anthropic/claude-opus-4-8
    effort: high
    allowed_tools:
      - LSP
  scratch:
    directory: /home/me/scratch
    description: "Scratch"
"""


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_split_provider_model() -> None:
    assert split_provider_model("anthropic/claude-opus-4-8") == (
        "anthropic",
        "claude-opus-4-8",
    )
    assert split_provider_model(None) == (None, None)
    assert split_provider_model("") == (None, None)


def test_split_provider_model_requires_provider() -> None:
    with pytest.raises(ValueError):
        split_provider_model("claude-opus-4-8")


def test_loads_and_validates(tmp_path) -> None:
    cfg = load_contexts(_write(tmp_path, CONFIG))
    assert sorted(cfg.contexts) == ["balam", "scratch"]
    assert cfg.default_context == "balam"
    balam = cfg.get("balam")
    assert balam.directory == "/home/me/balam"
    assert balam.provider_model == ("anthropic", "claude-opus-4-8")
    assert balam.effort == "high"
    assert balam.allowed_tools == ["LSP"]
    # Optional fields default cleanly.
    assert cfg.get("scratch").provider_model == (None, None)
    assert cfg.get("scratch").effort is None


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(ContextsConfigError):
        load_contexts(tmp_path / "absent.yaml")


def test_none_path_raises() -> None:
    with pytest.raises(ContextsConfigError):
        load_contexts(None)


def test_get_falls_back_to_default_for_unknown(tmp_path) -> None:
    cfg = load_contexts(_write(tmp_path, CONFIG))
    assert cfg.get("does-not-exist").directory == cfg.get("balam").directory
    assert cfg.resolve_name("does-not-exist") == "balam"
    assert cfg.resolve_name("scratch") == "scratch"
    assert cfg.resolve_name(None) == "balam"


def test_default_context_must_exist(tmp_path) -> None:
    bad = "default_context: nope\ncontexts:\n  a:\n    directory: /a\n    description: A\n"
    with pytest.raises(ContextsConfigError):
        load_contexts(_write(tmp_path, bad))


def test_unknown_effort_rejected(tmp_path) -> None:
    bad = (
        "default_context: a\ncontexts:\n  a:\n    directory: /a\n"
        "    description: A\n    effort: turbo\n"
    )
    with pytest.raises(ContextsConfigError):
        load_contexts(_write(tmp_path, bad))


def test_unqualified_model_rejected(tmp_path) -> None:
    bad = (
        "default_context: a\ncontexts:\n  a:\n    directory: /a\n"
        "    description: A\n    model: gpt-5\n"
    )
    with pytest.raises(ContextsConfigError):
        load_contexts(_write(tmp_path, bad))


def test_unknown_field_rejected(tmp_path) -> None:
    bad = (
        "default_context: a\ncontexts:\n  a:\n    directory: /a\n"
        "    description: A\n    sandbox: docker\n"
    )
    with pytest.raises(ContextsConfigError):
        load_contexts(_write(tmp_path, bad))


def test_construct_requires_nonempty_contexts() -> None:
    with pytest.raises(ValueError):
        ContextsConfig(default_context="x", contexts={})
