from types import SimpleNamespace

from balam.bot import _handle_context, _topic_link, is_owner
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

    async def reply_text(self, text: str, **_: object) -> None:
        self.replies.append(text)


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
    # A one-tap link to the new topic is handed back in the originating chat.
    assert any("https://t.me/c/1234567890/777" in reply for reply in message.replies)


async def test_context_switch_in_private_chat_links_via_web() -> None:
    # The real environment: a private bot↔owner DM (positive chat id), where the
    # one-tap link is the Telegram Web address built from the bot's id.
    router = _router()
    bot = _FakeBot(new_thread_id=723639, bot_id=BOT_ID)
    message = _FakeMessage(24320651, thread_id=723626)  # owner's user id
    update, context = _update_context(bot, router, message, ["scratch"])

    await _handle_context(update, context)

    assert bot.created_topics == [(24320651, "scratch")]
    assert any(f"https://web.telegram.org/a/#{BOT_ID}_723639" in reply for reply in message.replies)


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
