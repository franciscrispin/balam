from balam.agent_tools import ToolScopes
from balam.contexts import ContextConfig, ContextsConfig
from balam.router import Router, TopicRef
from balam.store import SessionStore


class FakeOpenCode:
    """Records create/exists calls; lets a test control session existence."""

    def __init__(self, *, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.created: list[tuple[str, str | None]] = []
        self.permissions: list[list[dict[str, str]] | None] = []
        self.updated_permissions: list[tuple[str, str | None, list[dict[str, str]]]] = []
        self.mcps: list[dict | None] = []
        self.registered_mcps: list[tuple[str, dict, str | None]] = []
        self.exists_calls: list[tuple[str, str | None]] = []
        self._counter = 0

    async def register_mcp(self, name: str, config: dict, *, directory: str | None = None) -> None:
        self.registered_mcps.append((name, config, directory))

    async def session_exists(self, session_id: str, *, directory: str | None = None) -> bool:
        self.exists_calls.append((session_id, directory))
        return session_id in self.existing

    async def create_session(
        self,
        title: str,
        *,
        directory: str | None = None,
        permission: list[dict[str, str]] | None = None,
        mcp: dict | None = None,
    ) -> str:
        self._counter += 1
        sid = f"ses_{self._counter}"
        self.existing.add(sid)
        self.created.append((sid, directory))
        self.permissions.append(permission)
        self.mcps.append(mcp)
        return sid

    async def update_session_permission(
        self,
        session_id: str,
        *,
        directory: str | None = None,
        permission: list[dict[str, str]],
    ) -> None:
        self.updated_permissions.append((session_id, directory, permission))


def _contexts() -> ContextsConfig:
    return ContextsConfig(
        default_context="balam",
        contexts={
            "balam": ContextConfig(
                directory="/work/balam",
                description="Balam",
                model="anthropic/claude-opus-4-8",
                effort="high",
            ),
            "scratch": ContextConfig(directory="/work/scratch", description="Scratch"),
        },
    )


def _store() -> SessionStore:
    return SessionStore(":memory:")


async def test_creates_session_in_default_context() -> None:
    store, oc = _store(), FakeOpenCode()
    router = Router(store, oc, _contexts())

    resolved = await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    assert resolved.directory == "/work/balam"
    assert resolved.provider == "anthropic"
    assert resolved.model == "claude-opus-4-8"
    assert resolved.effort == "high"
    assert oc.created == [(resolved.session_id, "/work/balam")]
    # Binding persisted with the default context name.
    assert store.get_row(1, 5) == (resolved.session_id, "balam")


async def test_reuses_live_session_and_its_bound_context() -> None:
    store, oc = _store(), FakeOpenCode()
    router = Router(store, oc, _contexts())
    # Pre-bind the topic to the "scratch" context.
    store.set(1, 5, "ses_live", 1, context="scratch")
    oc.existing.add("ses_live")

    resolved = await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    assert resolved.session_id == "ses_live"
    assert resolved.directory == "/work/scratch"
    assert resolved.provider is None and resolved.model is None and resolved.effort is None
    assert oc.created == []  # reused, not recreated
    assert oc.exists_calls == [("ses_live", "/work/scratch")]
    assert oc.updated_permissions == [
        (
            "ses_live",
            "/work/scratch",
            [
                {"permission": "*", "pattern": "*", "action": "ask"},
                {"permission": "todowrite", "pattern": "*", "action": "allow"},
                {"permission": "question", "pattern": "*", "action": "allow"},
            ],
        )
    ]


async def test_recreates_vanished_session_preserving_context() -> None:
    store, oc = _store(), FakeOpenCode()
    router = Router(store, oc, _contexts())
    # Bound to scratch, but the session no longer exists server-side.
    store.set(1, 5, "ses_gone", 1, context="scratch")

    resolved = await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    assert resolved.session_id != "ses_gone"
    assert resolved.directory == "/work/scratch"
    assert oc.created == [(resolved.session_id, "/work/scratch")]
    assert store.get_row(1, 5) == (resolved.session_id, "scratch")


async def test_create_topic_session_binds_new_topic_without_touching_others() -> None:
    store, oc = _store(), FakeOpenCode()
    router = Router(store, oc, _contexts())
    # An existing topic in the default context (thread 5).
    existing = await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    # /context scratch creates a *new* topic (thread 9) bound to scratch.
    new_session = await router.create_topic_session(1, 9, "scratch", "scratch")
    assert (new_session, "/work/scratch") in oc.created
    assert store.get_row(1, 9) == (new_session, "scratch")
    assert router.current_context_name(TopicRef(1, 9, "t")) == "scratch"

    # The original topic is untouched — its session and context survive.
    assert store.get_row(1, 5) == (existing.session_id, "balam")
    resolved = await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))
    assert resolved.session_id == existing.session_id
    assert resolved.directory == "/work/balam"


def test_current_context_name_defaults_when_unbound() -> None:
    store, oc = _store(), FakeOpenCode()
    router = Router(store, oc, _contexts())
    assert router.current_context_name(TopicRef(1, 5, "t")) == "balam"


async def test_create_session_passes_context_ruleset() -> None:
    store, oc = _store(), FakeOpenCode()
    contexts = ContextsConfig(
        default_context="balam",
        contexts={
            "balam": ContextConfig(
                directory="/work/balam",
                description="Balam",
                allowed_tools=["LSP", "Bash(git *)"],
            )
        },
    )
    router = Router(store, oc, contexts)

    await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    ruleset = oc.permissions[0]
    assert ruleset is not None
    assert ruleset[0] == {"permission": "*", "pattern": "*", "action": "ask"}
    assert {"permission": "lsp", "pattern": "*", "action": "allow"} in ruleset
    assert {"permission": "bash", "pattern": "git *", "action": "allow"} in ruleset


async def test_reused_session_syncs_context_ruleset() -> None:
    store, oc = _store(), FakeOpenCode(existing={"ses_live"})
    contexts = ContextsConfig(
        default_context="balam",
        contexts={
            "balam": ContextConfig(
                directory="/work/balam",
                description="Balam",
                allowed_tools=["Bash(git *)"],
            )
        },
    )
    router = Router(store, oc, contexts)
    store.set(1, 5, "ses_live", 1, context="balam")

    await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    assert oc.updated_permissions
    _, _, ruleset = oc.updated_permissions[0]
    assert {"permission": "bash", "pattern": "git *", "action": "allow"} in ruleset


def _wired_router(store: SessionStore, oc: FakeOpenCode, *, qualify_chat: bool = False) -> Router:
    return Router(
        store,
        oc,
        _contexts(),
        tool_scopes=ToolScopes(),
        mcp_base_url="http://127.0.0.1:3000",
        qualify_chat=qualify_chat,
    )


async def test_create_includes_balam_tool_server_and_rules() -> None:
    store, oc = _store(), FakeOpenCode()
    router = _wired_router(store, oc)

    await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    mcp = oc.mcps[0]
    assert mcp is not None and "balam_t5" in mcp
    assert mcp["balam_t5"]["type"] == "remote"
    assert mcp["balam_t5"]["url"].startswith("http://127.0.0.1:3000/mcp/")
    ruleset = oc.permissions[0]
    assert ruleset is not None
    # Order is load-bearing: glob-deny hides other topics' tools, own allow last.
    deny = {"permission": "balam_*_send_file", "pattern": "*", "action": "deny"}
    allow = {"permission": "balam_t5_send_file", "pattern": "*", "action": "allow"}
    assert deny in ruleset and allow in ruleset
    assert ruleset.index(allow) > ruleset.index(deny)


async def test_reuse_reregisters_balam_tool_server() -> None:
    store, oc = _store(), FakeOpenCode(existing={"ses_live"})
    router = _wired_router(store, oc)
    store.set(1, 5, "ses_live", 1, context="balam")

    await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    # OpenCode restarts lose in-memory MCP registrations while sessions persist;
    # the reuse path heals by re-registering (deterministic name → overwrite).
    assert [(n, d) for n, _, d in oc.registered_mcps] == [("balam_t5", "/work/balam")]
    _, _, ruleset = oc.updated_permissions[0]
    assert {"permission": "balam_t5_send_file", "pattern": "*", "action": "allow"} in ruleset


async def test_topics_get_distinct_tool_servers_and_stable_tokens() -> None:
    store, oc = _store(), FakeOpenCode()
    router = _wired_router(store, oc)

    await router.resolve(TopicRef(chat_id=1, thread_id=5, title="a"))
    await router.resolve(TopicRef(chat_id=1, thread_id=6, title="b"))

    url_t5 = oc.mcps[0]["balam_t5"]["url"]
    url_t6 = oc.mcps[1]["balam_t6"]["url"]
    assert url_t5 != url_t6

    # Re-resolving the same topic reuses the token (idempotent registration).
    store2, oc2 = _store(), FakeOpenCode()
    router2 = Router(
        store2, oc2, _contexts(), tool_scopes=ToolScopes(), mcp_base_url="http://127.0.0.1:3000"
    )
    first = await router2.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))
    oc2.existing.add(first.session_id)
    await router2.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))
    assert oc2.registered_mcps[0][1]["url"] == oc2.mcps[0]["balam_t5"]["url"]


async def test_create_topic_session_includes_tool_server() -> None:
    store, oc = _store(), FakeOpenCode()
    router = _wired_router(store, oc)

    await router.create_topic_session(1, 9, "scratch", "scratch")

    assert oc.mcps[0] is not None and "balam_t9" in oc.mcps[0]


async def test_unwired_router_keeps_legacy_behavior() -> None:
    store, oc = _store(), FakeOpenCode()
    router = Router(store, oc, _contexts())  # no tool_scopes / mcp_base_url

    await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    assert oc.mcps[0] == {}
    assert all("send_file" not in r["permission"] for r in oc.permissions[0])


async def test_qualified_server_name_for_multi_chat() -> None:
    store, oc = _store(), FakeOpenCode()
    router = _wired_router(store, oc, qualify_chat=True)

    await router.resolve(TopicRef(chat_id=-100123, thread_id=5, title="t"))

    assert "balam_cn100123_t5" in oc.mcps[0]


async def test_unknown_bound_context_falls_back_to_default() -> None:
    store, oc = _store(), FakeOpenCode()
    router = Router(store, oc, _contexts())
    # A binding to a context that has since been removed from config.yaml.
    store.set(1, 5, "ses_old", 1, context="deleted-ctx")

    resolved = await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    assert resolved.directory == "/work/balam"
    assert store.get_row(1, 5) == (resolved.session_id, "balam")
