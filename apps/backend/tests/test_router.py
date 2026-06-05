from balam.contexts import ContextConfig, ContextsConfig
from balam.router import Router, TopicRef
from balam.store import SessionStore


class FakeOpenCode:
    """Records create/exists calls; lets a test control session existence."""

    def __init__(self, *, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.created: list[tuple[str, str | None]] = []
        self.exists_calls: list[tuple[str, str | None]] = []
        self._counter = 0

    async def session_exists(self, session_id: str, *, directory: str | None = None) -> bool:
        self.exists_calls.append((session_id, directory))
        return session_id in self.existing

    async def create_session(self, title: str, *, directory: str | None = None) -> str:
        self._counter += 1
        sid = f"ses_{self._counter}"
        self.existing.add(sid)
        self.created.append((sid, directory))
        return sid


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


async def test_unknown_bound_context_falls_back_to_default() -> None:
    store, oc = _store(), FakeOpenCode()
    router = Router(store, oc, _contexts())
    # A binding to a context that has since been removed from config.yaml.
    store.set(1, 5, "ses_old", 1, context="deleted-ctx")

    resolved = await router.resolve(TopicRef(chat_id=1, thread_id=5, title="t"))

    assert resolved.directory == "/work/balam"
    assert store.get_row(1, 5) == (resolved.session_id, "balam")
