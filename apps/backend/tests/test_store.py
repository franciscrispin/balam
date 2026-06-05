from balam.store import GENERAL_THREAD_ID, SessionStore


def fresh_store() -> SessionStore:
    # Each test gets its own in-memory DB — no file, no cross-test bleed.
    return SessionStore(":memory:")


def test_returns_none_for_unmapped_topic() -> None:
    store = fresh_store()
    assert store.get_row(100, 5) is None


def test_round_trips_a_mapping() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1_700_000_000_000)
    assert store.get_row(100, 5) == ("ses_abc", None)


def test_keys_are_scoped_per_chat_and_thread() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_a", 1)
    store.set(100, 6, "ses_b", 2)
    store.set(200, 5, "ses_c", 3)
    assert store.get_row(100, 5)[0] == "ses_a"
    assert store.get_row(100, 6)[0] == "ses_b"
    assert store.get_row(200, 5)[0] == "ses_c"


def test_set_overwrites_existing_mapping() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_old", 1)
    store.set(100, 5, "ses_new", 2)
    assert store.get_row(100, 5)[0] == "ses_new"


def test_delete_removes_mapping() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1)
    store.delete(100, 5)
    assert store.get_row(100, 5) is None


def test_general_topic_maps_to_catch_all_key() -> None:
    store = fresh_store()
    store.set(100, None, "ses_general", 1)
    assert store.get_row(100, None) == ("ses_general", None)
    assert store.get_row(100, GENERAL_THREAD_ID) == ("ses_general", None)


def test_thread_key_normalizes_none() -> None:
    assert SessionStore.thread_key(None) == GENERAL_THREAD_ID
    assert SessionStore.thread_key(7) == 7


def test_get_row_returns_session_and_context() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1, context="balam")
    assert store.get_row(100, 5) == ("ses_abc", "balam")


def test_get_row_is_none_for_unmapped() -> None:
    assert fresh_store().get_row(100, 5) is None


def test_context_defaults_to_none_when_omitted() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1)  # legacy 4-arg call
    assert store.get_row(100, 5) == ("ses_abc", None)


def test_set_overwrites_context() -> None:
    store = fresh_store()
    store.set(100, 5, "ses_abc", 1, context="balam")
    store.set(100, 5, "ses_def", 2, context="scratch")
    assert store.get_row(100, 5) == ("ses_def", "scratch")
