from balam.contexts import ContextConfig
from balam.permissions import build_ruleset, parse_allowed_tool, send_file_rules


def _ctx(**kwargs) -> ContextConfig:
    base = {"directory": "/work/balam", "description": "Balam"}
    base.update(kwargs)
    return ContextConfig(**base)


# ---- parse_allowed_tool -------------------------------------------------------


def test_parse_plain_lowercases_hooks_form() -> None:
    assert parse_allowed_tool("LSP") == ("lsp", None)
    assert parse_allowed_tool("Read") == ("read", None)


def test_parse_extracts_pattern() -> None:
    assert parse_allowed_tool("Bash(git *)") == ("bash", "git *")
    assert parse_allowed_tool("bash()") == ("bash", None)


def test_parse_mcp_qualified_name() -> None:
    assert parse_allowed_tool("mcp__github__create_issue") == ("github_create_issue", None)


def test_parse_blank() -> None:
    assert parse_allowed_tool("   ") == (None, None)


# ---- build_ruleset ------------------------------------------------------------


def test_baseline_when_no_opt_ins() -> None:
    # A context with no opt-ins reduces to the ask-everything baseline + internals.
    rules = build_ruleset(_ctx())
    assert rules == [
        {"permission": "*", "pattern": "*", "action": "ask"},
        {"permission": "todowrite", "pattern": "*", "action": "allow"},
        {"permission": "question", "pattern": "*", "action": "allow"},
        {"permission": "plan_enter", "pattern": "*", "action": "allow"},
        {"permission": "plan_exit", "pattern": "*", "action": "allow"},
        {"permission": "task", "pattern": "*", "action": "allow"},
        {"permission": "edit", "pattern": "*opencode/plans/*.md", "action": "allow"},
    ]


def test_ask_baseline_is_first() -> None:
    rules = build_ruleset(_ctx(allowed_tools=["LSP"]))
    assert rules[0] == {"permission": "*", "pattern": "*", "action": "ask"}


def test_send_file_rules_order_is_deny_then_allow() -> None:
    # The glob-deny hides every topic's send_file from the model's tool list;
    # the topic's own allow must come after it (OpenCode uses the last match).
    rules = send_file_rules("balam_t42")
    assert rules == [
        {"permission": "balam_*_send_file", "pattern": "*", "action": "deny"},
        {"permission": "balam_t42_send_file", "pattern": "*", "action": "allow"},
    ]


def test_bash_pattern_is_verbatim() -> None:
    rules = build_ruleset(_ctx(allowed_tools=["Bash(git *)"]))
    assert {"permission": "bash", "pattern": "git *", "action": "allow"} in rules


def test_flag_tool_defaults_to_star_pattern() -> None:
    rules = build_ruleset(_ctx(allowed_tools=["webfetch"]))
    assert {"permission": "webfetch", "pattern": "*", "action": "allow"} in rules


def test_bare_skill_allows_every_skill() -> None:
    # "Skill" (no pattern) pre-approves all skills, so OpenCode never raises a
    # permission.asked for a skill invocation. The allow must follow the ask-all
    # baseline (OpenCode uses the last matching rule).
    rules = build_ruleset(_ctx(allowed_tools=["Skill"]))
    allow = {"permission": "skill", "pattern": "*", "action": "allow"}
    assert allow in rules
    assert rules.index(allow) > rules.index({"permission": "*", "pattern": "*", "action": "ask"})


def test_bare_edit_is_scoped_to_workspace_not_global() -> None:
    rules = build_ruleset(_ctx(allowed_tools=["Edit"]))
    # File-path category: no leading slash, ** glob, scoped to the directory.
    assert {"permission": "edit", "pattern": "work/balam/**", "action": "allow"} in rules
    # Never a global allow.
    assert {"permission": "edit", "pattern": "*", "action": "allow"} not in rules


def test_write_and_apply_patch_normalize_to_edit() -> None:
    for name in ("Write", "apply_patch"):
        rules = build_ruleset(_ctx(allowed_tools=[name]))
        assert {"permission": "edit", "pattern": "work/balam/**", "action": "allow"} in rules


def test_bare_edit_covers_additional_directories() -> None:
    rules = build_ruleset(_ctx(allowed_tools=["Edit"], additional_directories=["/work/lib"]))
    assert {"permission": "edit", "pattern": "work/balam/**", "action": "allow"} in rules
    assert {"permission": "edit", "pattern": "work/lib/**", "action": "allow"} in rules


def test_explicit_file_path_pattern_strips_leading_slash() -> None:
    rules = build_ruleset(_ctx(allowed_tools=["Read(/etc/hosts)"]))
    assert {"permission": "read", "pattern": "etc/hosts", "action": "allow"} in rules


def test_additional_directories_get_external_directory_grant() -> None:
    rules = build_ruleset(_ctx(additional_directories=["/work/lib", "/work/docs/"]))
    # external_directory keeps the leading slash and uses a single-star dir glob.
    patterns = [r["pattern"] for r in rules if r["permission"] == "external_directory"]
    assert patterns == ["/work/lib/*", "/work/docs/*"]
    assert all(r["action"] == "allow" for r in rules if r["permission"] == "external_directory")
