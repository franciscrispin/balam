"""Tests for the ephemeral markdown content store."""

from __future__ import annotations

import re

from balam.content_store import CONTENT_TTL_S, MAX_CONTENT_BYTES, ContentStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_put_returns_short_hex_id() -> None:
    store = ContentStore()
    content_id = store.put("Plan", "# A plan")
    assert re.fullmatch(r"[0-9a-f]{12}", content_id)


def test_round_trip() -> None:
    store = ContentStore()
    content_id = store.put("report.md", "# Report\n\nBody.")
    entry = store.get(content_id)
    assert entry is not None
    assert entry.title == "report.md"
    assert entry.content == "# Report\n\nBody."


def test_get_unknown_id_returns_none() -> None:
    assert ContentStore().get("deadbeef0000") is None


def test_entry_expires_after_ttl() -> None:
    clock = FakeClock()
    store = ContentStore(clock=clock)
    content_id = store.put("Plan", "stale")
    clock.advance(CONTENT_TTL_S + 1)
    assert store.get(content_id) is None
    # And it was evicted, not just hidden.
    clock.now = 1000.0
    assert store.get(content_id) is None


def test_entry_survives_within_ttl() -> None:
    clock = FakeClock()
    store = ContentStore(clock=clock)
    content_id = store.put("Plan", "fresh")
    clock.advance(CONTENT_TTL_S - 1)
    assert store.get(content_id) is not None


def test_put_purges_expired_siblings() -> None:
    clock = FakeClock()
    store = ContentStore(clock=clock)
    old_id = store.put("old", "old")
    clock.advance(CONTENT_TTL_S + 1)
    store.put("new", "new")
    assert old_id not in store._entries


def test_oversized_content_is_truncated_with_notice() -> None:
    store = ContentStore()
    content_id = store.put("big.md", "x" * (MAX_CONTENT_BYTES + 100))
    entry = store.get(content_id)
    assert entry is not None
    assert entry.content.endswith("…(truncated)")
    assert len(entry.content.encode("utf-8")) <= MAX_CONTENT_BYTES
