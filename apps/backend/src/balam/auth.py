"""The trust boundary (ADR-0008): who is allowed to drive this bot.

Balam runs one agent with real filesystem and shell access on the owner's
machine, so "is this really someone I let in?" is the single check everything
else rests on. The allowlist is normally just the owner, and may name a few more
people who share the owner's workspace supergroup — they are inside the *same*
boundary, not a lesser one (see ``Config.allowed_user_ids``).

It lives in its own module because both halves of the bot need it and neither
should import the other: message handlers get it declaratively through
``filters.User`` in ``build_application``, while callback queries carry no
handler filter at all and must ask :func:`callback_authorized` themselves.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from balam.config import Config


def is_allowed_user(from_id: int | None, allowed_user_ids: Collection[int]) -> bool:
    """The allowlist check, isolated for testing (ADR-0008)."""
    return from_id is not None and from_id in allowed_user_ids


def callback_authorized(query: Any, config: Config) -> bool:
    """Re-check the trust boundary (ADR-0008) for a callback: an allowed user id,
    plus the configured chat when set. Callbacks carry no handler filter, so each
    must verify the sender itself."""
    user = query.from_user
    if user is None or not is_allowed_user(user.id, config.allowed_user_ids):
        return False
    if config.allowed_telegram_chat_id is not None:
        chat = query.message.chat if query.message else None
        if chat is None or chat.id != config.allowed_telegram_chat_id:
            return False
    return True
