"""Stream an OpenCode reply into a Telegram topic using native message drafts.

Telegram's ``sendMessageDraft`` streams partial text without flicker (ADR-0010)
and works inside forum topics when ``message_thread_id`` is passed. This follows
the proven approach in ~/projects/zog (``src/zog/stream.py``):

  1. Accumulate assistant text as it streams; mark the draft dirty.
  2. A background loop flushes a draft every ~0.5s via ``send_message_draft``,
     reusing one ``draft_id`` so Telegram *animates* the updates.
  3. If a draft call fails, disable drafts and degrade gracefully — the final
     real message still goes out.
  4. On turn completion, send the real message(s) via ``send_message``. Both the
     draft and the final message render GFM as Telegram MarkdownV2 (ADR-0010),
     split into ≤4096-char chunks.

The transport-agnostic :class:`DraftSession` is unit-tested with a fake.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, Protocol

from balam.markdown import gfm_to_telegram
from balam.opencode import OpenCode

logger = logging.getLogger(__name__)

#: How often the background loop pushes a draft update (seconds), matching zog.
DRAFT_INTERVAL_S = 0.5

Renderer = Callable[[str], list[str]]


class DraftTransport(Protocol):
    """Where draft previews and final messages land."""

    async def send_draft(self, draft_id: int, text: str) -> None: ...
    async def send_message(self, text: str) -> None: ...


class DraftSession:
    """Tracks the in-progress draft for one streamed reply: accumulates text,
    flushes it as an animated draft, and finalizes into real message(s).

    Mirrors zog's ``_DraftState`` + ``_flush_draft`` + finalize flow.
    """

    def __init__(
        self,
        transport: DraftTransport,
        *,
        draft_id: int | None = None,
        render: Renderer = gfm_to_telegram,
    ) -> None:
        self._transport = transport
        # draft_id must be non-zero and stable for the segment (animates on change).
        self._draft_id = draft_id if draft_id is not None else random.randint(1, 2**31)
        self._render = render
        self._raw = ""
        self._dirty = False
        self._disabled = False

    @property
    def text(self) -> str:
        return self._raw

    @property
    def drafts_disabled(self) -> bool:
        return self._disabled

    def set_text(self, text: str) -> None:
        """Replace the accumulated text; marks dirty if it changed."""
        if text != self._raw:
            self._raw = text
            self._dirty = True

    async def flush_draft(self) -> None:
        """Flush the current text as a draft preview, if dirty and not disabled.

        Only the first chunk is previewed (full content is split at finalize). A
        failed draft call disables further previews — it never tears down the
        stream.
        """
        if self._disabled or not self._dirty:
            return
        chunks = self._render(self._raw)
        if not chunks:
            return
        try:
            await self._transport.send_draft(self._draft_id, chunks[0])
            self._dirty = False
        except Exception:
            logger.debug("send_message_draft failed; disabling drafts", exc_info=True)
            self._disabled = True

    async def finalize(
        self, fallback: str = "(the agent finished without producing any text)"
    ) -> None:
        """Send the accumulated text as real message(s), split at the char cap."""
        text = self._raw if self._raw.strip() else fallback
        for chunk in self._render(text):
            await self._transport.send_message(chunk)


def _describe_error(error: Any) -> str:
    """Extract a readable line from an OpenCode session error payload."""
    if isinstance(error, dict):
        name = error.get("name")
        message = (error.get("data") or {}).get("message")
        if name and message:
            return f"{name}: {message}"
        if message:
            return message
        if name:
            return name
    return "The agent reported an error."


def _join_parts(parts: dict[str, tuple[int, str]]) -> str:
    """Concatenate the session's assistant text parts in arrival order."""
    return "".join(text for _order, text in sorted(parts.values(), key=lambda p: p[0]))


def _make_transport(bot: Any, chat_id: int, thread_id: int | None) -> DraftTransport:
    # message_thread_id routes both the draft and the final message to the topic.
    thread_kwargs: dict[str, Any] = {} if thread_id is None else {"message_thread_id": thread_id}

    class _Transport:
        async def send_draft(self, draft_id: int, text: str) -> None:
            await bot.send_message_draft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=text,
                parse_mode="MarkdownV2",
                **thread_kwargs,
            )

        async def send_message(self, text: str) -> None:
            try:
                await bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="MarkdownV2", **thread_kwargs
                )
            except Exception:
                # Malformed MarkdownV2 → resend without formatting rather than drop.
                logger.debug("MarkdownV2 send failed; falling back to plain text", exc_info=True)
                await bot.send_message(chat_id=chat_id, text=text, **thread_kwargs)

    return _Transport()


async def stream_reply(
    *,
    bot: Any,
    opencode: OpenCode,
    session_id: str,
    chat_id: int,
    thread_id: int | None,
    prompt: str,
    draft_interval: float = DRAFT_INTERVAL_S,
) -> None:
    """Prompt the agent and stream its reply into the topic.

    Subscribes to the event stream *before* prompting so no early deltas are
    missed, animates a draft as text grows, and finalizes into real message(s)
    on ``session.idle`` (or ``session.error``).
    """
    transport = _make_transport(bot, chat_id, thread_id)
    draft = DraftSession(transport)
    thread_kwargs: dict[str, Any] = {} if thread_id is None else {"message_thread_id": thread_id}

    streaming = True

    async def flush_loop() -> None:
        while streaming:
            await asyncio.sleep(draft_interval)
            if not streaming:
                break
            await draft.flush_draft()

    flush_task = asyncio.create_task(flush_loop())

    try:
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing", **thread_kwargs)
        except Exception:
            pass

        await opencode.prompt(session_id, prompt)

        assistant_message_ids: set[str] = set()
        text_parts: dict[str, tuple[int, str]] = {}
        order = 0
        error_text: str | None = None

        async for event in opencode.events():
            etype = event.get("type")
            props = event.get("properties", {})

            if etype == "message.updated":
                info = props.get("info", {})
                if info.get("sessionID") == session_id and info.get("role") == "assistant":
                    assistant_message_ids.add(info.get("id"))

            elif etype == "message.part.updated":
                part = props.get("part", {})
                if part.get("type") != "text" or part.get("sessionID") != session_id:
                    continue
                # Skip the echoed user message; only render assistant text.
                if part.get("messageID") not in assistant_message_ids:
                    continue
                part_id = part.get("id")
                if part_id in text_parts:
                    text_parts[part_id] = (text_parts[part_id][0], part.get("text", ""))
                else:
                    text_parts[part_id] = (order, part.get("text", ""))
                    order += 1
                draft.set_text(_join_parts(text_parts))

            elif etype == "session.error" and props.get("sessionID") == session_id:
                error_text = _describe_error(props.get("error"))
                break

            elif etype == "session.idle" and props.get("sessionID") == session_id:
                break

        if error_text:
            base = _join_parts(text_parts)
            prefix = f"{base}\n\n" if base.strip() else ""
            draft.set_text(f"{prefix}⚠️ {error_text}")
    finally:
        # Stop the flusher before finalizing so the draft and the real message
        # don't race.
        streaming = False
        await flush_task

    # Replace the ephemeral draft with the real, persistent message(s).
    await draft.finalize()
