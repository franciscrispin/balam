"""Small Telegram addressing helpers shared by the bot and the streamer."""

from __future__ import annotations

from typing import Any


def thread_kwargs(thread_id: int | None) -> dict[str, Any]:
    """Bot-API kwargs that route a send to a forum topic.

    The General topic carries no ``message_thread_id`` (it is ``None``); for it we
    pass nothing so the send lands in the chat root rather than a nonexistent
    thread. Used for drafts, final messages, chat actions, and error notices so
    they all reach the same topic.
    """
    return {} if thread_id is None else {"message_thread_id": thread_id}
