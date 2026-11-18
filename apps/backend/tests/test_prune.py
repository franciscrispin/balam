from types import SimpleNamespace

from telegram.error import BadRequest, NetworkError

from balam.commands.prune import handle_prune, topic_exists
from balam.contexts import ContextConfig, ContextsConfig
from balam.router import Router
from balam.store import SessionStore

CHAT = -1001234567890


class _ProbeBot:
    """Answers ``reopenForumTopic`` the way Telegram does: open topics are
    TOPIC_NOT_MODIFIED, deleted ones TOPIC_ID_INVALID, closed ones succeed."""

    def __init__(
        self,
        *,
        gone: set[int] = frozenset(),
        closed: set[int] = frozenset(),
        broken: set[int] = frozenset(),
    ) -> None:
        self.gone, self.closed, self.broken = set(gone), set(closed), set(broken)
        self.reopened: list[int] = []
        self.reclosed: list[int] = []

    async def reopen_forum_topic(self, *, chat_id: int, message_thread_id: int) -> bool:
        if message_thread_id in self.broken:
            raise NetworkError("boom")
        if message_thread_id in self.gone:
            raise BadRequest("Topic_id_invalid")
        if message_thread_id in self.closed:
            self.reopened.append(message_thread_id)
            return True
        raise BadRequest("Topic_not_modified")

    async def close_forum_topic(self, *, chat_id: int, message_thread_id: int) -> bool:
        self.reclosed.append(message_thread_id)
        return True


class _Status:
    def __init__(self) -> None:
        self.edited: list[str] = []

    async def edit_text(self, text: str, **_: object) -> None:
        self.edited.append(text)


class _Message:
    def __init__(self) -> None:
        self.chat_id = CHAT
        self.replies: list[str] = []
        self.status = _Status()

    async def reply_text(self, text: str, **_: object) -> _Status:
        self.replies.append(text)
        return self.status


def _router() -> Router:
    contexts = ContextsConfig(
        default_context="balam",
        contexts={"balam": ContextConfig(directory="/work/balam", description="Balam")},
    )
    return Router(SessionStore(":memory:"), SimpleNamespace(), contexts)


def _env(bot: _ProbeBot, router: Router):
    message = _Message()
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"router": router}), bot=bot)
    return update, context, message


async def test_topic_exists_reads_telegram_answers() -> None:
    bot = _ProbeBot(gone={2}, closed={3}, broken={4})
    assert await topic_exists(bot, CHAT, 1) is True
    assert await topic_exists(bot, CHAT, 2) is False
    assert await topic_exists(bot, CHAT, 3) is True
    assert await topic_exists(bot, CHAT, 4) is None
    # The closed topic the probe reopened is closed again.
    assert bot.reopened == [3] and bot.reclosed == [3]


async def test_prune_forgets_only_topics_telegram_says_are_gone() -> None:
    router = _router()
    for thread_id in (101, 202, 303, 404):
        router._store.set(CHAT, thread_id, f"ses_{thread_id}", thread_id)
        router.set_topic_title(CHAT, thread_id, f"topic {thread_id}")
    bot = _ProbeBot(gone={202, 404}, broken={303})
    update, context, message = _env(bot, router)

    await handle_prune(update, context)

    remaining = {t for t, _, _ in router.list_topics(CHAT)}
    # Gone topics are purged; the live one and the unprobeable one are kept.
    assert remaining == {101, 303}
    summary = message.status.edited[-1]
    assert "Forgot 2 topic(s)" in summary
    assert "topic 202" in summary and "topic 404" in summary
    assert "1 topic(s) still open" in summary
    assert "1 couldn't be checked" in summary


async def test_prune_with_nothing_stale() -> None:
    router = _router()
    router._store.set(CHAT, 101, "ses_101", 1)
    update, context, message = _env(_ProbeBot(), router)

    await handle_prune(update, context)

    assert {t for t, _, _ in router.list_topics(CHAT)} == {101}
    assert "nothing to prune" in message.status.edited[-1]


async def test_prune_with_no_topics() -> None:
    update, context, message = _env(_ProbeBot(), _router())
    await handle_prune(update, context)
    assert message.replies == ["No topics to check."]
