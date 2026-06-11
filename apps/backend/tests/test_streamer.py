import asyncio
from types import SimpleNamespace

from telegram import InlineKeyboardButton

from balam.approvals import Choice, PendingApprovals, PendingQuestions
from balam.attachments import PromptFile
from balam.streamer import (
    DraftSession,
    _make_transport,
    plan_path_from_question,
    stream_reply,
)


class FakeTransport:
    """Records draft/message/edit calls; can simulate draft failures.

    ``send_message`` hands back an incrementing id so the live-edit fallback has a
    message to edit, mirroring the real transport.
    """

    def __init__(self, *, fail_drafts: bool = False) -> None:
        self.fail_drafts = fail_drafts
        self.ops: list[tuple[str, int | None, str]] = []
        self._next_id = 100

    async def send_draft(self, draft_id: int, text: str) -> None:
        if self.fail_drafts:
            raise RuntimeError("Textdraft_peer_invalid")
        self.ops.append(("draft", draft_id, text))

    async def send_message(self, text: str) -> int | None:
        self.ops.append(("message", None, text))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit_message(self, message_id: int, text: str) -> None:
        self.ops.append(("edit", message_id, text))


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


async def test_failing_draft_falls_back_to_live_edit_streaming() -> None:
    # A group chat rejects sendMessageDraft → switch to live-edit: send one
    # message, then edit it in place as the text grows (no silent gap).
    t = FakeTransport(fail_drafts=True)
    session = DraftSession(t, draft_id=7, render=_identity)

    session.set_text("hi")
    await session.flush_draft()  # draft fails → send the live-edit message now
    assert session.drafts_disabled is True

    session.set_text("hi there")
    await session.flush_draft()  # edits the same message in place
    session.set_text("hi there friend")
    await session.flush_draft()

    assert t.ops == [
        ("message", None, "hi"),
        ("edit", 100, "hi there"),
        ("edit", 100, "hi there friend"),
    ]


async def test_native_drafts_off_streams_live_edit_without_a_draft_attempt() -> None:
    # A group/supergroup caller disables drafts up front: live-edit from the
    # first flush, with no doomed sendMessageDraft call to fail first.
    t = FakeTransport(fail_drafts=True)  # would raise if a draft were attempted
    session = DraftSession(t, draft_id=7, render=_identity, native_drafts=False)
    assert session.drafts_disabled is True

    session.set_text("hi")
    await session.flush_draft()
    session.set_text("hi there")
    await session.flush_draft()

    assert t.ops == [
        ("message", None, "hi"),
        ("edit", 100, "hi there"),
    ]


async def test_live_edit_skips_unchanged_render() -> None:
    # Different raw text that renders identically must not trigger a redundant edit.
    def collapse(text: str) -> list[str]:
        return [text.strip()] if text.strip() else []

    t = FakeTransport(fail_drafts=True)
    session = DraftSession(t, draft_id=7, render=collapse)
    session.set_text("hi")
    await session.flush_draft()  # sends ("message", 100, "hi")
    session.set_text("hi ")  # dirty, but renders to "hi" → no redundant edit
    await session.flush_draft()
    assert t.ops == [("message", None, "hi")]


async def test_finalize_reuses_live_edit_message_for_first_chunk() -> None:
    # After live-edit streaming, finalize edits the streamed bubble (no duplicate)
    # and sends overflow chunks as new messages.
    t = FakeTransport(fail_drafts=True)
    session = DraftSession(t, draft_id=7, render=_chunk5)
    session.set_text("abc")  # one chunk → starts live-edit, shows "abc"
    await session.flush_draft()
    assert t.ops == [("message", None, "abc")]

    session.set_text("abcdefgh")  # final text, now two chunks
    await session.finalize()
    # first chunk edits the existing bubble (text changed); overflow is a new message.
    assert t.ops == [
        ("message", None, "abc"),
        ("edit", 100, "abcde"),
        ("message", None, "fgh"),
    ]


async def test_finalize_skips_redundant_edit_when_unchanged() -> None:
    # If the streamed bubble already shows the final first chunk, finalize must not
    # re-edit it (Telegram 400 "message is not modified").
    t = FakeTransport(fail_drafts=True)
    session = DraftSession(t, draft_id=7, render=_identity)
    session.set_text("done")
    await session.flush_draft()  # live-edit message shows "done"
    await session.finalize()  # same text → no edit
    assert t.ops == [("message", None, "done")]


async def test_live_edit_defers_while_text_overflows_one_chunk() -> None:
    # While streaming text spans >1 chunk, live-edit holds off (finalize handles
    # the split) rather than thrashing the single edited message.
    t = FakeTransport(fail_drafts=True)
    session = DraftSession(t, draft_id=7, render=_chunk5)
    session.set_text("abcdefghij")  # two chunks immediately
    await session.flush_draft()
    assert t.ops == []  # nothing streamed yet


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


# --- the real transport wiring (_make_transport) ------------------------------


class _RecordBot:
    """A bot stub recording PTB calls; can simulate MarkdownV2 parse failures."""

    def __init__(self, *, fail_markdown: bool = False) -> None:
        self.fail_markdown = fail_markdown
        self.calls: list[tuple] = []
        self._id = 500

    async def send_message_draft(self, **kwargs: object) -> None:
        self.calls.append(("draft", kwargs))

    async def send_message(self, *, chat_id: int, text: str, **kwargs: object):
        if self.fail_markdown and kwargs.get("parse_mode") == "MarkdownV2":
            raise RuntimeError("can't parse entities")
        self.calls.append(("send", text, kwargs.get("parse_mode")))
        self._id += 1
        return SimpleNamespace(message_id=self._id)

    async def edit_message_text(
        self, *, chat_id: int, message_id: int, text: str, **kwargs: object
    ) -> None:
        if self.fail_markdown and kwargs.get("parse_mode") == "MarkdownV2":
            raise RuntimeError("can't parse entities")
        self.calls.append(("edit", message_id, text, kwargs.get("parse_mode")))


async def test_transport_send_returns_id_and_edit_wires_through() -> None:
    bot = _RecordBot()
    transport = _make_transport(bot, chat_id=1, thread_id=7)

    mid = await transport.send_message("hello")
    assert mid == 501  # the live-edit fallback needs the real message id back

    await transport.edit_message(mid, "hello world")
    assert ("edit", 501, "hello world", "MarkdownV2") in bot.calls


async def test_transport_edit_falls_back_to_plain_text_on_markdown_error() -> None:
    bot = _RecordBot(fail_markdown=True)
    transport = _make_transport(bot, chat_id=1, thread_id=None)

    # send: MarkdownV2 raises → retried as plain text (parse_mode None).
    await transport.send_message("x")
    assert any(c[0] == "send" and c[2] is None for c in bot.calls)

    # edit: MarkdownV2 raises → retried as plain text rather than dropping.
    await transport.edit_message(777, "y")
    assert any(c[0] == "edit" and c[1] == 777 and c[3] is None for c in bot.calls)


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


def _reasoning_part(mid: str, text: str, pid: str = "prt_reasoning") -> dict[str, object]:
    return _ev(
        "message.part.updated",
        part={"type": "reasoning", "sessionID": SID, "messageID": mid, "id": pid, "text": text},
    )


def _metadata_reasoning_text_part(
    mid: str, text: str, pid: str = "prt_metadata_reasoning"
) -> dict[str, object]:
    return _ev(
        "message.part.updated",
        part={
            "type": "text",
            "sessionID": SID,
            "messageID": mid,
            "id": pid,
            "text": text,
            "metadata": {"type": "reasoning"},
        },
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


async def test_stream_reply_in_group_chat_never_attempts_a_native_draft() -> None:
    # Negative chat_id (group/supergroup) → live-edit streaming from the start;
    # sendMessageDraft is private-chat only and must not be tried at all.
    class DraftRecordingBot(FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.draft_calls = 0

        async def send_message_draft(self, **kwargs: object) -> None:
            self.draft_calls += 1

    bot = DraftRecordingBot()
    await stream_reply(
        bot=bot,
        opencode=FakeOpenCode(
            [
                _msg_updated("assistant", AID),
                _text_part(AID, "hey there"),
                _ev("session.idle", sessionID=SID),
            ]
        ),
        session_id=SID,
        chat_id=-1003953430909,
        thread_id=99,
        prompt="hello",
        draft_interval=0.01,
    )
    assert bot.draft_calls == 0
    assert bot.messages == ["hey there"]


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


def _retry_status(attempt: int, message: str) -> dict[str, object]:
    return _ev(
        "session.status",
        sessionID=SID,
        status={"type": "retry", "attempt": attempt, "message": message, "next": 0},
    )


async def test_stream_reply_posts_single_retry_notice() -> None:
    bot = await _run(
        [
            _msg_updated("assistant", AID),
            _retry_status(1, "Too Many Requests: the usage limit has been reached"),
            _retry_status(2, "Too Many Requests: the usage limit has been reached"),
            _text_part(AID, "recovered"),
            _ev("session.idle", sessionID=SID),
        ]
    )
    notices = [m for m in bot.messages if "rate-limited" in m]
    # One notice per turn even across multiple retry attempts; points at /cancel.
    assert len(notices) == 1
    assert "/cancel" in notices[0]
    assert "the usage limit has been reached" in notices[0]
    # The retry notice is separate from the finalized answer.
    assert "recovered" in bot.messages


async def test_stream_reply_ignores_non_retry_status() -> None:
    bot = await _run(
        [
            _msg_updated("assistant", AID),
            _ev("session.status", sessionID=SID, status={"type": "busy"}),
            _text_part(AID, "hello"),
            _ev("session.idle", sessionID=SID),
        ]
    )
    assert not any("rate-limited" in m for m in bot.messages)


async def test_stream_reply_sends_reasoning_separately_from_answer() -> None:
    bot = await _run(
        [
            _msg_updated("assistant", AID),
            _reasoning_part(AID, "I should be sent as reasoning."),
            _text_part(AID, "the answer"),
            _ev("session.idle", sessionID=SID),
        ]
    )
    assert bot.messages == [r"I should be sent as reasoning\.", "the answer"]


async def test_stream_reply_sends_metadata_reasoning_separately_from_answer() -> None:
    bot = await _run(
        [
            _msg_updated("assistant", AID),
            _metadata_reasoning_text_part(AID, "I should be sent as reasoning."),
            _text_part(AID, "the answer"),
            _ev("session.idle", sessionID=SID),
        ]
    )
    assert bot.messages == [r"I should be sent as reasoning\.", "the answer"]


async def test_stream_reply_renders_tool_line_separately_from_answer() -> None:
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
    assert len(bot.messages) == 2
    reasoning, answer = bot.messages
    # Tool line is progress/reasoning, not part of the answer message. Path is
    # still shown relative to the context directory.
    assert "🔧 Read" in reasoning
    assert "src/foo.py" in reasoning
    assert "/work/proj" not in reasoning
    assert answer == r"Let me look\.Done\."


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
        "agent": None,
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
        self.question_replies: list[tuple[str, list[list[str]]]] = []
        self.question_rejections: list[str] = []
        self._replied = asyncio.Event()
        self._question_replied = asyncio.Event()

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

    async def reply_question(
        self, request_id: str, answers: list[list[str]], *, directory: str | None = None
    ) -> None:
        self.question_replies.append((request_id, answers))
        self._question_replied.set()

    async def reject_question(self, request_id: str, *, directory: str | None = None) -> None:
        self.question_rejections.append(request_id)
        self._question_replied.set()

    async def events(self, *, directory: str | None = None, ready: asyncio.Event | None = None):
        self.events_directory = directory
        if ready is not None:
            ready.set()
        for event in self._script:
            if event == "WAIT_REPLY":
                await self._replied.wait()
                continue
            if event == "WAIT_QUESTION_REPLY":
                await self._question_replied.wait()
                continue
            yield event


def _token_from_keyboard(markup: object) -> str:
    for row in markup.inline_keyboard:  # type: ignore[attr-defined]
        for button in row:
            if button.callback_data and button.callback_data.startswith("appr:"):
                return button.callback_data.split(":", 2)[2]
    raise AssertionError("no approval button found")


def _question_callback(markup: object, label: str) -> str:
    for row in markup.inline_keyboard:  # type: ignore[attr-defined]
        for button in row:
            if button.text == label:
                return button.callback_data
    raise AssertionError(f"no question button {label!r} found")


def _question(request_id: str, *, multiple: bool = False) -> dict[str, object]:
    return _ev(
        "question.asked",
        id=request_id,
        sessionID=SID,
        questions=[
            {
                "question": "Pick a weather.",
                "header": "Weather",
                "options": [
                    {"label": "Sunny", "description": "Bright."},
                    {"label": "Rainy", "description": "Cozy."},
                ],
                "multiple": multiple,
            },
            {
                "question": "Pick a snack.",
                "header": "Snack",
                "options": [
                    {"label": "Fruit", "description": "Fresh."},
                    {"label": "Chips", "description": "Salty."},
                ],
                "multiple": multiple,
            },
        ],
        tool={"messageID": AID, "callID": "cq"},
    )


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


async def test_permission_prompt_includes_category_when_different_from_tool() -> None:
    pending = PendingApprovals()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _tool_part(
                "c1",
                "bash",
                {
                    "status": "running",
                    "input": {"command": "git status --short", "workdir": "/work/other"},
                },
            ),
            _permission("per_1", "c1", category="external_directory"),
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
    assert bot.keyboards, "expected an approval keyboard"
    assert "Permission:" in bot.messages[0]
    assert "external_directory" in bot.messages[0]
    token = _token_from_keyboard(bot.keyboards[0])
    assert pending.resolve(token, Choice.DENY) is True
    await task


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


async def test_question_permission_auto_allows_without_keyboard() -> None:
    pending = PendingApprovals()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _permission("per_q", "cq", category="question"),
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

    assert oc.replies == [("per_q", "once", None)]
    assert bot.keyboards == []


async def test_question_asked_sends_inline_questions_and_replies() -> None:
    pending_questions = PendingQuestions()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _question("q_1"),
            "WAIT_QUESTION_REPLY",
            _text_part(AID, "thanks"),
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
            pending_questions=pending_questions,
            draft_interval=0.01,
        )
    )

    for _ in range(200):
        if len(bot.keyboards) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(bot.keyboards) == 2
    assert _question_callback(bot.keyboards[0], "Type your own answer").startswith("qstc:")
    first = _question_callback(bot.keyboards[0], "Sunny")
    second = _question_callback(bot.keyboards[1], "Chips")
    _, token, q_index, o_index = first.split(":")
    assert pending_questions.resolve(token, int(q_index), int(o_index)) is True
    _, token, q_index, o_index = second.split(":")
    assert pending_questions.resolve(token, int(q_index), int(o_index)) is True

    await task
    assert oc.question_replies == [("q_1", [["Sunny"], ["Chips"]])]
    assert oc.question_rejections == []


async def test_question_asked_multi_select_preserves_multiple_answers() -> None:
    pending_questions = PendingQuestions()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _question("q_1", multiple=True),
            "WAIT_QUESTION_REPLY",
            _text_part(AID, "thanks"),
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
            pending_questions=pending_questions,
            draft_interval=0.01,
        )
    )

    for _ in range(200):
        if len(bot.keyboards) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(bot.keyboards) == 2
    assert _question_callback(bot.keyboards[0], "☐ Sunny")
    assert _question_callback(bot.keyboards[0], "Done").startswith("qstd:")
    first = _question_callback(bot.keyboards[0], "☐ Sunny")
    second = _question_callback(bot.keyboards[0], "☐ Rainy")
    _, token, q_index, o_index = first.split(":")
    assert pending_questions.toggle(token, int(q_index), int(o_index)) is True
    _, token, q_index, o_index = second.split(":")
    assert pending_questions.toggle(token, int(q_index), int(o_index)) is True
    assert pending_questions.finish_multi(token, 0) is True
    first = _question_callback(bot.keyboards[1], "☐ Chips")
    _, token, q_index, o_index = first.split(":")
    assert pending_questions.toggle(token, int(q_index), int(o_index)) is True
    assert pending_questions.finish_multi(token, 1) is True

    await task
    assert oc.question_replies == [("q_1", [["Sunny", "Rainy"], ["Chips"]])]
    assert oc.question_rejections == []


# --- plan_exit questions ("View plan" Mini App button) ------------------------


def _plan_question(request_id: str, plan_rel: str = ".opencode/plans/1-x.md") -> dict[str, object]:
    return _ev(
        "question.asked",
        id=request_id,
        sessionID=SID,
        questions=[
            {
                "question": (
                    f"Plan at {plan_rel} is complete. Would you like to switch to the "
                    "build agent and start implementing?"
                ),
                "header": "Build Agent",
                "custom": False,
                "options": [
                    {"label": "Yes", "description": "Switch to build agent"},
                    {"label": "No", "description": "Keep planning"},
                ],
            }
        ],
        tool={"messageID": AID, "callID": "cp"},
    )


def test_plan_path_from_question_resolves_relative_path() -> None:
    request = {
        "questions": [{"question": "Plan at .opencode/plans/1-x.md is complete. Switch?"}],
        "tool": {"messageID": AID, "callID": "cp"},
    }
    tool_parts = {"cp": ("plan_exit", {}, "running")}
    assert (
        plan_path_from_question(request, tool_parts, "/work/proj")
        == "/work/proj/.opencode/plans/1-x.md"
    )


def test_plan_path_from_question_resolves_upward_relative_path() -> None:
    # Non-git directories: OpenCode renders the global plans dir relative to the
    # worktree, which goes upward.
    request = {"questions": [{"question": "Plan at ../../.local/x.md is complete."}]}
    assert plan_path_from_question(request, {}, "/home/u/proj") == "/home/.local/x.md"


def test_plan_path_from_question_rejects_other_tools() -> None:
    # The text matches, but the owning tool (by callID) is not plan_exit.
    request = {
        "questions": [{"question": "Plan at foo.md is complete, just quoting docs."}],
        "tool": {"messageID": AID, "callID": "cq"},
    }
    tool_parts = {"cq": ("question", {}, "running")}
    assert plan_path_from_question(request, tool_parts, "/work/proj") is None


def test_plan_path_from_question_none_for_ordinary_questions() -> None:
    request = {"questions": [{"question": "Pick a weather."}]}
    assert plan_path_from_question(request, {}, "/work/proj") is None


async def test_plan_exit_question_carries_view_plan_button(tmp_path) -> None:
    plan_file = tmp_path / ".opencode" / "plans" / "1-x.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# The plan\n\n- do the thing")

    pending_questions = PendingQuestions()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _tool_part("cp", "plan_exit", {"status": "running", "input": {}}),
            _plan_question("q_plan"),
            "WAIT_QUESTION_REPLY",
            _ev("session.idle", sessionID=SID),
        ]
    )
    bot = FakeBot()
    seen: list[tuple[str, str]] = []

    def plan_view(title: str, content: str) -> InlineKeyboardButton:
        seen.append((title, content))
        return InlineKeyboardButton("📋 View plan", url="https://t.me/b/app?startapp=markdown__c_x")

    task = asyncio.create_task(
        stream_reply(
            bot=bot,
            opencode=oc,
            session_id=SID,
            chat_id=1,
            thread_id=99,
            prompt="x",
            directory=str(tmp_path),
            pending_questions=pending_questions,
            plan_view=plan_view,
            draft_interval=0.01,
        )
    )

    for _ in range(200):
        if bot.keyboards:
            break
        await asyncio.sleep(0.01)
    assert bot.keyboards, "expected the plan_exit question keyboard"
    assert seen == [("1-x.md", "# The plan\n\n- do the thing")]
    button_callback = _question_callback(bot.keyboards[0], "📋 View plan")
    assert button_callback is None  # URL button, not a callback button
    # The Yes/No flow is untouched: answer and let the turn finish.
    yes = _question_callback(bot.keyboards[0], "Yes")
    _, token, q_index, o_index = yes.split(":")
    assert pending_questions.resolve(token, int(q_index), int(o_index)) is True
    await task
    assert oc.question_replies == [("q_plan", [["Yes"]])]


async def test_plan_exit_question_without_plan_file_sends_plain_keyboard(tmp_path) -> None:
    pending_questions = PendingQuestions()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _tool_part("cp", "plan_exit", {"status": "running", "input": {}}),
            _plan_question("q_plan"),
            "WAIT_QUESTION_REPLY",
            _ev("session.idle", sessionID=SID),
        ]
    )
    bot = FakeBot()

    def plan_view(title: str, content: str) -> InlineKeyboardButton:  # pragma: no cover
        raise AssertionError("plan_view must not be called when the file is missing")

    task = asyncio.create_task(
        stream_reply(
            bot=bot,
            opencode=oc,
            session_id=SID,
            chat_id=1,
            thread_id=99,
            prompt="x",
            directory=str(tmp_path),  # no plan file under it
            pending_questions=pending_questions,
            plan_view=plan_view,
            draft_interval=0.01,
        )
    )

    for _ in range(200):
        if bot.keyboards:
            break
        await asyncio.sleep(0.01)
    assert bot.keyboards
    labels = [b.text for row in bot.keyboards[0].inline_keyboard for b in row]
    assert "📋 View plan" not in labels
    yes = _question_callback(bot.keyboards[0], "Yes")
    _, token, q_index, o_index = yes.split(":")
    pending_questions.resolve(token, int(q_index), int(o_index))
    await task
    assert oc.question_replies == [("q_plan", [["Yes"]])]


async def test_ordinary_question_does_not_call_plan_view() -> None:
    pending_questions = PendingQuestions()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _question("q_1"),
            "WAIT_QUESTION_REPLY",
            _ev("session.idle", sessionID=SID),
        ]
    )
    bot = FakeBot()

    def plan_view(title: str, content: str) -> InlineKeyboardButton:  # pragma: no cover
        raise AssertionError("plan_view must not be called for ordinary questions")

    task = asyncio.create_task(
        stream_reply(
            bot=bot,
            opencode=oc,
            session_id=SID,
            chat_id=1,
            thread_id=99,
            prompt="x",
            directory="/work/proj",
            pending_questions=pending_questions,
            plan_view=plan_view,
            draft_interval=0.01,
        )
    )
    for _ in range(200):
        if len(bot.keyboards) >= 2:
            break
        await asyncio.sleep(0.01)
    for keyboard, label in ((bot.keyboards[0], "Sunny"), (bot.keyboards[1], "Chips")):
        callback = _question_callback(keyboard, label)
        _, token, q_index, o_index = callback.split(":")
        pending_questions.resolve(token, int(q_index), int(o_index))
    await task
    assert oc.question_replies == [("q_1", [["Sunny"], ["Chips"]])]


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


# --- sticky plan mode (/plan): agent forwarding + on_plan_approved ------------


async def test_stream_reply_forwards_agent_to_prompt() -> None:
    bot = FakeBot()
    oc = PromptGatedOpenCode([_ev("session.idle", sessionID=SID)])
    await stream_reply(
        bot=bot,
        opencode=oc,
        session_id=SID,
        chat_id=1,
        thread_id=99,
        prompt="plan it",
        directory="/work/proj",
        agent="plan",
        draft_interval=0.01,
    )
    assert oc.prompt_kwargs["agent"] == "plan"


async def _run_plan_question_turn(answer_label: str, tmp_path) -> list[str]:
    """Drive a plan_exit question to ``answer_label``; return on_plan_approved calls."""
    plan_file = tmp_path / ".opencode" / "plans" / "1-x.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# The plan")

    pending_questions = PendingQuestions()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _tool_part("cp", "plan_exit", {"status": "running", "input": {}}),
            _plan_question("q_plan"),
            "WAIT_QUESTION_REPLY",
            _ev("session.idle", sessionID=SID),
        ]
    )
    bot = FakeBot()
    approved: list[str] = []

    task = asyncio.create_task(
        stream_reply(
            bot=bot,
            opencode=oc,
            session_id=SID,
            chat_id=1,
            thread_id=99,
            prompt="x",
            directory=str(tmp_path),
            pending_questions=pending_questions,
            on_plan_approved=lambda: approved.append("cleared"),
            draft_interval=0.01,
        )
    )
    for _ in range(200):
        if bot.keyboards:
            break
        await asyncio.sleep(0.01)
    assert bot.keyboards
    callback = _question_callback(bot.keyboards[0], answer_label)
    _, token, q_index, o_index = callback.split(":")
    assert pending_questions.resolve(token, int(q_index), int(o_index)) is True
    await task
    assert oc.question_replies == [("q_plan", [[answer_label]])]
    return approved


async def test_plan_exit_yes_fires_on_plan_approved(tmp_path) -> None:
    # "Yes" switches the session to the build agent server-side; the callback
    # lets the bot drop its sticky plan-mode flag in step.
    assert await _run_plan_question_turn("Yes", tmp_path) == ["cleared"]


async def test_plan_exit_no_does_not_fire_on_plan_approved(tmp_path) -> None:
    # "No" keeps the session in plan mode, so the flag must stay.
    assert await _run_plan_question_turn("No", tmp_path) == []


async def test_ordinary_question_does_not_fire_on_plan_approved() -> None:
    pending_questions = PendingQuestions()
    oc = PermissionOpenCode(
        [
            _msg_updated("assistant", AID),
            _question("q_1"),
            "WAIT_QUESTION_REPLY",
            _ev("session.idle", sessionID=SID),
        ]
    )
    bot = FakeBot()
    approved: list[str] = []

    task = asyncio.create_task(
        stream_reply(
            bot=bot,
            opencode=oc,
            session_id=SID,
            chat_id=1,
            thread_id=99,
            prompt="x",
            directory="/work/proj",
            pending_questions=pending_questions,
            on_plan_approved=lambda: approved.append("cleared"),
            draft_interval=0.01,
        )
    )
    for _ in range(200):
        if len(bot.keyboards) >= 2:
            break
        await asyncio.sleep(0.01)
    for keyboard, label in ((bot.keyboards[0], "Sunny"), (bot.keyboards[1], "Chips")):
        callback = _question_callback(keyboard, label)
        _, token, q_index, o_index = callback.split(":")
        pending_questions.resolve(token, int(q_index), int(o_index))
    await task
    assert approved == []
