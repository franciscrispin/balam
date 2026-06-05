from datetime import UTC, datetime
from types import SimpleNamespace

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import CommandHandler, MessageHandler

from balam.bot import (
    BOT_COMMANDS,
    _handle_context,
    _topic_link,
    build_application,
    is_owner,
    register_commands,
)
from balam.contexts import ContextConfig, ContextsConfig
from balam.router import Router, TopicRef
from balam.store import SessionStore

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

    async def create_session(self, title: str, *, directory: str | None = None) -> str:
        self._n += 1
        return f"ses_{self._n}"

    async def session_exists(self, session_id: str, *, directory: str | None = None) -> bool:
        return True


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
        self.sent: list[tuple[int, str, int | None]] = []

    async def create_forum_topic(self, *, chat_id: int, name: str) -> SimpleNamespace:
        self.created_topics.append((chat_id, name))
        return SimpleNamespace(message_thread_id=self._new_thread_id, name=name)

    async def send_message(
        self, *, chat_id: int, text: str, message_thread_id: int | None = None, **_: object
    ) -> None:
        self.sent.append((chat_id, text, message_thread_id))


class _FakeMessage:
    def __init__(self, chat_id: int, thread_id: int | None) -> None:
        self.chat_id = chat_id
        self.message_thread_id = thread_id
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


# --- chat scoping (ADR-0010): the bot acts only in the balamies supergroup -----


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
    return next(h for h in app.handlers[0] if isinstance(h, CommandHandler))


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


def test_bot_commands_includes_context() -> None:
    assert any(c.command == "context" for c in BOT_COMMANDS)
