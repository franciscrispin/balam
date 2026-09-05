"""Boot-time Telegram warnings for the two setups that otherwise fail silently."""

from __future__ import annotations

from types import SimpleNamespace

from telegram.error import BadRequest, NetworkError

from balam.startup_checks import check_telegram_setup

CHAT = -1001234567890


def _config(chat_id: int | None = CHAT) -> SimpleNamespace:
    return SimpleNamespace(allowed_telegram_chat_id=chat_id)


class _FakeBot:
    def __init__(
        self,
        *,
        privacy_on: bool = False,
        status: str = "member",
        chat: SimpleNamespace | None = None,
        chat_error: Exception | None = None,
    ) -> None:
        self._me = SimpleNamespace(
            id=7, username="balam_bot", can_read_all_group_messages=not privacy_on
        )
        self._status = status
        self._chat = chat or SimpleNamespace(type="supergroup", is_forum=True, title="workspace")
        self._chat_error = chat_error
        self.calls: list[str] = []

    async def get_me(self) -> SimpleNamespace:
        self.calls.append("get_me")
        return self._me

    async def get_chat(self, chat_id: int) -> SimpleNamespace:
        self.calls.append("get_chat")
        if self._chat_error is not None:
            raise self._chat_error
        return self._chat

    async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
        self.calls.append("get_chat_member")
        return SimpleNamespace(status=self._status)


async def test_no_target_chat_asks_nothing() -> None:
    # Legacy owner-anywhere DM mode: no group, nothing to check.
    bot = _FakeBot(privacy_on=True)
    assert await check_telegram_setup(bot, _config(None)) == []
    assert bot.calls == []


async def test_healthy_setup_is_quiet() -> None:
    assert await check_telegram_setup(_FakeBot(), _config()) == []


async def test_privacy_mode_on_warns_with_the_fix() -> None:
    warnings = await check_telegram_setup(_FakeBot(privacy_on=True), _config())
    assert len(warnings) == 1
    assert "privacy mode is ON for @balam_bot" in warnings[0]
    assert "/setprivacy" in warnings[0]
    assert str(CHAT) in warnings[0]


async def test_privacy_mode_on_is_fine_for_an_admin_bot() -> None:
    # Admins receive every group message regardless of the BotFather setting.
    for status in ("administrator", "creator"):
        bot = _FakeBot(privacy_on=True, status=status)
        assert await check_telegram_setup(bot, _config()) == []


async def test_unreachable_chat_points_at_the_id() -> None:
    # The stale pre-Topics id, or the bot not being in the group: either way
    # Telegram cannot show the bot the chat, and every update would be dropped.
    bot = _FakeBot(chat_error=BadRequest("Chat not found"))
    warnings = await check_telegram_setup(bot, _config())
    assert len(warnings) == 1
    assert "ALLOWED_TELEGRAM_CHAT_ID" in warnings[0]
    assert "Chat not found" in warnings[0]
    # No point asking about membership in a chat we cannot see.
    assert "get_chat_member" not in bot.calls


async def test_topics_off_warns() -> None:
    chat = SimpleNamespace(type="supergroup", is_forum=False, title="workspace")
    warnings = await check_telegram_setup(_FakeBot(chat=chat), _config())
    assert len(warnings) == 1
    assert "Topics switched off" in warnings[0]


async def test_not_a_supergroup_warns() -> None:
    chat = SimpleNamespace(type="channel", is_forum=None, title="news")
    warnings = await check_telegram_setup(_FakeBot(chat=chat), _config())
    assert len(warnings) == 1
    assert "not a forum supergroup" in warnings[0]


async def test_api_failure_is_a_warning_not_a_crash() -> None:
    class _Down(_FakeBot):
        async def get_me(self) -> SimpleNamespace:
            raise NetworkError("boom")

    warnings = await check_telegram_setup(_Down(), _config())
    assert len(warnings) == 1
    assert "getMe failed" in warnings[0]
