import asyncio

from balam.approvals import Choice, PendingApprovals
from balam.attachments import PromptFile
from balam.streamer import DraftSession, stream_reply


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


# --- stream_reply event-loop regression tests ---------------------------------
#
# These reproduce OpenCode's real SSE ordering (captured live): the assistant's
# message.updated precedes its text parts, and the user's prompt echoes back as
# a text part that must not be rendered. The original bug was prompting before
# subscribing, which missed the assistant's message.updated entirely.

SID = "ses_test"
UID = "msg_user"
AID = "msg_assistant"


def _ev(etype: str, **props: object) -> dict[str, object]:
    return {"type": etype, "properties": props}


def _msg_updated(role: str, mid: str) -> dict[str, object]:
    return _ev("message.updated", info={"sessionID": SID, "role": role, "id": mid})


def _text_part(mid: str, text: str, pid: str = "prt_1") -> dict[str, object]:
    return _ev(
        "message.part.updated",
        part={"type": "text", "sessionID": SID, "messageID": mid, "id": pid, "text": text},
    )


def _tool_part(
    call_id: str, tool: str, state: dict[str, object], pid: str = "prt_tool"
) -> dict[str, object]:
    return _ev(
        "message.part.updated",
        part={
            "type": "tool",
            "sessionID": SID,
            "messageID": AID,
            "id": pid,
            "callID": call_id,
            "tool": tool,
            "state": state,
        },
    )


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.keyboards: list[object] = []

    async def send_chat_action(self, **kwargs: object) -> None:
        pass

    async def send_message_draft(self, **kwargs: object) -> None:
        pass

    async def send_message(
        self, *, text: str, reply_markup: object = None, **kwargs: object
    ) -> None:
        self.messages.append(text)
        if reply_markup is not None:
            self.keyboards.append(reply_markup)


class FakeOpenCode:
    """Yields a scripted event list; records that prompt() is called."""

    def __init__(self, events: list[dict[str, object]]) -> None:
        self._events = events
        self.prompted = False
        self.prompt_kwargs: dict[str, object] = {}
        self.events_directory: object = "<unset>"

    async def prompt(self, session_id: str, text: str, **kwargs: object) -> None:
        self.prompted = True
        self.prompt_kwargs = kwargs

    async def events(self, *, directory: str | None = None, ready: asyncio.Event | None = None):
        self.events_directory = directory
        if ready is not None:
            ready.set()
        for event in self._events:
            yield event


async def _run(events: list[dict[str, object]], *, directory: str | None = None) -> FakeBot:
    bot = FakeBot()
    await stream_reply(
        bot=bot,
        opencode=FakeOpenCode(events),
        session_id=SID,
        chat_id=1,
        thread_id=99,
        prompt="hello",
        directory=directory,
        draft_interval=0.01,  # tiny: finalize waits on the flusher's sleep
    )
    return bot


async def test_stream_reply_captures_assistant_text_and_skips_user_echo() -> None:
    bot = await _run(
        [
            _msg_updated("user", UID),
            _text_part(UID, "hello"),  # echoed prompt — skip
            _msg_updated("assistant", AID),
            _text_part(AID, "hey"),
            _text_part(AID, "hey there"),
            _ev("session.idle", sessionID=SID),
        ]
    )
    assert bot.messages == ["hey there"]


async def test_stream_reply_renders_tool_line_interleaved_with_text() -> None:
    fpath = "/work/proj/src/foo.py"
    bot = await _run(
        [
            _msg_updated("assistant", AID),
            _text_part(AID, "Let me look.", "prt_text1"),
            # Tool invoked (running), then completes — renders only at terminal.
            _tool_part("call_1", "read", {"status": "running", "input": {"filePath": fpath}}),
            _tool_part(
                "call_1",
                "read",
                {"status": "completed", "input": {"filePath": fpath}, "output": "..."},
            ),
            _text_part(AID, "Done.", "prt_text2"),
            _ev("session.idle", sessionID=SID),
        ],
        directory="/work/proj",
    )
    assert len(bot.messages) == 1
    final = bot.messages[0]
    # Tool line, path shown relative to the context directory, interleaved
    # between the two prose fragments in arrival order.
    assert "🔧 Read" in final
    assert "src/foo.py" in final
    assert "/work/proj" not in final
    assert final.index("look") < final.index("Read") < final.index("Done")


async def test_stream_reply_truncates_bash_output() -> None:
    long_output = "\n".join(f"line {i}" for i in range(200))
    bot = await _run(
        [
            _msg_updated("assistant", AID),
            _tool_part(
                "call_b",
                "bash",
                {"status": "completed", "input": {"command": "seq 200"}, "output": long_output},
            ),
            _ev("session.idle", sessionID=SID),
        ]
    )
    final = bot.messages[0]
    assert "🔧 Bash" in final
    assert "seq 200" in final  # the command is shown
    assert "truncated" in final
    assert "line 199" in final  # the tail is kept
    assert "line 0" not in final  # the head is dropped


class PromptGatedOpenCode(FakeOpenCode):
    """Emits events only after prompt() — mirrors OpenCode (the assistant replies
    to the prompt), so the prompt is deterministically sent before the stream
    drains."""

    def __init__(self, events: list[dict[str, object]]) -> None:
        super().__init__(events)
        self._gate = asyncio.Event()

    async def prompt(self, session_id: str, text: str, **kwargs: object) -> None:
        await super().prompt(session_id, text, **kwargs)
        self._gate.set()

    async def events(self, *, directory: str | None = None, ready: asyncio.Event | None = None):
        self.events_directory = directory
        if ready is not None:
            ready.set()
        await self._gate.wait()
        for event in self._events:
            yield event


async def test_stream_reply_forwards_context_to_prompt() -> None:
    bot = FakeBot()
    oc = PromptGatedOpenCode(
        [
            _msg_updated("assistant", AID),
            _text_part(AID, "ok"),
            _ev("session.idle", sessionID=SID),
        ]
    )
    await stream_reply(
        bot=bot,
        opencode=oc,
        session_id=SID,
        chat_id=1,
        thread_id=99,
        prompt="hello",
        directory="/work/proj",
        provider="anthropic",
        model="claude-opus-4-8",
        effort="high",
        draft_interval=0.01,
    )
    assert oc.prompt_kwargs == {
        "directory": "/work/proj",
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "effort": "high",
        "files": None,
    }
    # OpenCode scopes message/session events to the worktree, so the event
    # subscription must carry the same directory or only server.* events arrive
    # and the reply never finalizes (regression: subscribed without directory).
    assert oc.events_directory == "/work/proj"


async def test_stream_reply_forwards_files_to_prompt() -> None:
    bot = FakeBot()
    oc = PromptGatedOpenCode(
        [
            _msg_updated("assistant", AID),
            _text_part(AID, "got it"),
            _ev("session.idle", sessionID=SID),
        ]
    )
    files = [PromptFile(mime="image/jpeg", url="data:image/jpeg;base64,AAAA")]

    await stream_reply(
        bot=bot,
        opencode=oc,
        session_id=SID,
        chat_id=1,
        thread_id=99,
        prompt="see this",
        files=files,
        draft_interval=0.01,
    )

    assert oc.prompt_kwargs["files"] == files


# --- permission.asked handling (interactive approval) -------------------------


def _permission(request_id: str, call_id: str, category: str = "read") -> dict[str, object]:
    return _ev(
        "permission.asked",
        id=request_id,
        sessionID=SID,
        permission=category,
        patterns=[],
        metadata={},
        always=[],
        tool={"messageID": AID, "callID": call_id},
    )


class PermissionOpenCode(FakeOpenCode):
    """Records ``reply_permission`` calls and lets the event script wait for a
    reply before proceeding (``"WAIT_REPLY"`` sentinel), mirroring how OpenCode
    stays busy until a permission is answered."""

    def __init__(self, events: list[object]) -> None:
        super().__init__([])
        self._script = events
        self.replies: list[tuple[str, str, str | None]] = []
        self._replied = asyncio.Event()

    async def reply_permission(
        self,
        request_id: str,
        reply: str,
        *,
        directory: str | None = None,
        message: str | None = None,
    ) -> None:
        self.replies.append((request_id, reply, message))
        self._replied.set()

    async def events(self, *, directory: str | None = None, ready: asyncio.Event | None = None):
        self.events_directory = directory
        if ready is not None:
            ready.set()
        for event in self._script:
            if event == "WAIT_REPLY":
                await self._replied.wait()
                continue
            yield event


def _token_from_keyboard(markup: object) -> str:
    for row in markup.inline_keyboard:  # type: ignore[attr-defined]
        for button in row:
            if button.callback_data and button.callback_data.startswith("appr:"):
                return button.callback_data.split(":", 2)[2]
    raise AssertionError("no approval button found")


async def test_permission_auto_allows_read_in_workspace() -> None:
    pending = PendingApprovals()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _tool_part(
                "c1", "read", {"status": "running", "input": {"filePath": "/work/proj/a.py"}}
            ),
            _permission("per_1", "c1", category="read"),
            "WAIT_REPLY",
            _ev("session.idle", sessionID=SID),
        ]
    )
    bot = FakeBot()
    await stream_reply(
        bot=bot,
        opencode=oc,
        session_id=SID,
        chat_id=1,
        thread_id=99,
        prompt="x",
        directory="/work/proj",
        pending=pending,
        allowed_dirs=["/work/proj"],
        draft_interval=0.01,
    )
    # Auto-allowed with reply "once"; no keyboard shown.
    assert oc.replies == [("per_1", "once", None)]
    assert bot.keyboards == []


async def test_permission_asks_and_denies_out_of_scope_read() -> None:
    pending = PendingApprovals()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _tool_part("c1", "read", {"status": "running", "input": {"filePath": "/etc/hosts"}}),
            _permission("per_1", "c1", category="read"),
            "WAIT_REPLY",
            _ev("session.idle", sessionID=SID),
        ]
    )
    bot = FakeBot()
    task = asyncio.create_task(
        stream_reply(
            bot=bot,
            opencode=oc,
            session_id=SID,
            chat_id=1,
            thread_id=99,
            prompt="x",
            directory="/work/proj",
            pending=pending,
            allowed_dirs=["/work/proj"],
            draft_interval=0.01,
        )
    )
    # Wait for the approval keyboard, then tap "Deny".
    for _ in range(200):
        if bot.keyboards:
            break
        await asyncio.sleep(0.01)
    assert bot.keyboards, "expected an approval keyboard"
    token = _token_from_keyboard(bot.keyboards[0])
    assert pending.resolve(token, Choice.DENY) is True
    await task
    assert oc.replies == [("per_1", "reject", "Denied by the user.")]


async def test_permission_accept_all_edits_unblocks_session() -> None:
    pending = PendingApprovals()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _tool_part(
                "c1", "edit", {"status": "running", "input": {"filePath": "/work/proj/a.py"}}
            ),
            _permission("per_1", "c1", category="edit"),
            "WAIT_REPLY",
            _ev("session.idle", sessionID=SID),
        ]
    )
    bot = FakeBot()
    task = asyncio.create_task(
        stream_reply(
            bot=bot,
            opencode=oc,
            session_id=SID,
            chat_id=1,
            thread_id=99,
            prompt="x",
            directory="/work/proj",
            pending=pending,
            allowed_dirs=["/work/proj"],
            draft_interval=0.01,
        )
    )
    for _ in range(200):
        if bot.keyboards:
            break
        await asyncio.sleep(0.01)
    assert bot.keyboards, "an in-workspace edit should prompt before accept-all"
    token = _token_from_keyboard(bot.keyboards[0])
    pending.resolve(token, Choice.ALL)
    await task
    # Allowed with "once" for this call, and the session flag is now set so the
    # next in-workspace edit auto-allows.
    assert oc.replies == [("per_1", "once", None)]
    assert pending.is_accept_all_edits(SID) is True


async def test_stream_reply_subscribes_before_prompting() -> None:
    # If the stream is never established, we must not prompt into a dead sub.
    bot = FakeBot()

    class NeverReady(FakeOpenCode):
        async def events(self, *, directory: str | None = None, ready: asyncio.Event | None = None):
            raise RuntimeError("stream failed to open")
            yield  # pragma: no cover  (makes this an async generator)

    oc = NeverReady([])
    try:
        await stream_reply(
            bot=bot,
            opencode=oc,
            session_id=SID,
            chat_id=1,
            thread_id=99,
            prompt="hello",
            draft_interval=0.01,
        )
    except RuntimeError:
        pass
    assert oc.prompted is False
