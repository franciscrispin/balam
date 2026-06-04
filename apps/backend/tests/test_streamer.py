from balam.streamer import DraftSession


class FakeTransport:
    """Records draft/message calls; can simulate draft failures."""

    def __init__(self, *, fail_drafts: bool = False) -> None:
        self.fail_drafts = fail_drafts
        self.ops: list[tuple[str, int | None, str]] = []

    async def send_draft(self, draft_id: int, text: str) -> None:
        if self.fail_drafts:
            raise RuntimeError("drafts unavailable")
        self.ops.append(("draft", draft_id, text))

    async def send_message(self, text: str) -> None:
        self.ops.append(("message", None, text))


# Identity-ish renderer so DraftSession tests are independent of markdown.
def _identity(text: str) -> list[str]:
    return [text] if text else []


# Renderer that splits into fixed-size chunks, to test multi-message finalize.
def _chunk5(text: str) -> list[str]:
    return [text[i : i + 5] for i in range(0, len(text), 5)] if text else []


async def test_flush_sends_draft_only_when_dirty_reusing_one_id() -> None:
    t = FakeTransport()
    session = DraftSession(t, draft_id=42, render=_identity)

    await session.flush_draft()  # not dirty yet → nothing
    assert t.ops == []

    session.set_text("hel")
    await session.flush_draft()
    await session.flush_draft()  # still clean → no duplicate
    session.set_text("hello")
    await session.flush_draft()

    assert t.ops == [("draft", 42, "hel"), ("draft", 42, "hello")]


async def test_set_text_to_same_value_does_not_redirty() -> None:
    t = FakeTransport()
    session = DraftSession(t, draft_id=1, render=_identity)
    session.set_text("same")
    await session.flush_draft()
    session.set_text("same")
    await session.flush_draft()
    assert t.ops == [("draft", 1, "same")]


async def test_failing_draft_disables_drafts() -> None:
    t = FakeTransport(fail_drafts=True)
    session = DraftSession(t, draft_id=7, render=_identity)
    session.set_text("hi")
    await session.flush_draft()
    assert session.drafts_disabled is True
    session.set_text("hi there")
    await session.flush_draft()  # disabled → no-op
    assert t.ops == []


async def test_finalize_sends_real_message() -> None:
    t = FakeTransport()
    session = DraftSession(t, draft_id=1, render=_identity)
    session.set_text("the answer")
    await session.finalize()
    assert t.ops == [("message", None, "the answer")]


async def test_finalize_splits_into_multiple_messages() -> None:
    t = FakeTransport()
    session = DraftSession(t, draft_id=1, render=_chunk5)
    session.set_text("abcdefghij")
    await session.finalize()
    assert t.ops == [("message", None, "abcde"), ("message", None, "fghij")]


async def test_finalize_emits_fallback_when_no_text() -> None:
    t = FakeTransport()
    session = DraftSession(t, draft_id=1, render=_identity)
    await session.finalize("(nothing)")
    assert t.ops == [("message", None, "(nothing)")]
