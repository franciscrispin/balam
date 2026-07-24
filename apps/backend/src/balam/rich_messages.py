"""Bot API 10.1 Rich Messages — send GFM to Telegram without MarkdownV2 escaping.

Telegram parses GitHub-Flavored Markdown natively via ``sendRichMessage`` and
``InputRichMessage.markdown`` (Bot API 10.1, 2026-06-11), so the agent's output
goes over the wire as-is instead of through the escaping pass in
:mod:`balam.markdown`. Rich messages also carry structure MarkdownV2 has no way
to express — tables, headings, task lists, ``<details>`` collapsibles — and lift
the length cap from 4096 to 32768 characters.

python-telegram-bot does not wrap these methods: upstream paused Bot API 10.1
work on 2026-06-18 pending an internal refactor and closed the community PRs, so
support is queued for PTB v23 (issue #5261). Until then we call the endpoints
through :meth:`telegram.Bot.do_api_request`, which exists for exactly this and
still runs the request through PTB's transport, rate limiter and retry handling.

Every entry point falls back to the MarkdownV2 path on failure, so a payload
Telegram rejects (``RICH_MESSAGE_EMPTY``) degrades to the old rendering rather
than dropping the message.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

from telegram.error import RetryAfter
from telegram.warnings import PTBUserWarning

from balam.markdown import gfm_to_telegram, split_message

logger = logging.getLogger(__name__)

# PTB *does* wrap editMessageText, so it nudges callers away from
# do_api_request — but its wrapper predates Bot API 10.1 and has no way to pass
# ``rich_message`` (and requires ``text``, which 10.1 makes optional). The raw
# call is the only route, so drop the nudge rather than log it once per edit.
warnings.filterwarnings(
    "ignore",
    message=r'.*do_api_request\("editMessageText".*',
    category=PTBUserWarning,
)

#: Telegram's cap on rich message text (Bot API 10.1, "Rich Message Limits").
RICH_MAX_LENGTH = 32768


def chunk_rich(text: str) -> list[str]:
    """Split GFM into ≤:data:`RICH_MAX_LENGTH` chunks, code-block-aware.

    A :data:`balam.streamer.Renderer` for rich mode: the transport wants raw GFM,
    so unlike :func:`balam.markdown.gfm_to_telegram` this only enforces the
    length cap. At 32768 characters an agent reply is virtually always one chunk.
    """
    return split_message(text, RICH_MAX_LENGTH)


def _rich_payload(markdown: str) -> dict[str, Any]:
    # skip_entity_detection stops Telegram from auto-linkifying bare URLs, @names
    # and #tags inside agent output (code identifiers turn into stray links).
    return {"markdown": markdown, "skip_entity_detection": True}


async def send_rich_message(
    bot: Any,
    *,
    chat_id: int,
    markdown: str,
    thread_kwargs: dict[str, Any] | None = None,
    reply_markup: Any = None,
) -> int | None:
    """Send ``markdown`` as a rich message; return its message id.

    Raises on flood control (the caller's rate limiter already retried) and
    returns ``None`` if the response carries no message id.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": _rich_payload(markdown),
        **(thread_kwargs or {}),
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = await bot.do_api_request("sendRichMessage", api_kwargs=payload)
    if isinstance(result, dict):
        message_id = result.get("message_id")
        return int(message_id) if message_id is not None else None
    return getattr(result, "message_id", None)


async def edit_rich_message(
    bot: Any,
    *,
    chat_id: int,
    message_id: int,
    markdown: str,
) -> None:
    """Replace a message's content with rich ``markdown`` (``editMessageText``).

    "message is not modified" is benign — an identical render — and swallowed.
    """
    try:
        await bot.do_api_request(
            "editMessageText",
            api_kwargs={
                "chat_id": chat_id,
                "message_id": message_id,
                "rich_message": _rich_payload(markdown),
            },
        )
    except RetryAfter:
        raise
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        raise


async def send_rich_draft(
    bot: Any,
    *,
    chat_id: int,
    draft_id: int,
    markdown: str,
    thread_kwargs: dict[str, Any] | None = None,
) -> None:
    """Stream a partial rich message (``sendRichMessageDraft``).

    Private chats only — a forum supergroup rejects this with
    ``TEXTDRAFT_PEER_INVALID``, which the caller treats as "switch to live-edit
    streaming" exactly as it does for plain ``sendMessageDraft``.
    """
    await bot.do_api_request(
        "sendRichMessageDraft",
        api_kwargs={
            "chat_id": chat_id,
            "draft_id": draft_id,
            "rich_message": _rich_payload(markdown),
            **(thread_kwargs or {}),
        },
    )


def markdown_v2_fallback(markdown: str) -> list[str]:
    """Render GFM the old way, for when Telegram rejects the rich payload."""
    return gfm_to_telegram(markdown)
