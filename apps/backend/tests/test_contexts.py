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


def _named(name: str) -> str:
    return f"default_context: {name}\ncontexts:\n  {name}:\n    directory: /a\n    description: A\n"


@pytest.mark.parametrize(
    "name",
    [
        "my.proj",  # '.' survives URL-quoting but Telegram drops the start_param
        "has space",
        "a__b",  # the start_param view/context separator
        "c_decade",  # 'decade' is all hex → parsed as a content id by the frontend
        "x" * 55,  # blows the 64-char start_param budget behind "markdown__"
    ],
)
def test_context_name_breaking_start_param_rejected(tmp_path, name) -> None:
    with pytest.raises(ContextsConfigError):
        load_contexts(_write(tmp_path, _named(name)))


def test_context_name_within_start_param_contract_accepted(tmp_path) -> None:
    # 'c_compose' is fine: 'ompose' is not hex, so it can't be a content id.
    cfg = load_contexts(_write(tmp_path, _named("c_compose")))
    assert "c_compose" in cfg.contexts


MCP_CONFIG = """
default_context: a
contexts:
  a:
    directory: /a
    description: A
    mcp:
      db:
        type: local
        command: ["postgres-mcp", "--restricted"]
        environment:
          DATABASE_URI: ${TEST_DB_URI}
      api:
        type: remote
        url: https://x/mcp
        headers:
          Authorization: Bearer ${TEST_API_TOKEN}
"""


def test_mcp_parsed_and_env_expanded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_DB_URI", "postgres://secret")
    monkeypatch.setenv("TEST_API_TOKEN", "tok123")
    cfg = load_contexts(_write(tmp_path, MCP_CONFIG))
    mcp = cfg.get("a").mcp
    assert mcp["db"]["environment"]["DATABASE_URI"] == "postgres://secret"
    assert mcp["api"]["headers"]["Authorization"] == "Bearer tok123"


def test_mcp_missing_env_var_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TEST_DB_URI", raising=False)
    monkeypatch.delenv("TEST_API_TOKEN", raising=False)
    with pytest.raises(ContextsConfigError):
        load_contexts(_write(tmp_path, MCP_CONFIG))


def test_mcp_bad_shape_rejected(tmp_path) -> None:
    bad = (
        "default_context: a\ncontexts:\n  a:\n    directory: /a\n"
        "    description: A\n    mcp:\n      broken:\n        type: remote\n"
    )  # remote with no url
    with pytest.raises(ContextsConfigError):
        load_contexts(_write(tmp_path, bad))


def test_mcp_defaults_to_empty(tmp_path) -> None:
    cfg = load_contexts(_write(tmp_path, CONFIG))
    assert cfg.get("balam").mcp == {}
