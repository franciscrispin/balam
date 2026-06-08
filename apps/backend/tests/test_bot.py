import asyncio
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace

from telegram import Chat, Message, MessageEntity, PhotoSize, Update, User
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from balam.approvals import Choice, PendingApprovals
from balam.bot import (
    BOT_COMMANDS,
    _handle_approval_callback,
    _handle_cancel,
    _handle_context,
    _handle_message,
    _handle_new,
    _handle_rename,
    _handle_status,
    _topic_link,
    _topic_name,
    build_application,
    is_owner,
    register_commands,
)
from balam.contexts import ContextConfig, ContextsConfig
from balam.router import Router, TopicRef
from balam.store import SessionStore
from balam.turns import TurnRegistry

OWNER = 424242
SUPERGROUP = -1001234567890


def test_accepts_owner_id() -> None:
    assert is_owner(OWNER, OWNER) is True


def test_rejects_other_id() -> None:
    assert is_owner(999, OWNER) is False


def test_rejects_missing_sender() -> None:
    assert is_owner(None, OWNER) is False


def test_does_not_treat_zero_as_wildcard() -> None:
    assert is_owner(0, OWNER) is False


# --- /context opens a new topic -----------------------------------------------


BOT_ID = 8761754586


def test_topic_link_for_private_supergroup() -> None:
    # -100<internal> → t.me/c/<internal>/<thread> (official, all clients).
    assert _topic_link(SUPERGROUP, 42) == "https://t.me/c/1234567890/42"


def test_topic_link_for_private_chat_uses_web_address() -> None:
    # Private chat with topics has no documented deep link → Telegram Web URL.
    assert _topic_link(24320651, 42, bot_id=BOT_ID) == f"https://web.telegram.org/a/#{BOT_ID}_42"


def test_topic_link_none_for_private_chat_without_bot_id() -> None:
    assert _topic_link(24320651, 42) is None


class _FakeOpenCode:
    def __init__(self) -> None:
        self._n = 0
        self.aborted: list[tuple[str, str | None]] = []

    async def create_session(
        self,
        title: str,
        *,
        directory: str | None = None,
        permission: list[dict[str, str]] | None = None,
    ) -> str:
        self._n += 1
        return f"ses_{self._n}"

    async def session_exists(self, session_id: str, *, directory: str | None = None) -> bool:
        return True

    async def abort_session(self, session_id: str, *, directory: str | None = None) -> None:
        self.aborted.append((session_id, directory))


def _router() -> Router:
    contexts = ContextsConfig(
        default_context="balam",
        contexts={
            "balam": ContextConfig(directory="/work/balam", description="Balam"),
            "scratch": ContextConfig(directory="/work/scratch", description="Scratch"),
        },
    )
    return Router(SessionStore(":memory:"), _FakeOpenCode(), contexts)


class _FakeBot:
    def __init__(self, *, new_thread_id: int = 777, bot_id: int = BOT_ID) -> None:
        self._new_thread_id = new_thread_id
        self.id = bot_id
        self.created_topics: list[tuple[int, str]] = []
        self.deleted_topics: list[tuple[int, int]] = []
        self.edited_topics: list[tuple[int, int, str]] = []
        self.sent: list[tuple[int, str, int | None]] = []

    async def create_forum_topic(self, *, chat_id: int, name: str) -> SimpleNamespace:
        self.created_topics.append((chat_id, name))
        return SimpleNamespace(message_thread_id=self._new_thread_id, name=name)

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        message_thread_id: int | None = None,
        **_: object,
    ) -> None:
        self.sent.append((chat_id, text, message_thread_id))

    async def edit_forum_topic(self, *, chat_id: int, message_thread_id: int, name: str) -> None:
        self.edited_topics.append((chat_id, message_thread_id, name))

    async def delete_forum_topic(self, *, chat_id: int, message_thread_id: int) -> None:
        self.deleted_topics.append((chat_id, message_thread_id))


class _FakeMessage:
    def __init__(self, chat_id: int, thread_id: int | None, *, is_forum: bool = False) -> None:
        self.chat_id = chat_id
        self.message_thread_id = thread_id
        self.chat = SimpleNamespace(is_forum=is_forum)
        self.reply_to_message = None
        self.replies: list[str] = []
        self.reply_markups: list[object] = []

    async def reply_text(self, text: str, *, reply_markup: object = None, **_: object) -> None:
        self.replies.append(text)
        self.reply_markups.append(reply_markup)


def _button_urls(message: _FakeMessage) -> list[str]:
    """Every URL carried by an inline-keyboard button across the message's replies."""
    urls: list[str] = []
    for markup in message.reply_markups:
        if markup is None:
            continue
        for row in markup.inline_keyboard:
            urls.extend(button.url for button in row if button.url)
    return urls


def _update_context(bot: _FakeBot, router: Router, message: _FakeMessage, args: list[str]):
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"router": router}),
        bot=bot,
        args=args,
    )
    return update, context


async def test_context_switch_opens_new_topic_and_links_to_it() -> None:
    router = _router()
    bot = _FakeBot(new_thread_id=777)
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context = _update_context(bot, router, message, ["scratch"])

    await _handle_context(update, context)

    # A new topic is created for the context (not a rebind of thread 5).
    assert bot.created_topics == [(SUPERGROUP, "scratch")]
    # Its session is bound to the new thread, in the scratch workspace.
    assert router.current_context_name(TopicRef(SUPERGROUP, 777, "t")) == "scratch"
    # The new topic is greeted (a message into thread 777).
    assert any(thread == 777 for _chat, _text, thread in bot.sent)
    # A one-tap link to the new topic is handed back as an inline URL button.
    assert "https://t.me/c/1234567890/777" in _button_urls(message)


async def test_context_switch_in_private_chat_links_via_web() -> None:
    # The real environment: a private bot↔owner DM (positive chat id), where the
    # one-tap link is the Telegram Web address built from the bot's id.
    router = _router()
    bot = _FakeBot(new_thread_id=723639, bot_id=BOT_ID)
    message = _FakeMessage(24320651, thread_id=723626)  # owner's user id
    update, context = _update_context(bot, router, message, ["scratch"])

    await _handle_context(update, context)

    assert bot.created_topics == [(24320651, "scratch")]
    assert f"https://web.telegram.org/a/#{BOT_ID}_723639" in _button_urls(message)


async def test_context_switch_from_general_also_opens_a_topic() -> None:
    router = _router()
    bot = _FakeBot(new_thread_id=900)
    message = _FakeMessage(SUPERGROUP, thread_id=None)  # General
    update, context = _update_context(bot, router, message, ["scratch"])

    await _handle_context(update, context)

    assert bot.created_topics == [(SUPERGROUP, "scratch")]
    assert router.current_context_name(TopicRef(SUPERGROUP, 900, "t")) == "scratch"


async def test_unknown_context_is_rejected_without_creating_a_topic() -> None:
    router = _router()
    bot = _FakeBot()
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context = _update_context(bot, router, message, ["nope"])

    await _handle_context(update, context)

    assert bot.created_topics == []
    assert any("Unknown context" in reply for reply in message.replies)


# --- /new, /status, /cancel ---------------------------------------------------


def _session_cmd_env(message: _FakeMessage):
    """An (update, context) pair wired with router + opencode + turns, plus the
    bare opencode/turns/router handles for assertions."""
    opencode = _FakeOpenCode()
    contexts = ContextsConfig(
        default_context="balam",
        contexts={
            "balam": ContextConfig(
                directory="/work/balam",
                description="Balam",
                model="anthropic/claude-opus-4-8",
            ),
        },
    )
    router = Router(SessionStore(":memory:"), opencode, contexts)
    turns = TurnRegistry()
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"router": router, "opencode": opencode, "turns": turns}
        ),
    )
    return update, context, router, opencode, turns


def _sleeping_turn(turns: TurnRegistry, chat_id: int, thread_id: int | None, session_id: str):
    """Register a never-finishing turn (a parked task) for a topic; return it."""
    task = asyncio.ensure_future(asyncio.Event().wait())
    turns.register(chat_id, thread_id, task, session_id, "/work/balam")
    return task


async def test_new_opens_a_new_topic_in_the_current_context() -> None:
    # /new mirrors /context <name>, but reuses the current topic's context.
    router = _router()
    bot = _FakeBot(new_thread_id=900)
    await router.create_topic_session(SUPERGROUP, 5, "scratch", "scratch")  # current topic
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context = _update_context(bot, router, message, [])

    await _handle_new(update, context)

    # A brand-new topic is created, bound to the same context as the current one.
    assert bot.created_topics == [(SUPERGROUP, "scratch")]
    assert router.current_context_name(TopicRef(SUPERGROUP, 900, "t")) == "scratch"
    # The current topic is left untouched — its session is preserved.
    assert router.current_session_id(TopicRef(SUPERGROUP, 5, "t")) is not None
    # A one-tap link to the new topic is handed back.
    assert "https://t.me/c/1234567890/900" in _button_urls(message)


# --- topic naming -------------------------------------------------------------


async def test_first_message_auto_names_existing_topic(monkeypatch) -> None:
    async def fake_stream_reply(**_: object) -> None:
        return None

    monkeypatch.setattr("balam.bot.stream_reply", fake_stream_reply)

    router = _router()
    bot = _FakeBot()
    message = SimpleNamespace(
        chat_id=SUPERGROUP,
        message_thread_id=5,
        chat=SimpleNamespace(is_forum=True),
        photo=[],
        document=None,
        text="Please inspect the failing backend tests and fix them",
        caption=None,
        reply_to_message=None,
    )
    update, context, turns = _message_env(message, bot, router=router)

    await _handle_message(update, context)
    turn = turns.get(SUPERGROUP, 5)
    assert turn is not None
    await turn.task

    assert bot.edited_topics == [
        (SUPERGROUP, 5, "balam: Please inspect the failing backend tests and fix them")
    ]
    assert router.topic_auto_named(TopicRef(SUPERGROUP, 5, "t")) is True


async def test_first_message_does_not_auto_name_twice(monkeypatch) -> None:
    async def fake_stream_reply(**_: object) -> None:
        return None

    monkeypatch.setattr("balam.bot.stream_reply", fake_stream_reply)

    router = _router()
    bot = _FakeBot()
    message = SimpleNamespace(
        chat_id=SUPERGROUP,
        message_thread_id=5,
        chat=SimpleNamespace(is_forum=True),
        photo=[],
        document=None,
        text="first",
        caption=None,
        reply_to_message=None,
    )
    update, context, turns = _message_env(message, bot, router=router)

    await _handle_message(update, context)
    first_turn = turns.get(SUPERGROUP, 5)
    assert first_turn is not None
    await first_turn.task
    message.text = "second"
    await _handle_message(update, context)
    second_turn = turns.get(SUPERGROUP, 5)
    assert second_turn is not None
    await second_turn.task

    assert bot.edited_topics == [(SUPERGROUP, 5, "balam: first")]


async def test_general_message_creates_named_topic_in_current_context(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_stream_reply(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("balam.bot.stream_reply", fake_stream_reply)

    router = _router()
    bot = _FakeBot(new_thread_id=901)
    message = SimpleNamespace(
        chat_id=SUPERGROUP,
        message_thread_id=None,
        chat=SimpleNamespace(is_forum=True),
        photo=[],
        document=None,
        text="Start a focused topic from General",
        caption=None,
        reply_to_message=None,
        replies=[],
        reply_markups=[],
    )

    async def reply_text(text: str, *, reply_markup: object = None, **_: object) -> None:
        message.replies.append(text)
        message.reply_markups.append(reply_markup)

    message.reply_text = reply_text
    update, context, turns = _message_env(message, bot, router=router)

    await _handle_message(update, context)
    turn = turns.get(SUPERGROUP, 901)
    assert turn is not None
    await turn.task

    assert bot.created_topics == [(SUPERGROUP, "balam: Start a focused topic from General")]
    assert router.current_context_name(TopicRef(SUPERGROUP, 901, "t")) == "balam"
    assert router.topic_auto_named(TopicRef(SUPERGROUP, 901, "t")) is True
    assert captured["thread_id"] == 901
    assert "https://t.me/c/1234567890/901" in _button_urls(message)


async def test_rename_changes_current_topic_and_blocks_auto_name() -> None:
    router = _router()
    bot = _FakeBot()
    await router.create_topic_session(SUPERGROUP, 5, "balam", "balam")
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context = _update_context(bot, router, message, ["Build", "fix"])

    await _handle_rename(update, context)

    assert bot.edited_topics == [(SUPERGROUP, 5, "Build fix")]
    assert router.topic_auto_named(TopicRef(SUPERGROUP, 5, "t")) is True
    assert "Renamed topic" in message.replies[-1]


async def test_rename_requires_name() -> None:
    router = _router()
    bot = _FakeBot()
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context = _update_context(bot, router, message, [])

    await _handle_rename(update, context)

    assert bot.edited_topics == []
    assert "Usage" in message.replies[-1]


def test_topic_name_truncates_to_telegram_limit() -> None:
    name = _topic_name("balam", "x" * 200)

    assert len(name) == 128
    assert name.startswith("balam: ")
    assert name.endswith("...")


async def test_new_with_arg_opens_topic_in_named_context() -> None:
    # /new <name> binds the new topic to <name>, not the current topic's context.
    router = _router()
    bot = _FakeBot(new_thread_id=902)
    await router.create_topic_session(SUPERGROUP, 5, "balam", "balam")  # current topic
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context = _update_context(bot, router, message, ["scratch"])

    await _handle_new(update, context)

    assert bot.created_topics == [(SUPERGROUP, "scratch")]
    assert router.current_context_name(TopicRef(SUPERGROUP, 902, "t")) == "scratch"


async def test_new_with_unknown_context_reports_error() -> None:
    router = _router()
    bot = _FakeBot(new_thread_id=903)
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context = _update_context(bot, router, message, ["nope"])

    await _handle_new(update, context)

    assert bot.created_topics == []
    assert "Unknown context" in message.replies[-1]


async def test_new_from_unbound_topic_uses_default_context() -> None:
    router = _router()
    bot = _FakeBot(new_thread_id=901)
    message = _FakeMessage(SUPERGROUP, thread_id=None)  # General — unbound
    update, context = _update_context(bot, router, message, [])

    await _handle_new(update, context)

    assert bot.created_topics == [(SUPERGROUP, "balam")]  # default_context
    assert router.current_context_name(TopicRef(SUPERGROUP, 901, "t")) == "balam"


async def test_status_reports_context_session_and_idle() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, router, _opencode, _turns = _session_cmd_env(message)
    session_id = (await router.resolve(TopicRef(SUPERGROUP, 5, "t"))).session_id

    await _handle_status(update, context)

    reply = message.replies[-1]
    assert "balam" in reply
    assert session_id in reply
    assert "idle" in reply


async def test_status_reports_running_turn() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, _router, _opencode, turns = _session_cmd_env(message)
    task = _sleeping_turn(turns, SUPERGROUP, 5, "ses_running")

    await _handle_status(update, context)

    assert "running" in message.replies[-1]
    task.cancel()


async def test_cancel_with_no_running_turn() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, *_ = _session_cmd_env(message)

    await _handle_cancel(update, context)

    assert any("No running turn" in r for r in message.replies)


async def test_cancel_aborts_running_turn() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, _router, opencode, turns = _session_cmd_env(message)
    task = _sleeping_turn(turns, SUPERGROUP, 5, "ses_running")

    await _handle_cancel(update, context)
    await asyncio.sleep(0)  # let the fire-and-forget abort task run

    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert opencode.aborted == [("ses_running", "/work/balam")]
    assert any("Cancelled" in r for r in message.replies)


# --- chat scoping (ADR-0010): the bot acts only in the workspace supergroup -----


def _config(*, chat_id: int | None) -> SimpleNamespace:
    # build_application only reads these three fields off the config.
    return SimpleNamespace(
        telegram_bot_token="123456:fake-token-for-tests",
        allowed_telegram_user_id=OWNER,
        allowed_telegram_chat_id=chat_id,
    )


def _message_handler(app) -> MessageHandler:
    return next(h for h in app.handlers[0] if isinstance(h, MessageHandler))


def _command_handler(app) -> CommandHandler:
    # All command handlers share the same `allowed` filter; pick /context's.
    return next(
        h for h in app.handlers[0] if isinstance(h, CommandHandler) and "context" in h.commands
    )


def _text_update(chat_id: int, user_id: int, text: str = "hello") -> Update:
    entities = []
    if text.startswith("/"):
        entities = [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text))]
    message = Message(
        message_id=1,
        date=datetime(2026, 6, 5, tzinfo=UTC),
        chat=Chat(id=chat_id, type=Chat.SUPERGROUP),
        from_user=User(id=user_id, is_bot=False, first_name="o"),
        text=text,
        entities=entities,
    )
    # CommandHandler resolves /cmd@<bot> against the bot's username.
    message.set_bot(SimpleNamespace(username="heybalambot"))
    return Update(update_id=1, message=message)


def _build(chat_id: int | None):
    return build_application(_config(chat_id=chat_id), opencode=None, router=None)


def test_message_handler_scoped_accepts_owner_in_target_chat() -> None:
    handler = _message_handler(_build(SUPERGROUP))
    assert handler.check_update(_text_update(SUPERGROUP, OWNER)) is not False


def test_message_handler_scoped_rejects_owner_in_other_chat() -> None:
    handler = _message_handler(_build(SUPERGROUP))
    # Same owner, but the old DM / a different chat — now ignored.
    assert handler.check_update(_text_update(OWNER, OWNER)) is False


def test_message_handler_scoped_rejects_stranger_in_target_chat() -> None:
    handler = _message_handler(_build(SUPERGROUP))
    assert handler.check_update(_text_update(SUPERGROUP, 999)) is False


def test_message_handler_unscoped_accepts_owner_anywhere() -> None:
    # Backward compatible: no chat id → owner-anywhere (legacy DM) behavior.
    handler = _message_handler(_build(None))
    assert handler.check_update(_text_update(OWNER, OWNER)) is not False
    assert handler.check_update(_text_update(SUPERGROUP, OWNER)) is not False


def test_command_handler_scoped_rejects_owner_in_other_chat() -> None:
    handler = _command_handler(_build(SUPERGROUP))
    assert handler.check_update(_text_update(OWNER, OWNER, "/context")) is False


def test_command_handler_scoped_accepts_owner_in_target_chat() -> None:
    handler = _command_handler(_build(SUPERGROUP))
    assert handler.check_update(_text_update(SUPERGROUP, OWNER, "/context")) is not False


# --- command registration (setMyCommands) makes /context work in groups -------


class _RecordingBot:
    def __init__(self) -> None:
        self.calls: list[tuple[type, int | None, tuple[str, ...]]] = []

    async def set_my_commands(self, commands, *, scope=None, **_: object) -> None:
        chat_id = getattr(scope, "chat_id", None)
        self.calls.append((type(scope), chat_id, tuple(c.command for c in commands)))


async def test_register_commands_sets_default_and_group_scopes() -> None:
    bot = _RecordingBot()
    await register_commands(bot, chat_id=None)
    scopes = [c[0].__name__ for c in bot.calls]
    assert "BotCommandScopeDefault" in scopes
    assert "BotCommandScopeAllGroupChats" in scopes
    # No per-chat scope when none is configured.
    assert all(c[1] is None for c in bot.calls)
    # All registrations carry the /context command.
    assert all("context" in c[2] for c in bot.calls)


async def test_register_commands_adds_chat_scope_when_configured() -> None:
    bot = _RecordingBot()
    await register_commands(bot, chat_id=SUPERGROUP)
    chat_scoped = [c for c in bot.calls if c[0].__name__ == "BotCommandScopeChat"]
    assert chat_scoped and chat_scoped[0][1] == SUPERGROUP


def test_bot_commands_includes_all_commands() -> None:
    names = {c.command for c in BOT_COMMANDS}
    assert {"new", "rename", "status", "cancel", "context"} <= names


# --- approval inline-keyboard callback ----------------------------------------


class _FakeCBMessage:
    def __init__(self, chat_id: int = SUPERGROUP) -> None:
        self.text = "🔐 Allow Edit?"
        self.text_markdown_v2 = "🔐 Allow Edit?"
        self.chat = SimpleNamespace(id=chat_id)
        self.edited: list[str] = []

    async def edit_text(self, *, text: str, reply_markup=None, **_: object) -> None:
        self.edited.append(text)


class _FakeQuery:
    def __init__(self, data: str, user_id: int, message: _FakeCBMessage | None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, **_: object) -> None:
        self.answers.append(text)


def _callback_env(query: _FakeQuery, pending: PendingApprovals, *, chat_id: int | None = None):
    config = SimpleNamespace(allowed_telegram_user_id=OWNER, allowed_telegram_chat_id=chat_id)
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"config": config, "pending": pending})
    )
    return update, context


async def test_approval_callback_owner_allow_resolves_future() -> None:
    pending = PendingApprovals()
    token, future = pending.register("ses_x")
    query = _FakeQuery(f"appr:allow:{token}", OWNER, _FakeCBMessage())
    update, context = _callback_env(query, pending)

    await _handle_approval_callback(update, context)

    assert future.done() and future.result() is Choice.ALLOW
    assert query.message.edited  # outcome annotated, keyboard removed


async def test_approval_callback_all_sets_accept_all_edits() -> None:
    pending = PendingApprovals()
    token, future = pending.register("ses_y")
    query = _FakeQuery(f"appr:all:{token}", OWNER, _FakeCBMessage())
    update, context = _callback_env(query, pending)

    await _handle_approval_callback(update, context)

    assert future.result() is Choice.ALL
    assert pending.is_accept_all_edits("ses_y") is True


async def test_approval_callback_ignores_stranger() -> None:
    pending = PendingApprovals()
    token, future = pending.register("ses_x")
    query = _FakeQuery(f"appr:allow:{token}", 999, _FakeCBMessage())
    update, context = _callback_env(query, pending)

    await _handle_approval_callback(update, context)

    assert not future.done()  # a stranger's tap never resolves the approval


async def test_approval_callback_rejects_owner_in_other_chat() -> None:
    pending = PendingApprovals()
    token, future = pending.register("ses_x")
    # Scoped to SUPERGROUP, but the press comes from a different chat.
    query = _FakeQuery(f"appr:allow:{token}", OWNER, _FakeCBMessage(chat_id=111))
    update, context = _callback_env(query, pending, chat_id=SUPERGROUP)

    await _handle_approval_callback(update, context)

    assert not future.done()


async def test_approval_callback_expired_token_is_acknowledged() -> None:
    pending = PendingApprovals()
    query = _FakeQuery("appr:allow:gone", OWNER, _FakeCBMessage())
    update, context = _callback_env(query, pending)

    await _handle_approval_callback(update, context)

    assert any("expired" in (a or "").lower() for a in query.answers)
    # The stale keyboard is still stripped (edit with the original text, no buttons).
    assert query.message.edited


def _callback_handler(app) -> CallbackQueryHandler:
    return next(h for h in app.handlers[0] if isinstance(h, CallbackQueryHandler))


def test_callback_handler_is_registered() -> None:
    # The approval keyboard is routed by a CallbackQueryHandler matching appr:*.
    assert _callback_handler(_build(SUPERGROUP)) is not None


# --- inbound attachments (§4) -------------------------------------------------


class _AttachmentBot:
    """Serves attachment bytes via get_file → download_as_bytearray."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def get_file(self, file_id: str):
        data = self._data

        class _F:
            async def download_as_bytearray(self) -> bytearray:
                return bytearray(data)

        return _F()


def _message_env(message, bot, *, router: Router | None = None):
    opencode = _FakeOpenCode()
    contexts = ContextsConfig(
        default_context="balam",
        contexts={"balam": ContextConfig(directory="/work/balam", description="Balam")},
    )
    router = router or Router(SessionStore(":memory:"), opencode, contexts)
    turns = TurnRegistry()
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "router": router,
                "opencode": opencode,
                "turns": turns,
                "pending": PendingApprovals(),
            }
        ),
        bot=bot,
    )
    return update, context, turns


async def test_message_with_photo_forwards_file_part_and_caption(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_stream_reply(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("balam.bot.stream_reply", fake_stream_reply)

    bot = _AttachmentBot(b"\xff\xd8jpeg")
    message = SimpleNamespace(
        chat_id=SUPERGROUP,
        message_thread_id=5,
        photo=[SimpleNamespace(file_id="large")],
        document=None,
        text=None,
        caption="what is this?",
        reply_to_message=None,
    )
    update, context, turns = _message_env(message, bot)

    await _handle_message(update, context)
    turn = turns.get(SUPERGROUP, 5)
    assert turn is not None
    await turn.task  # let the background turn run to completion

    assert captured["prompt"] == "what is this?"
    files = captured["files"]
    assert [f.mime for f in files] == ["image/jpeg"]
    assert files[0].url.startswith("data:image/jpeg;base64,")


def _photo_update(chat_id: int, user_id: int) -> Update:
    message = Message(
        message_id=1,
        date=datetime(2026, 6, 5, tzinfo=UTC),
        chat=Chat(id=chat_id, type=Chat.SUPERGROUP),
        from_user=User(id=user_id, is_bot=False, first_name="o"),
        photo=(PhotoSize(file_id="f", file_unique_id="u", width=1, height=1),),
    )
    return Update(update_id=1, message=message)


def test_message_handler_accepts_photo_from_owner() -> None:
    # The broadened filter (§4) lets photos through, not just text.
    handler = _message_handler(_build(SUPERGROUP))
    assert handler.check_update(_photo_update(SUPERGROUP, OWNER)) is not False
