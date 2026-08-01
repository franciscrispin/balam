"""The trust boundary (ADR-0008): who is allowed to drive this bot.

Balam runs one agent with real filesystem and shell access on the owner's
machine, so "is this really the owner?" is the single check everything else
rests on. It lives in its own module because both halves of the bot need it and
neither should import the other: message handlers get it declaratively through
``filters.User`` in ``build_application``, while callback queries carry no
handler filter at all and must ask :func:`callback_authorized` themselves.
"""

from __future__ import annotations

from typing import Any

from balam.config import Config


def is_owner(from_id: int | None, allowed_user_id: int) -> bool:
    """The allowlist check, isolated for testing (ADR-0008)."""
    return from_id is not None and from_id == allowed_user_id


def callback_authorized(query: Any, config: Config) -> bool:
    """Re-check the trust boundary (ADR-0008) for a callback: owner id, plus the
    configured chat when set. Callbacks carry no handler filter, so each must
    verify the sender itself."""
    user = query.from_user
    if user is None or not is_owner(user.id, config.allowed_telegram_user_id):
        return False
    if config.allowed_telegram_chat_id is not None:
        chat = query.message.chat if query.message else None
        if chat is None or chat.id != config.allowed_telegram_chat_id:
            return False
    return True
