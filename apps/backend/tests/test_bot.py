import asyncio
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace

from telegram import Chat, Message, MessageEntity, PhotoSize, Update, User
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

from balam.agent.opencode_backend import OpenCodeBackend
from balam.approvals import Choice, PendingApprovals, PendingDeletions, PendingQuestions
from balam.bot import (
    BOT_COMMANDS,
    _handle_approval_callback,
    _handle_cancel,
    _handle_context,
    _handle_delete_confirm_callback,
    _handle_delete_page_callback,
    _handle_effort,
    _handle_message,
    _handle_model,
    _handle_new,
    _handle_question_callback,
    _handle_question_custom_callback,
    _handle_question_done_callback,
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
from balam.turns import TurnJob, TurnRegistry

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


BOT_ID = 7000000042


def test_topic_link_for_private_supergroup() -> None:
    # -100<internal> → t.me/c/<internal>/<thread> (official, all clients).
    assert _topic_link(SUPERGROUP, 42) == "https://t.me/c/1234567890/42"


def test_topic_link_for_private_chat_uses_web_address() -> None:
    # Private chat with topics has no documented deep link → Telegram Web URL.
    assert _topic_link(55555555, 42, bot_id=BOT_ID) == f"https://web.telegram.org/a/#{BOT_ID}_42"


def test_topic_link_none_for_private_chat_without_bot_id() -> None:
    assert _topic_link(55555555, 42) is None


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
        mcp: dict | None = None,
    ) -> str:
        self._n += 1
        return f"ses_{self._n}"

    async def session_exists(self, session_id: str, *, directory: str | None = None) -> bool:
        return True

    async def update_session_permission(
        self,
        session_id: str,
        *,
        directory: str | None = None,
        permission: list[dict[str, str]],
    ) -> None:
        return None

    async def abort_session(self, session_id: str, *, directory: str | None = None) -> None:
        self.aborted.append((session_id, directory))


def _router() -> Router:
    contexts = ContextsConfig(
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
    message = _FakeMessage(55555555, thread_id=723626)  # fake owner user id
    update, context = _update_context(bot, router, message, ["scratch"])

    await _handle_context(update, context)

    assert bot.created_topics == [(55555555, "scratch")]
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


def _session_cmd_env(message: _FakeMessage, args: list[str] | None = None):
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
                effort="high",
            ),
        },
    )
    router = Router(SessionStore(":memory:"), opencode, contexts)
    turns = TurnRegistry()
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "router": router,
                "backend": OpenCodeBackend(opencode),
                "turns": turns,
                "config": SimpleNamespace(agent_backend="opencode"),
            }
        ),
        args=args or [],
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


async def test_status_reports_effective_model_and_effort_overrides() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, router, _opencode, _turns = _session_cmd_env(message)
    router.set_model_override(SUPERGROUP, 5, "anthropic", "claude-sonnet-4")
    router.set_effort_override(SUPERGROUP, 5, "medium")

    await _handle_status(update, context)

    reply = message.replies[-1]
    assert "Model: anthropic/claude-sonnet-4" in reply
    assert "Effort: medium" in reply


async def test_model_reports_current_effective_value() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, *_ = _session_cmd_env(message)

    await _handle_model(update, context)

    reply = message.replies[-1]
    assert "Model: anthropic/claude-opus-4-8" in reply
    assert "Source: context default" in reply


async def test_model_sets_topic_override() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, router, *_ = _session_cmd_env(message, ["anthropic/claude-sonnet-4"])

    await _handle_model(update, context)

    resolved = await router.resolve(TopicRef(SUPERGROUP, 5, "t"))
    assert resolved.provider == "anthropic"
    assert resolved.model == "claude-sonnet-4"
    assert "Model override set" in message.replies[-1]


async def test_model_reset_clears_topic_override() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, router, *_ = _session_cmd_env(message, ["reset"])
    router.set_model_override(SUPERGROUP, 5, "anthropic", "claude-sonnet-4")

    await _handle_model(update, context)

    resolved = await router.resolve(TopicRef(SUPERGROUP, 5, "t"))
    assert resolved.model == "claude-opus-4-8"
    assert "Model reset" in message.replies[-1]


async def test_model_rejects_unqualified_value() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, router, *_ = _session_cmd_env(message, ["claude-sonnet-4"])

    await _handle_model(update, context)

    assert router.model_override(SUPERGROUP, 5) == (None, None)
    assert "Usage: /model" in message.replies[-1]


async def test_model_rejects_empty_model_part() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, router, *_ = _session_cmd_env(message, ["anthropic/"])

    await _handle_model(update, context)

    assert router.model_override(SUPERGROUP, 5) == (None, None)
    assert "Usage: /model" in message.replies[-1]


async def test_effort_reports_current_effective_value() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, *_ = _session_cmd_env(message)

    await _handle_effort(update, context)

    reply = message.replies[-1]
    assert "Effort: high" in reply
    assert "Source: context default" in reply


async def test_effort_sets_topic_override() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, router, *_ = _session_cmd_env(message, ["medium"])

    await _handle_effort(update, context)

    resolved = await router.resolve(TopicRef(SUPERGROUP, 5, "t"))
    assert resolved.effort == "medium"
    assert "Effort override set" in message.replies[-1]


async def test_effort_reset_clears_topic_override() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, router, *_ = _session_cmd_env(message, ["reset"])
    router.set_effort_override(SUPERGROUP, 5, "medium")

    await _handle_effort(update, context)

    resolved = await router.resolve(TopicRef(SUPERGROUP, 5, "t"))
    assert resolved.effort == "high"
    assert "Effort reset" in message.replies[-1]


async def test_effort_rejects_unknown_value() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, router, *_ = _session_cmd_env(message, ["turbo"])

    await _handle_effort(update, context)

    assert router.effort_override(SUPERGROUP, 5) is None
    assert "Unknown effort" in message.replies[-1]
    assert "xhigh" in message.replies[-1]


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


# --- one-turn-per-topic queueing (ADR-0009) -----------------------------------


def _job(prompt: str) -> TurnJob:
    return TurnJob(
        prompt=prompt,
        session_id="ses_x",
        directory="/work/balam",
        provider=None,
        model=None,
        effort=None,
        allowed_dirs=["/work/balam"],
        files=[],
    )


def _text_msg(chat_id: int, thread_id: int | None, text: str) -> _FakeMessage:
    """A forum text message that ``_handle_message`` can consume end to end."""
    msg = _FakeMessage(chat_id, thread_id, is_forum=True)
    msg.photo = []
    msg.document = None
    msg.text = text
    msg.caption = None
    return msg


def test_turn_registry_queue_is_fifo_and_per_topic() -> None:
    turns = TurnRegistry()
    assert turns.queue_len(SUPERGROUP, 5) == 0
    assert turns.pop_next(SUPERGROUP, 5) is None

    assert turns.enqueue(SUPERGROUP, 5, _job("a")) == 1
    assert turns.enqueue(SUPERGROUP, 5, _job("b")) == 2
    assert turns.enqueue(SUPERGROUP, 6, _job("other")) == 1  # different topic, own queue

    assert turns.queue_len(SUPERGROUP, 5) == 2
    assert turns.pop_next(SUPERGROUP, 5).prompt == "a"
    assert turns.pop_next(SUPERGROUP, 5).prompt == "b"
    assert turns.pop_next(SUPERGROUP, 5) is None
    assert turns.queue_len(SUPERGROUP, 6) == 1

    assert turns.clear_queue(SUPERGROUP, 6) == 1
    assert turns.clear_queue(SUPERGROUP, 6) == 0


async def test_message_during_running_turn_is_queued_then_drains(monkeypatch) -> None:
    prompts: list[str] = []
    gate = asyncio.Event()
    first_started = asyncio.Event()

    async def fake_stream_reply(*, prompt: str, **_: object) -> None:
        prompts.append(prompt)
        if prompt == "first":
            first_started.set()
            await gate.wait()

    monkeypatch.setattr("balam.bot.stream_reply", fake_stream_reply)

    router = _router()
    message = _text_msg(SUPERGROUP, 5, "first")
    update, context, turns = _message_env(message, _FakeBot(), router=router)

    # The first message starts a turn straight away.
    await _handle_message(update, context)
    await asyncio.wait_for(first_started.wait(), 1)
    first_task = turns.get(SUPERGROUP, 5).task

    # A second message arriving mid-turn is queued, not run, and acknowledged.
    message.text = "second"
    await _handle_message(update, context)
    assert prompts == ["first"]
    assert turns.queue_len(SUPERGROUP, 5) == 1
    assert any("Queued" in r for r in message.replies)

    # Finishing the first turn drains the queued message onto the same session.
    gate.set()
    await first_task
    while (turn := turns.get(SUPERGROUP, 5)) is not None:
        await turn.task
    assert prompts == ["first", "second"]
    assert turns.queue_len(SUPERGROUP, 5) == 0


async def test_queued_messages_drain_in_fifo_order(monkeypatch) -> None:
    prompts: list[str] = []
    gate = asyncio.Event()
    first_started = asyncio.Event()

    async def fake_stream_reply(*, prompt: str, **_: object) -> None:
        prompts.append(prompt)
        if prompt == "first":
            first_started.set()
            await gate.wait()

    monkeypatch.setattr("balam.bot.stream_reply", fake_stream_reply)

    router = _router()
    message = _text_msg(SUPERGROUP, 5, "first")
    update, context, turns = _message_env(message, _FakeBot(), router=router)

    await _handle_message(update, context)
    await asyncio.wait_for(first_started.wait(), 1)
    first_task = turns.get(SUPERGROUP, 5).task

    message.text = "second"
    await _handle_message(update, context)
    message.text = "third"
    await _handle_message(update, context)
    assert turns.queue_len(SUPERGROUP, 5) == 2

    gate.set()
    await first_task
    while (turn := turns.get(SUPERGROUP, 5)) is not None:
        await turn.task  # each finished turn hands the slot to the next
    assert prompts == ["first", "second", "third"]


async def test_queued_message_reads_plan_mode_at_drain_time(monkeypatch) -> None:
    # The plan agent must be derived when a job actually runs, not when it was
    # enqueued: a message queued during a planning turn would otherwise drag the
    # session back into plan mode after the plan was approved mid-turn.
    agents: list[tuple[str, object]] = []
    gate = asyncio.Event()
    first_started = asyncio.Event()

    async def fake_stream_reply(*, prompt: str, plan_mode: bool = False, **_: object) -> None:
        agents.append((prompt, plan_mode))
        if prompt == "first":
            first_started.set()
            await gate.wait()

    monkeypatch.setattr("balam.bot.stream_reply", fake_stream_reply)

    router = _router()
    router.set_plan_mode(SUPERGROUP, 5, True)
    message = _text_msg(SUPERGROUP, 5, "first")
    update, context, turns = _message_env(message, _FakeBot(), router=router)

    await _handle_message(update, context)
    await asyncio.wait_for(first_started.wait(), 1)
    first_task = turns.get(SUPERGROUP, 5).task

    # Queue a second message mid-turn, then clear the flag (what on_plan_approved
    # does when the plan_exit question is answered "Yes") before the queue drains.
    message.text = "second"
    await _handle_message(update, context)
    router.set_plan_mode(SUPERGROUP, 5, False)

    gate.set()
    await first_task
    while (turn := turns.get(SUPERGROUP, 5)) is not None:
        await turn.task

    assert agents == [("first", True), ("second", False)]


async def test_cancel_drops_queued_messages(monkeypatch) -> None:
    gate = asyncio.Event()
    first_started = asyncio.Event()

    async def fake_stream_reply(*, prompt: str, **_: object) -> None:
        if prompt == "first":
            first_started.set()
            await gate.wait()

    monkeypatch.setattr("balam.bot.stream_reply", fake_stream_reply)

    router = _router()
    message = _text_msg(SUPERGROUP, 5, "first")
    update, context, turns = _message_env(message, _FakeBot(), router=router)

    await _handle_message(update, context)
    await asyncio.wait_for(first_started.wait(), 1)
    first_task = turns.get(SUPERGROUP, 5).task
    message.text = "second"
    await _handle_message(update, context)
    assert turns.queue_len(SUPERGROUP, 5) == 1

    # /cancel stops the running turn AND clears anything queued behind it.
    await _handle_cancel(update, context)
    await asyncio.sleep(0)  # let the cancellation propagate
    with contextlib.suppress(asyncio.CancelledError):
        await first_task

    assert turns.queue_len(SUPERGROUP, 5) == 0
    assert turns.get(SUPERGROUP, 5) is None  # nothing drained after cancel
    assert any("queued" in r.lower() for r in message.replies)


async def test_status_reports_queue_depth() -> None:
    message = _FakeMessage(SUPERGROUP, thread_id=5)
    update, context, _router, _opencode, turns = _session_cmd_env(message)
    task = _sleeping_turn(turns, SUPERGROUP, 5, "ses_running")
    turns.enqueue(SUPERGROUP, 5, _job("queued one"))
    turns.enqueue(SUPERGROUP, 5, _job("queued two"))

    await _handle_status(update, context)

    assert "Queued: 2" in message.replies[-1]
    task.cancel()


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
    return build_application(_config(chat_id=chat_id), backend=None, router=None)


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
    assert {"new", "rename", "status", "model", "effort", "cancel", "context"} <= names


# --- approval inline-keyboard callback ----------------------------------------


class _FakeCBMessage:
    def __init__(self, chat_id: int = SUPERGROUP, thread_id: int | None = None) -> None:
        self.text = "🔐 Allow Edit?"
        self.text_markdown_v2 = "🔐 Allow Edit?"
        self.chat = SimpleNamespace(id=chat_id)
        self.message_thread_id = thread_id
        self.edited: list[str] = []
        self.reply_markups: list[object] = []
        self.deleted = 0

    async def edit_text(self, *, text: str, reply_markup=None, **_: object) -> None:
        self.edited.append(text)
        self.reply_markups.append(reply_markup)

    async def edit_reply_markup(self, *, reply_markup=None, **_: object) -> None:
        self.reply_markups.append(reply_markup)

    async def delete(self) -> None:
        self.deleted += 1


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
        application=SimpleNamespace(
            bot_data={
                "config": config,
                "pending": pending,
                "background_tasks": set(),
                "approval_delete_delay_s": 0,
            }
        )
    )
    return update, context


def _question_callback_env(
    query: _FakeQuery, pending_questions: PendingQuestions, *, chat_id: int | None = None
):
    config = SimpleNamespace(allowed_telegram_user_id=OWNER, allowed_telegram_chat_id=chat_id)
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"config": config, "pending_questions": pending_questions}
        )
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
    assert "Approved" in query.message.edited[-1]


async def test_approval_callback_owner_allow_deletes_prompt_after_edit() -> None:
    pending = PendingApprovals()
    token, future = pending.register("ses_x")
    message = _FakeCBMessage()
    query = _FakeQuery(f"appr:allow:{token}", OWNER, message)
    update, context = _callback_env(query, pending)

    await _handle_approval_callback(update, context)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert future.result() is Choice.ALLOW
    assert message.edited and "Approved" in message.edited[-1]
    assert message.deleted == 1


async def test_approval_callback_all_sets_accept_all_edits() -> None:
    pending = PendingApprovals()
    token, future = pending.register("ses_y")
    query = _FakeQuery(f"appr:all:{token}", OWNER, _FakeCBMessage())
    update, context = _callback_env(query, pending)

    await _handle_approval_callback(update, context)

    assert future.result() is Choice.ALL
    assert pending.is_accept_all_edits("ses_y") is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_approval_callback_deny_does_not_delete_prompt() -> None:
    pending = PendingApprovals()
    token, future = pending.register("ses_x")
    message = _FakeCBMessage()
    query = _FakeQuery(f"appr:deny:{token}", OWNER, message)
    update, context = _callback_env(query, pending)

    await _handle_approval_callback(update, context)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert future.result() is Choice.DENY
    assert message.edited and "Denied" in message.edited[-1]
    assert message.deleted == 0


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


# --- question custom-answer callback ------------------------------------------


def _button_texts(markup: object) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


async def test_question_callback_single_select_resolves_and_clears_keyboard() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Coffee", "Tea"]], chat_id=SUPERGROUP, thread_id=7
    )
    query = _FakeQuery(f"qst:{token}:0:1", OWNER, _FakeCBMessage(thread_id=7))
    update, context = _question_callback_env(query, pending_questions)

    await _handle_question_callback(update, context)

    assert futures[0].result() == ["Tea"]
    assert query.message.edited
    assert query.message.reply_markups[-1] is None


async def test_question_callback_multi_select_toggles_without_resolving() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Coffee", "Tea"]], multiples=[True], chat_id=SUPERGROUP, thread_id=7
    )
    query = _FakeQuery(f"qst:{token}:0:1", OWNER, _FakeCBMessage(thread_id=7))
    update, context = _question_callback_env(query, pending_questions)

    await _handle_question_callback(update, context)

    assert not futures[0].done()
    assert query.answers == ["Selected."]
    assert _button_texts(query.message.reply_markups[-1]) == [
        "☐ Coffee",
        "☑ Tea",
        "Done",
        "Type your own answer",
    ]


async def test_question_callback_multi_select_can_unselect() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Coffee", "Tea"]], multiples=[True], chat_id=SUPERGROUP, thread_id=7
    )
    message = _FakeCBMessage(thread_id=7)
    query = _FakeQuery(f"qst:{token}:0:1", OWNER, message)
    update, context = _question_callback_env(query, pending_questions)

    await _handle_question_callback(update, context)
    await _handle_question_callback(update, context)

    assert not futures[0].done()
    assert query.answers == ["Selected.", "Unselected."]
    assert _button_texts(message.reply_markups[-1]) == [
        "☐ Coffee",
        "☐ Tea",
        "Done",
        "Type your own answer",
    ]


async def test_question_done_callback_requires_selection() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Coffee", "Tea"]], multiples=[True], chat_id=SUPERGROUP, thread_id=7
    )
    query = _FakeQuery(f"qstd:{token}:0", OWNER, _FakeCBMessage(thread_id=7))
    update, context = _question_callback_env(query, pending_questions)

    await _handle_question_done_callback(update, context)

    assert not futures[0].done()
    assert query.answers == ["Select at least one option."]


async def test_question_done_callback_resolves_multi_select() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x",
        [["Coffee", "Tea", "Water"]],
        multiples=[True],
        chat_id=SUPERGROUP,
        thread_id=7,
    )
    assert pending_questions.toggle(token, 0, 0) is True
    assert pending_questions.toggle(token, 0, 2) is True
    query = _FakeQuery(f"qstd:{token}:0", OWNER, _FakeCBMessage(thread_id=7))
    update, context = _question_callback_env(query, pending_questions)

    await _handle_question_done_callback(update, context)

    assert futures[0].result() == ["Coffee", "Water"]
    assert query.message.reply_markups[-1] is None


async def test_question_done_callback_resolves_multi_select_with_custom_answer() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x",
        [["Coffee", "Tea", "Water"]],
        multiples=[True],
        chat_id=SUPERGROUP,
        thread_id=7,
    )
    assert pending_questions.toggle(token, 0, 1) is True
    assert pending_questions.await_custom(token, 0, SUPERGROUP, 7) is True
    assert pending_questions.resolve_custom(SUPERGROUP, 7, "kombucha").status == "added"
    query = _FakeQuery(f"qstd:{token}:0", OWNER, _FakeCBMessage(thread_id=7))
    update, context = _question_callback_env(query, pending_questions)

    await _handle_question_done_callback(update, context)

    assert futures[0].result() == ["Tea", "kombucha"]


async def test_question_done_callback_allows_custom_only_multi_select() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Coffee", "Tea"]], multiples=[True], chat_id=SUPERGROUP, thread_id=7
    )
    assert pending_questions.await_custom(token, 0, SUPERGROUP, 7) is True
    assert pending_questions.resolve_custom(SUPERGROUP, 7, "sparkling water").status == "added"
    query = _FakeQuery(f"qstd:{token}:0", OWNER, _FakeCBMessage(thread_id=7))
    update, context = _question_callback_env(query, pending_questions)

    await _handle_question_done_callback(update, context)

    assert futures[0].result() == ["sparkling water"]


async def test_question_custom_callback_arms_next_topic_message() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Preset"]], chat_id=SUPERGROUP, thread_id=7
    )
    query = _FakeQuery(f"qstc:{token}:0", OWNER, _FakeCBMessage(thread_id=7))
    update, context = _question_callback_env(query, pending_questions)

    await _handle_question_custom_callback(update, context)

    assert not futures[0].done()
    assert query.message.edited
    assert any("next message" in (answer or "") for answer in query.answers)
    assert pending_questions.resolve_custom(SUPERGROUP, 7, "typed answer").status == "resolved"
    assert futures[0].result() == ["typed answer"]


async def test_question_custom_callback_multi_select_keeps_keyboard_visible() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Preset"]], multiples=[True], chat_id=SUPERGROUP, thread_id=7
    )
    message = _FakeCBMessage(thread_id=7)
    query = _FakeQuery(f"qstc:{token}:0", OWNER, message)
    update, context = _question_callback_env(query, pending_questions)

    await _handle_question_custom_callback(update, context)

    assert not futures[0].done()
    assert not message.edited
    assert query.answers == ["Send your custom answer, then tap Done."]
    assert pending_questions.resolve_custom(SUPERGROUP, 7, "typed answer").status == "added"
    assert not futures[0].done()


async def test_message_resolves_pending_custom_answer_without_starting_turn() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Preset"]], chat_id=SUPERGROUP, thread_id=5
    )
    assert pending_questions.await_custom(token, 0, SUPERGROUP, 5)

    replies: list[str] = []
    message = SimpleNamespace(
        chat_id=SUPERGROUP,
        message_thread_id=5,
        text="my typed answer",
        caption=None,
        reply_text=lambda text: replies.append(text),
    )

    async def reply_text(text: str) -> None:
        replies.append(text)

    message.reply_text = reply_text
    update, context, turns = _message_env(message, SimpleNamespace())
    context.application.bot_data["pending_questions"] = pending_questions

    await _handle_message(update, context)

    assert futures[0].result() == ["my typed answer"]
    assert replies == ["✅ Answer sent."]
    assert turns.get(SUPERGROUP, 5) is None


async def test_message_shows_typed_answer_on_the_original_question_message() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Preset"]], chat_id=SUPERGROUP, thread_id=5
    )
    pending_questions.set_message(token, 0, 424242, "❓ *Snack*\nPick one\\.")
    assert pending_questions.await_custom(token, 0, SUPERGROUP, 5)

    edits: list[dict[str, object]] = []

    async def edit_message_text(**kwargs: object) -> None:
        edits.append(kwargs)

    async def reply_text(text: str) -> None:
        pass

    bot = SimpleNamespace(edit_message_text=edit_message_text)
    message = SimpleNamespace(
        chat_id=SUPERGROUP,
        message_thread_id=5,
        text="kombucha (please)",
        caption=None,
        reply_text=reply_text,
    )
    update, context, turns = _message_env(message, bot)
    context.application.bot_data["pending_questions"] = pending_questions

    await _handle_message(update, context)

    assert futures[0].result() == ["kombucha (please)"]
    # The original question message is edited to show the answer (its "Reply with
    # your answer" note replaced) with the keyboard stripped and the answer escaped.
    assert len(edits) == 1
    edit = edits[0]
    assert edit["message_id"] == 424242
    assert edit["reply_markup"] is None
    assert "❓ *Snack*" in edit["text"]
    assert "✅ *Answered:* kombucha \\(please\\)" in edit["text"]


async def test_message_adds_pending_multi_select_custom_answer_without_starting_turn() -> None:
    pending_questions = PendingQuestions()
    token, futures = pending_questions.register(
        "ses_x", [["Preset"]], multiples=[True], chat_id=SUPERGROUP, thread_id=5
    )
    assert pending_questions.await_custom(token, 0, SUPERGROUP, 5)

    replies: list[str] = []

    async def reply_text(text: str) -> None:
        replies.append(text)

    message = SimpleNamespace(
        chat_id=SUPERGROUP,
        message_thread_id=5,
        text="my typed answer",
        caption=None,
        reply_text=reply_text,
    )
    update, context, turns = _message_env(message, SimpleNamespace())
    context.application.bot_data["pending_questions"] = pending_questions

    await _handle_message(update, context)

    assert not futures[0].done()
    assert pending_questions.finish_multi(token, 0) is True
    assert futures[0].result() == ["my typed answer"]
    assert replies == ["✅ Custom answer added. Select more options or tap Done."]
    assert turns.get(SUPERGROUP, 5) is None


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
                "backend": OpenCodeBackend(opencode),
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


class _DeleteBot(_FakeBot):
    """A bot whose ``delete_forum_topic`` rejects the given thread ids the way the
    Telegram API does for a topic that no longer exists."""

    def __init__(self, *, stale: set[int]) -> None:
        super().__init__()
        self._stale = stale

    async def delete_forum_topic(self, *, chat_id: int, message_thread_id: int) -> None:
        if message_thread_id in self._stale:
            raise BadRequest("Topic_id_invalid")
        await super().delete_forum_topic(chat_id=chat_id, message_thread_id=message_thread_id)


def _delete_callback_env(query: _FakeQuery, pending_deletions: PendingDeletions, bot: _FakeBot):
    config = SimpleNamespace(allowed_telegram_user_id=OWNER, allowed_telegram_chat_id=None)
    router = _router()
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "config": config,
                "pending_deletions": pending_deletions,
                "router": router,
            }
        ),
        bot=bot,
    )
    return update, context, router


async def test_delete_confirm_purges_topics_already_gone_from_telegram() -> None:
    # A topic deleted straight from the Telegram UI leaves a stale local row
    # (Telegram sends no delete update). Re-deleting it via /delete used to be a
    # permanent failure — the API answers TOPIC_ID_INVALID and the row was never
    # purged, so it kept reappearing in the picker, un-clearable. Now the stale
    # row is purged and the deletion counts as success.
    pending = PendingDeletions()
    token = pending.register(SUPERGROUP, [(101, "live"), (202, "stale")])
    assert pending.toggle(token, 101) is True
    assert pending.toggle(token, 202) is True

    bot = _DeleteBot(stale={202})
    message = _FakeCBMessage()
    query = _FakeQuery(f"deld:{token}", OWNER, message)
    update, context, router = _delete_callback_env(query, pending, bot)
    # Seed both rows so we can assert the stale one is purged.
    router._store.set(SUPERGROUP, 101, "ses_live", 1)
    router._store.set(SUPERGROUP, 202, "ses_stale", 2)

    await _handle_delete_confirm_callback(update, context)

    # The live topic was deleted via the API; the stale one was not re-attempted
    # past its rejection — but both local rows are gone.
    assert bot.deleted_topics == [(SUPERGROUP, 101)]
    assert router._store.get_row(SUPERGROUP, 101) is None
    assert router._store.get_row(SUPERGROUP, 202) is None
    # Both count as removed; nothing is reported as un-deletable.
    assert message.edited and message.edited[-1] == "🗑 Deleted 2 topic(s)."


def _topics(n: int) -> list[tuple[int, str]]:
    return [(i, f"t{i}") for i in range(1, n + 1)]


def test_delete_picker_pages_the_snapshot() -> None:
    # The picker windows the full snapshot PAGE_SIZE topics at a time.
    pending = PendingDeletions()
    size = PendingDeletions.PAGE_SIZE
    token = pending.register(SUPERGROUP, _topics(size * 2 + 1))

    page, page_count, total, selected = pending.page_info(token)
    assert (page, page_count, total, selected) == (0, 3, size * 2 + 1, 0)
    # First page shows the first window only.
    assert [tid for tid, _, _ in pending.entries(token)] == list(range(1, size + 1))

    assert pending.set_page(token, 2) == 2
    # Last page holds the remainder.
    assert [tid for tid, _, _ in pending.entries(token)] == [size * 2 + 1]
    # set_page clamps out-of-range requests instead of going blank.
    assert pending.set_page(token, 99) == 2
    assert pending.set_page(token, -5) == 0


def test_delete_picker_selection_persists_across_pages() -> None:
    # A topic checked on one page stays selected after paging away and is included
    # at confirm — the whole point of real pagination over the old cap.
    pending = PendingDeletions()
    size = PendingDeletions.PAGE_SIZE
    token = pending.register(SUPERGROUP, _topics(size + 2))

    assert pending.toggle(token, 1) is True  # page 0
    pending.set_page(token, 1)
    assert pending.toggle(token, size + 2) is True  # page 1
    # Selection count spans the snapshot, not just the visible page.
    assert pending.page_info(token)[3] == 2
    assert pending.selected_thread_ids(token) == [1, size + 2]


def test_delete_picker_expired_token_is_inert() -> None:
    pending = PendingDeletions()
    assert pending.entries("nope") is None
    assert pending.page_info("nope") is None
    assert pending.set_page("nope", 0) is None


async def test_delete_page_callback_flips_page_keeping_selection() -> None:
    pending = PendingDeletions()
    size = PendingDeletions.PAGE_SIZE
    token = pending.register(SUPERGROUP, _topics(size + 1))
    assert pending.toggle(token, 1) is True

    message = _FakeCBMessage()
    query = _FakeQuery(f"delp:{token}:1", OWNER, message)
    update, context, _ = _delete_callback_env(query, pending, _FakeBot())

    await _handle_delete_page_callback(update, context)

    # The picker advanced and re-rendered; the selection survived the page flip.
    assert pending.page_info(token)[0] == 1
    assert pending.selected_thread_ids(token) == [1]
    assert message.reply_markups  # keyboard was refreshed


async def test_delete_page_callback_on_expired_token_clears_keyboard() -> None:
    pending = PendingDeletions()
    message = _FakeCBMessage()
    query = _FakeQuery("delp:gone:1", OWNER, message)
    update, context, _ = _delete_callback_env(query, pending, _FakeBot())

    await _handle_delete_page_callback(update, context)

    assert query.answers and query.answers[-1] == "This picker has expired."
