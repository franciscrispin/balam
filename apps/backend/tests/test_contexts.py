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


def test_split_provider_model_allows_bare_id_for_the_sdk() -> None:
    # The Claude Agent SDK takes a bare id; OpenCode falls back to its default.
    assert split_provider_model("claude-opus-4-8") == (None, "claude-opus-4-8")
    assert split_provider_model("opus") == (None, "opus")


def test_match_name_is_case_insensitive(tmp_path) -> None:
    cfg = load_contexts(_write(tmp_path, CONFIG))
    # Typed casing is normalized to the canonical config key (/new Balam → balam).
    assert cfg.match_name("Balam") == "balam"
    assert cfg.match_name("BALAM") == "balam"
    assert cfg.match_name("balam") == "balam"
    assert cfg.match_name("Scratch") == "scratch"


def test_match_name_returns_none_for_unknown_or_empty(tmp_path) -> None:
    cfg = load_contexts(_write(tmp_path, CONFIG))
    assert cfg.match_name("nope") is None
    assert cfg.match_name("") is None
    assert cfg.match_name(None) is None


def test_match_name_prefers_exact_when_keys_collide_by_case() -> None:
    # Both casings defined: an exact match must win so each stays addressable.
    cfg = ContextsConfig(
        default_context="balam",
        contexts={
            "balam": {"directory": "/a", "description": "lower"},
            "Balam": {"directory": "/b", "description": "upper"},
        },
    )
    assert cfg.match_name("Balam") == "Balam"
    assert cfg.match_name("balam") == "balam"


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


def test_bare_model_loads_for_the_sdk(tmp_path) -> None:
    # A bare model id (no provider) is valid — the Claude Agent SDK takes it
    # directly; OpenCode would fall back to its default.
    cfg = (
        "default_context: a\ncontexts:\n  a:\n    directory: /a\n"
        "    description: A\n    model: claude-opus-4-8\n"
    )
    loaded = load_contexts(_write(tmp_path, cfg))
    assert loaded.get("a").provider_model == (None, "claude-opus-4-8")


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
