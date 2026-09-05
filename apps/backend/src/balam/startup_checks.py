"""Boot-time Telegram sanity checks (ADR-0008/0010).

Two misconfigurations make a bot that looks alive ignore every plain message in
its forum topics, and Telegram reports neither: group privacy mode still on in
BotFather (the bot then receives only commands, @mentions and replies to its own
messages), and an ``ALLOWED_TELEGRAM_CHAT_ID`` that is not the current id of
the supergroup — the classic case being a basic group that gained Topics, and
with them a new id. :mod:`balam.config` refuses the *shape* of a wrong id; this
module asks Telegram about the rest once, at startup, and hands ``app.py`` one
warning per finding to log.

Warnings, not errors: a bot that is a group admin hears everything regardless
of privacy mode, and a transient API failure at boot must not keep the bot
down. The point is a loud line in the journal where there used to be silence.
"""

from __future__ import annotations

from typing import Any

from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

from balam.config import Config

#: Where the fixes are written up; every warning points there.
_DOCS = 'deploy/README.md, "Telegram: the bot and the group"'


async def check_telegram_setup(bot: Any, config: Config) -> list[str]:
    """Return one human-readable warning per detected problem (empty when fine).

    Only meaningful with a target chat: in the legacy owner-anywhere DM mode
    there is no group whose privacy mode or id could matter, so nothing is
    asked and nothing is returned.
    """
    chat_id = config.allowed_telegram_chat_id
    if chat_id is None:
        return []

    try:
        me = await bot.get_me()
    except TelegramError as exc:
        return [f"getMe failed ({exc}); skipping the Telegram setup checks"]

    try:
        chat = await bot.get_chat(chat_id)
    except TelegramError as exc:
        return [
            f"cannot see chat {chat_id} ({exc}). Is ALLOWED_TELEGRAM_CHAT_ID the current "
            f"-100… id of the forum supergroup, and is @{me.username} a member of it? Until "
            f"then the bot ignores every message. {_DOCS}."
        ]

    warnings: list[str] = []
    if chat.type != ChatType.SUPERGROUP:
        warnings.append(
            f"chat {chat_id} is a {chat.type}, not a forum supergroup; Balam addresses forum "
            f"topics. {_DOCS}."
        )
    elif not chat.is_forum:
        warnings.append(
            f"chat {chat_id} ({chat.title!r}) has Topics switched off; Balam addresses forum "
            f"topics, so enable Topics in the group settings. {_DOCS}."
        )

    if not me.can_read_all_group_messages and not await _is_admin(bot, chat_id, me.id):
        warnings.append(
            f"group privacy mode is ON for @{me.username}: plain messages in chat {chat_id} "
            "never reach the bot (only commands, @mentions and replies do), and Telegram logs "
            "nothing. In BotFather: /setprivacy → Disable, then remove and re-add the bot to "
            f"the group (or make it a group admin). {_DOCS}."
        )
    return warnings


async def _is_admin(bot: Any, chat_id: int, user_id: int) -> bool:
    """Whether the bot administers ``chat_id`` — admins receive every message,
    privacy mode or not. Unknown counts as ``False``: better a spurious warning
    than a silent one."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
