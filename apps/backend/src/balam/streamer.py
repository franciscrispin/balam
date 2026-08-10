"""Stream an OpenCode reply into a Telegram topic using native message drafts.

Telegram's ``sendMessageDraft`` streams partial text without flicker (ADR-0010).
But it is **private-chat only** — its documented ``chat_id`` is "the target private
chat", and a supergroup/topic is rejected with ``Textdraft_peer_invalid``. So in the
"workspace" forum supergroup (the live deployment) we fall back to **live-edit
streaming**: send one real message and edit it in place as the text grows — the
throttled ``editMessageText`` path ADR-0010 specifies. Approach follows zog
(``src/zog/stream.py``) and open-shrimp (``stream.py``'s ``_send_live_edit``):

  1. Accumulate assistant text as it streams; mark the draft dirty.
  2. A background loop flushes every ~0.5s, reusing one ``draft_id`` so Telegram
     *animates* native drafts.
  3. The streaming approach is picked up front from the chat type: private chats
     (positive ``chat_id`` in the Bot API) use native drafts; groups/supergroups
     (negative ``chat_id``) go straight to live-edit, never burning a doomed
     ``sendMessageDraft`` call per turn. A draft failure in a private chat still
     falls back to live-edit mid-turn instead of going silent.
  4. On turn completion, send the real message(s). A live-edit message is reused
     for the first chunk (no duplicate); overflow goes to new messages. Drafts and
     final messages render GFM as Telegram MarkdownV2 (ADR-0010), ≤4096-char chunks.
  5. The answer ends the turn: it is the topic's bottom layer and always streams
     last. Whenever another message lands below the streamed answer bubble — a
     question keyboard or approval prompt mid-stream, progress overflow at
     finalize, a retry notice (Telegram cannot insert above them) — the stale
     bubble is deleted and the answer re-sent at the bottom. The same tail check
     drives this on every live-edit flush and at finalize (see
     ``DraftSession._drop_if_stale``); the reasoning/progress stream keeps its
     position (it is the layer above) and is never re-anchored.

The transport-agnostic :class:`DraftSession` is unit-tested with a fake.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, Protocol

from telegram.error import RetryAfter

from balam.agent.backend import AgentBackend, FollowUpChannel, TurnRequest
from balam.agent.events import (
    BackgroundTask,
    BackgroundTasksChanged,
    PermissionRequested,
    QuestionAsked,
    ReasoningUpdated,
    RetryNotice,
    SessionStarted,
    TextUpdated,
    ToolUpdated,
    TurnFailed,
    TurnFinished,
    TurnStepFinished,
)
from balam.approvals import (
    Choice,
    PendingApprovals,
    PendingQuestions,
    Verdict,
    decide,
    request_target_paths,
)
from balam.attachments import PromptFile
from balam.markdown import gfm_to_telegram
from balam.rich_messages import (
    chunk_rich,
    edit_rich_message,
    markdown_v2_fallback,
    send_rich_draft,
    send_rich_message,
)
from balam.stream_render import (
    GroupEntry,
    StreamPart,
    _approval_keyboard,
    _format_approval_request,
    _format_question,
    _join_stream,
    _question_keyboard,
    _render_background_notice,
    _render_todos,
    _render_tool_group,
    _render_tool_part,
)
from balam.telegram_utils import thread_kwargs
from balam.tools import Tool

logger = logging.getLogger(__name__)

#: How often the background loop pushes a draft update (seconds), matching zog.
DRAFT_INTERVAL_S = 0.5

#: Caps on inline Bash output, matching open-shrimp. Full output goes to the
#: Mini App later (Tier 2/3); for now we inline-truncate, keeping the tail.
BASH_OUTPUT_MAX_LINES = 50
BASH_OUTPUT_MAX_CHARS = 1500

Renderer = Callable[[str], list[str]]


class DraftTransport(Protocol):
    """Where draft previews and final messages land.

    ``send_message`` returns the new message's id (or ``None``) so the live-edit
    fallback can keep editing it; ``edit_message`` updates a message in place;
    ``delete_message`` removes one (used to drop a stale streamed bubble when
    the answer must be re-sent at the bottom of the topic).
    """

    async def send_draft(self, draft_id: int, text: str) -> None: ...
    async def send_message(self, text: str) -> int | None: ...
    async def edit_message(self, message_id: int, text: str) -> None: ...
    async def delete_message(self, message_id: int) -> None: ...


class DraftSession:
    """Tracks the in-progress draft for one streamed reply: accumulates text,
    flushes it as an animated draft, and finalizes into real message(s).

    Mirrors zog's ``_DraftState`` + ``_flush_draft`` + finalize flow. Native
    ``sendMessageDraft`` only works in **private chats** (Telegram rejects it
    elsewhere with ``Textdraft_peer_invalid``); ``native_drafts=False`` starts a
    group/supergroup session directly in **live-edit streaming** — send one real
    message and keep editing it in place — exactly the throttled ``editMessageText``
    fallback ADR-0010 calls for (ported from open-shrimp's ``_send_live_edit``).
    A failing draft call still flips to live-edit mid-turn as a safety net.
    """

    def __init__(
        self,
        transport: DraftTransport,
        *,
        draft_id: int | None = None,
        render: Renderer = gfm_to_telegram,
        native_drafts: bool = True,
    ) -> None:
        self._transport = transport
        # draft_id must be non-zero and stable for the segment (animates on change).
        self._draft_id = draft_id if draft_id is not None else random.randint(1, 2**31)
        self._render = render
        self._raw = ""
        self._dirty = False
        # Native drafts disabled (unsupported chat type) → use live-edit instead.
        # ``native_drafts=False`` disables them up front when the caller already
        # knows the chat can't take them (groups/supergroups).
        self._disabled = not native_drafts
        # The live-edit message reused across flushes and at finalize, and the
        # last text pushed to it (so an unchanged render is not re-sent).
        self._live_edit_message_id: int | None = None
        self._live_edit_last: str | None = None

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

    async def _drop_if_stale(self, latest_message_id: int | None) -> None:
        """Delete the streamed bubble when another message landed below it.

        ``latest_message_id`` is the id of the most recent message the turn sent
        to the topic (its "tail"). When it isn't this draft's live-edit bubble,
        something landed below the bubble — Telegram can't move it, so the bubble
        is deleted and the caller re-sends the text at the bottom. If the delete
        fails the bubble is kept and edited in place instead, so content is never
        duplicated. This is the single re-anchor primitive shared by the live-edit
        flush (mid-stream) and finalize (turn end)."""
        if (
            self._live_edit_message_id is not None
            and latest_message_id is not None
            and latest_message_id != self._live_edit_message_id
        ):
            try:
                await self._transport.delete_message(self._live_edit_message_id)
            except Exception:
                logger.debug("could not delete stale streamed bubble", exc_info=True)
            else:
                self._live_edit_message_id = None
                self._live_edit_last = None

    async def flush_draft(self, latest_message_id: int | None = None) -> None:
        """Flush the current text as a streaming preview, if dirty.

        Uses native ``sendMessageDraft`` until it fails (e.g. a group chat, which
        Telegram refuses), then switches permanently to live-edit streaming. Only
        the first chunk is previewed; the full content is split at finalize.

        ``latest_message_id`` is the topic's tail (see :meth:`_drop_if_stale`);
        pass it for a stream that must stay at the bottom (the answer) so the
        live-edit bubble re-anchors below any intervening message, and omit it for
        one that keeps its position (the progress stream)."""
        if not self._dirty:
            return
        if self._disabled:
            await self._flush_live_edit(latest_message_id)
            return
        chunks = self._render(self._raw)
        if not chunks:
            return
        try:
            await self._transport.send_draft(self._draft_id, chunks[0])
            self._dirty = False
        except Exception:
            # Native drafts aren't available for this chat (a supergroup/topic
            # raises Textdraft_peer_invalid) — switch to live-edit and flush it
            # now so the user doesn't wait for the next tick. Expected in groups,
            # so log without the traceback.
            logger.info("draft streaming unavailable; switching to live-edit streaming")
            self._disabled = True
            await self._flush_live_edit(latest_message_id)

    async def _flush_live_edit(self, latest_message_id: int | None = None) -> None:
        """Live-edit fallback: send one message, then edit it in place as text
        grows. Defers while the text overflows one chunk (handled at finalize).

        Before editing, re-anchors below the topic tail (:meth:`_drop_if_stale`)
        so the bubble never edits in place *above* a message that landed under it
        — the whole point of ``latest_message_id``."""
        if not self._dirty:
            return
        chunks = self._render(self._raw)
        if not chunks or len(chunks) > 1:
            return
        # Only re-anchor once we can immediately re-send as one bubble (guarded by
        # the single-chunk check above); a multi-chunk stream defers to finalize.
        await self._drop_if_stale(latest_message_id)
        text = chunks[0]
        if self._live_edit_message_id is not None and text == self._live_edit_last:
            self._dirty = False
            return
        try:
            if self._live_edit_message_id is None:
                self._live_edit_message_id = await self._transport.send_message(text)
            else:
                await self._transport.edit_message(self._live_edit_message_id, text)
            self._live_edit_last = text
            self._dirty = False
        except Exception:
            logger.debug("live-edit flush failed", exc_info=True)

    async def finalize(
        self,
        fallback: str = "(the agent finished without producing any text)",
        *,
        latest_message_id: int | None = None,
    ) -> None:
        """Send the accumulated text as real message(s), split at the char cap.

        If a live-edit message exists, its first chunk is delivered by editing
        that message in place (no duplicate of the streamed bubble); any overflow
        chunks are sent as new messages.

        ``latest_message_id`` is the topic tail (:meth:`_drop_if_stale`): when it
        isn't the live-edit message, other messages landed *below* the streamed
        bubble — and since this text must end the turn, the stale bubble is
        deleted and the text re-sent at the bottom (same primitive the live-edit
        flush uses mid-stream). If the delete fails the bubble is edited in place
        instead, so the content is never duplicated.
        """
        text = self._raw if self._raw.strip() else fallback
        await self._drop_if_stale(latest_message_id)
        for i, chunk in enumerate(self._render(text)):
            if i == 0 and self._live_edit_message_id is not None:
                # Skip the edit when the streamed bubble already shows this text —
                # Telegram would otherwise 400 with "message is not modified".
                if chunk != self._live_edit_last:
                    await self._transport.edit_message(self._live_edit_message_id, chunk)
            else:
                await self._transport.send_message(chunk)

    def reset(self) -> None:
        """Forget the just-finalized bubble so the next step streams a fresh one.

        Used at a mid-turn step boundary (a folded-in follow-up): :meth:`finalize`
        has committed this step's text to a permanent message, so drop the
        live-edit anchor and accumulated text — the next flush sends a new bubble
        below it instead of editing the finalized one. A new ``draft_id`` starts a
        clean native-draft animation for the next step."""
        self._raw = ""
        self._dirty = False
        self._live_edit_message_id = None
        self._live_edit_last = None
        self._draft_id = random.randint(1, 2**31)


def _make_transport(
    bot: Any,
    chat_id: int,
    thread_id: int | None,
    on_sent: Callable[[int | None], None] | None = None,
    *,
    rich: bool = False,
) -> DraftTransport:
    # message_thread_id routes both the draft and the final message to the topic.
    topic_kwargs = thread_kwargs(thread_id)

    class _Transport:
        # In rich mode ``text`` arrives as raw GFM (the renderer only chunks it);
        # in MarkdownV2 mode it is already escaped. Each rich call falls back to
        # the MarkdownV2 path for *this message only*, so one payload Telegram
        # dislikes never disables rich mode for the rest of the turn.

        async def send_draft(self, draft_id: int, text: str) -> None:
            if rich:
                await send_rich_draft(
                    bot,
                    chat_id=chat_id,
                    draft_id=draft_id,
                    markdown=text,
                    thread_kwargs=topic_kwargs,
                )
                return
            await bot.send_message_draft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=text,
                parse_mode="MarkdownV2",
                **topic_kwargs,
            )

        async def _send_markdown_v2(self, text: str) -> int | None:
            try:
                msg = await bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="MarkdownV2", **topic_kwargs
                )
            except RetryAfter:
                # Flood control, not a formatting problem — a plain resend would
                # burn another doomed request. The rate limiter already retried.
                raise
            except Exception:
                # Malformed MarkdownV2 → resend without formatting rather than drop.
                logger.debug("MarkdownV2 send failed; falling back to plain text", exc_info=True)
                msg = await bot.send_message(chat_id=chat_id, text=text, **topic_kwargs)
            message_id = getattr(msg, "message_id", None)
            if on_sent is not None:
                on_sent(message_id)
            return message_id

        async def send_message(self, text: str) -> int | None:
            if rich:
                try:
                    message_id = await send_rich_message(
                        bot,
                        chat_id=chat_id,
                        markdown=text,
                        thread_kwargs=topic_kwargs,
                    )
                except RetryAfter:
                    raise
                except Exception:
                    logger.info("rich send rejected; falling back to MarkdownV2", exc_info=True)
                else:
                    if on_sent is not None:
                        on_sent(message_id)
                    return message_id
                # Escaping the same GFM can exceed the 4096 cap that rich mode
                # let through, so the fallback may span several messages. The
                # last one is the live-edit anchor and the topic tail.
                message_id = None
                for chunk in markdown_v2_fallback(text):
                    message_id = await self._send_markdown_v2(chunk)
                return message_id
            return await self._send_markdown_v2(text)

        async def delete_message(self, message_id: int) -> None:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)

        async def edit_message(self, message_id: int, text: str) -> None:
            # edit_message_text addresses the message by id within the chat, so no
            # thread kwargs. "message is not modified" is benign (identical render).
            if rich:
                try:
                    await edit_rich_message(
                        bot, chat_id=chat_id, message_id=message_id, markdown=text
                    )
                except RetryAfter:
                    raise
                except Exception:
                    logger.info("rich edit rejected; falling back to MarkdownV2", exc_info=True)
                else:
                    return
                # Only the first chunk can be edited in place; overflow is left to
                # finalize, which re-sends the whole text as fresh messages.
                chunks = markdown_v2_fallback(text)
                if not chunks:
                    return
                text = chunks[0]
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="MarkdownV2",
                )
            except RetryAfter:
                # Flood control, not a formatting problem — a plain re-edit would
                # burn another doomed request. The rate limiter already retried.
                raise
            except Exception as exc:
                if "not modified" in str(exc).lower():
                    return
                logger.debug("MarkdownV2 edit failed; falling back to plain text", exc_info=True)
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                except Exception as exc2:
                    if "not modified" not in str(exc2).lower():
                        raise

    return _Transport()


async def stream_reply(
    *,
    bot: Any,
    backend: AgentBackend,
    session_id: str | None,
    chat_id: int,
    thread_id: int | None,
    prompt: str,
    directory: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    pending: PendingApprovals | None = None,
    pending_questions: PendingQuestions | None = None,
    allowed_dirs: list[str] | None = None,
    additional_directories: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    mcp: dict[str, Any] | None = None,
    files: list[PromptFile] | None = None,
    on_session_started: Callable[[str], None] | None = None,
    follow_ups: FollowUpChannel | None = None,
    draft_interval: float = DRAFT_INTERVAL_S,
    tool_stream: str = "collapsed",
    rich_messages: bool = False,
    unattended: bool = False,
) -> None:
    """Run one turn through ``backend`` and stream its reply into the topic.

    ``directory``/``provider``/``model``/``effort`` come from the topic's
    resolved context (:class:`balam.router.ResolvedSession`) and ride the
    :class:`~balam.agent.backend.TurnRequest` so the agent runs in the right
    workspace with the right model. The backend owns subscribing/prompting and
    yields the normalized :mod:`balam.agent.events` stream; this function renders
    it, animates a draft as text grows, and finalizes into real message(s) when
    the turn finishes (or fails).

    When ``pending`` is given, a :class:`~balam.agent.events.PermissionRequested`
    is dispatched to a background task that runs :func:`balam.approvals.decide`
    against ``allowed_dirs`` and either auto-allows or shows an inline keyboard and
    awaits the user's choice (ADR-0012). Without ``pending`` the request is left
    unhandled (e.g. unit tests of the text/tool path).

    ``on_session_started`` receives the backend's real session id once known —
    used to persist a lazily-minted SDK session.

    ``follow_ups`` is the channel the bot offers mid-turn messages onto; a
    streaming-input backend folds them into this live turn and marks each fold
    with a :class:`~balam.agent.events.TurnStepFinished`, on which we finalize the
    current answer/reasoning bubbles and reset so the next step streams fresh.

    ``rich_messages`` (``Config.rich_messages``, on by default) sends the
    agent's GFM as Bot API 10.1 rich messages, which Telegram parses natively —
    tables, headings, task lists, collapsibles, and a 32768-char cap; any
    message Telegram rejects falls back to the MarkdownV2 rendering on its own
    (see :mod:`balam.rich_messages`). ``False`` — the deprecated
    ``RICH_MESSAGES=false`` escape hatch — escapes every message to MarkdownV2
    up front. The parameter default stays ``False`` so the transport-level
    tests keep exercising that fallback renderer explicitly; the production
    caller (:mod:`balam.turns`) always passes the config value.

    ``tool_stream`` (``TOOL_STREAM``) picks how tool calls render in the
    progress stream: ``"collapsed"`` (default) folds a burst of consecutive
    calls into one summary line with the per-call detail in an expandable
    blockquote (see :func:`_render_tool_group`); ``"full"`` keeps the legacy
    one-line-per-call stream.

    ``unattended`` marks a turn nobody started by hand — a scheduled run
    (ADR-0016). Approval keyboards and question keyboards both await a tap that
    will never come, and neither has a timeout, so an unattended turn asks for
    neither: :func:`balam.approvals.decide` denies anything past an in-workspace
    read, questions are rejected, and each block is posted into the topic so the
    owner can see in the morning what the agent wanted.

    ``todowrite`` calls are excluded from both modes: the agent's to-do list
    renders as its own live **checklist message** (``📋 Progress`` with one
    ✅/🔄/⬜/❌ line per item), posted once on the first update and edited in
    place as items advance — mirroring iu's progress view.
    """
    # Local session id: known up front for OpenCode, learned from the first
    # SessionStarted event for a lazily-minted SDK session.
    sid = session_id
    # The topic's tail: the id of the most recent message this turn sent to it —
    # live-edit bubbles and finalize chunks (via the transport's on_sent) plus
    # approval prompts, question keyboards, and retry notices (noted at their send
    # sites). The answer stream compares its streamed bubble against this on every
    # flush and at finalize to know whether anything landed below it (Telegram ids
    # are monotonic); if so it re-anchors at the bottom so the answer stays last.
    # Every send must update it — an edit (in place) must not, or a stream would
    # think its own bubble displaced it.
    last_sent_id: int | None = None

    def note_sent(message_id: int | None) -> None:
        nonlocal last_sent_id
        if message_id is not None:
            last_sent_id = message_id

    transport = _make_transport(bot, chat_id, thread_id, on_sent=note_sent, rich=rich_messages)
    # sendMessageDraft is private-chat only; in the Bot API private chats have
    # positive ids and groups/supergroups negative ones, so the chat id alone
    # picks the streaming approach — no wasted draft call per group turn.
    native_drafts = chat_id > 0
    # Rich mode hands the transport raw GFM (Telegram parses it), so the renderer
    # only enforces the 32768-char cap instead of escaping to MarkdownV2.
    render: Renderer = chunk_rich if rich_messages else gfm_to_telegram
    reasoning_draft = DraftSession(transport, native_drafts=native_drafts, render=render)
    answer_draft = DraftSession(transport, native_drafts=native_drafts, render=render)
    topic_kwargs = thread_kwargs(thread_id)

    streaming = True
    # Serializes draft mutation between the periodic flusher and a mid-turn step
    # finalize (a folded-in follow-up): both touch the same DraftSessions, so
    # without this the flusher could send a bubble the finalize is retiring.
    render_lock = asyncio.Lock()

    async def flush_loop() -> None:
        while streaming:
            await asyncio.sleep(draft_interval)
            if not streaming:
                break
            # The reasoning stream keeps its position (no tail); the answer stream
            # re-anchors below anything that landed under it (the topic tail), so
            # it always streams at the bottom rather than above an intervening
            # question/approval prompt.
            async with render_lock:
                await reasoning_draft.flush_draft()
                await answer_draft.flush_draft(latest_message_id=last_sent_id)

    flush_task = asyncio.create_task(flush_loop())

    # Reasoning/progress and answer text are delivered as separate messages.
    # Tool calls are progress, so they live with the reasoning stream.
    reasoning_parts: dict[str, StreamPart] = {}
    answer_parts: dict[str, StreamPart] = {}
    # The assistant message whose text currently fills the answer draft. The
    # agent opens a new assistant message per step, and a step's interim
    # narration ("I'll check…") is a plain text part just like the final
    # answer — only the *last* message's text is the answer.
    answer_message_id: str | None = None
    order = 0
    error_text: str | None = None
    retry_noticed = False
    # What the agent left running in the background, as of the last
    # BackgroundTasksChanged (full state, so this is the live set — empty once
    # everything finished). Reported at turn end so a task never keeps running,
    # or gets killed with the turn's process, without the user knowing.
    background_tasks: tuple[BackgroundTask, ...] = ()
    # Collapsed tool stream (TOOL_STREAM=collapsed): consecutive calls fold into
    # one group part. The *open* group keeps absorbing new calls until something
    # else lands in the topic — answer/reasoning text, an approval keyboard, a
    # question, a retry notice, a step boundary, or the turn's end closes it.
    group_calls: dict[str, list[str]] = {}  # group part key → call ids, arrival order
    group_entries: dict[str, GroupEntry] = {}  # call id → latest state
    call_group: dict[str, str] = {}  # call id → its group part key
    open_group_key: str | None = None
    group_count = 0

    def refresh_group(group_key: str) -> None:
        """Re-render a group part from its calls' latest state (a call that was
        still running when its group closed re-renders the closed group when it
        completes, e.g. after a permission prompt)."""
        nonlocal order
        entries = [group_entries[cid] for cid in group_calls.get(group_key, ())]
        kind, text = _render_tool_group(
            entries,
            active=group_key == open_group_key,
            directory=directory,
            rich=rich_messages,
        )
        prior = reasoning_parts.get(group_key)
        if prior is None:
            if not text:
                return
            pos = order
            order += 1
        else:
            pos = prior[0]
        reasoning_parts[group_key] = (pos, kind, text)
        reasoning_draft.set_text(_join_stream(reasoning_parts))

    def close_open_group() -> None:
        """Seal the open group, if any: later calls start a new group, and the
        final form (single line / expandable quote) replaces the live summary."""
        nonlocal open_group_key
        if open_group_key is None:
            return
        key, open_group_key = open_group_key, None
        refresh_group(key)

    dirs = allowed_dirs or ([directory] if directory else [])
    # Per-request approval tasks, so the event loop isn't blocked while the user
    # decides. Torn down with the consumer.
    permission_tasks: set[asyncio.Task[None]] = set()
    question_tasks: set[asyncio.Task[None]] = set()

    async def request_approval(
        request_id: str, category: str, tool: str, tool_input: dict[str, Any]
    ) -> None:
        """Ask the user via an inline keyboard, then reply to the backend. The
        callback handler resolves the future and updates the message; here we
        only translate the choice into a permission reply. ``category`` drives the
        keyboard (whether to offer "accept all edits"); ``tool`` is display-only."""
        assert pending is not None
        # The keyboard lands below the progress stream, ending the tool burst —
        # seal the group here, not on every PermissionRequested (auto-allowed
        # requests send nothing and must not fragment the group).
        close_open_group()
        token, future = pending.register(sid or "")
        gfm = _format_approval_request(tool, tool_input, directory, category)
        keyboard = _approval_keyboard(token, category)
        chunks = gfm_to_telegram(gfm)
        text = chunks[0] if chunks else f"🔐 Allow {tool}?"
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
                **topic_kwargs,
            )
            note_sent(getattr(msg, "message_id", None))
        except Exception:
            logger.debug("approval keyboard MarkdownV2 send failed; retrying plain", exc_info=True)
            try:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=f"🔐 Allow {tool}? (see request)",
                    reply_markup=keyboard,
                    **topic_kwargs,
                )
                note_sent(getattr(msg, "message_id", None))
            except Exception:
                logger.exception("failed to send approval keyboard; denying")
                pending.discard(token)
                await backend.reply_permission(
                    request_id,
                    allow=False,
                    directory=directory,
                    message="Could not prompt the user.",
                )
                return
        try:
            choice = await future
        except asyncio.CancelledError:
            # Turn torn down (e.g. /cancel) before the user answered: unblock the
            # agent so it isn't left waiting on a permission that will never come.
            await backend.reply_permission(
                request_id, allow=False, directory=directory, message="Cancelled."
            )
            raise
        finally:
            pending.discard(token)
        if choice is Choice.DENY:
            await backend.reply_permission(
                request_id, allow=False, directory=directory, message="Denied by the user."
            )
        else:
            await backend.reply_permission(request_id, allow=True, directory=directory)

    async def note_unattended_block(detail: str) -> None:
        """Post what a scheduled run refused, as a line in the topic.

        The owner reads the topic hours later, so a silent denial would look like
        the agent simply chose not to do the thing. Best-effort, like every other
        progress line — a failed send must not break the turn."""
        # The notice lands below the progress stream: seal the tool group.
        close_open_group()
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=f"🚫 {detail} — scheduled run, nobody to approve.",
                **topic_kwargs,
            )
            note_sent(getattr(msg, "message_id", None))
        except Exception:
            logger.debug("failed to post unattended-block notice", exc_info=True)

    async def handle_permission(request: PermissionRequested) -> None:
        cwd = dirs[0] if dirs else None
        # Classify by the permission category; take edit targets from the request
        # metadata (authoritative for apply_patch) and reads from the input.
        paths = request_target_paths(request.category, request.metadata, request.input, cwd)
        verdict = decide(
            request.category,
            paths,
            allowed_dirs=dirs,
            accept_all_edits=pending.is_accept_all_edits(sid or "") if pending else False,
            unattended=unattended,
        )
        if verdict is Verdict.ALLOW:
            await backend.reply_permission(request.request_id, allow=True, directory=directory)
            return
        if verdict is Verdict.DENY:
            # Unattended only (ADR-0016): refuse rather than park the turn on a
            # keyboard nobody will tap. The agent gets a refusal it can reason
            # about and still finish, instead of hanging and wedging the topic.
            await backend.reply_permission(
                request.request_id,
                allow=False,
                directory=directory,
                message=(
                    "Denied: this is a scheduled, unattended run. Only reads inside the "
                    "workspace are allowed. Report what you needed instead of retrying."
                ),
            )
            await note_unattended_block(f"denied {request.tool}")
            return
        await request_approval(request.request_id, request.category, request.tool, request.input)

    async def request_questions(request: QuestionAsked) -> None:
        if unattended:
            # Same wedge as an unattended approval: the question keyboard awaits a
            # tap that will never come, and gather() has no timeout. Reject it so
            # the turn finishes, and say so in the topic (ADR-0016).
            await note_unattended_block("skipped a question")
            await backend.reject_question(request.request_id, directory=directory)
            return
        if pending_questions is None:
            await backend.reject_question(request.request_id, directory=directory)
            return
        raw_questions = request.questions
        if not raw_questions:
            await backend.reject_question(request.request_id, directory=directory)
            return

        questions = [q for q in raw_questions if isinstance(q, dict)]
        labels: list[list[str]] = []
        multiples: list[bool] = []
        customs: list[bool] = []
        for question in questions:
            options = question.get("options")
            if not isinstance(options, list) or not options:
                await backend.reject_question(request.request_id, directory=directory)
                return
            labels.append([str(o.get("label") or "") for o in options if isinstance(o, dict)])
            multiples.append(question.get("multiple", False) is True)
            customs.append(question.get("custom", True) is not False)
        if any(not question_labels for question_labels in labels):
            await backend.reject_question(request.request_id, directory=directory)
            return

        # Question keyboards land below the progress stream: seal the tool group.
        close_open_group()
        token, futures = pending_questions.register(
            sid or "",
            labels,
            multiples=multiples,
            customs=customs,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        try:
            for index, question in enumerate(questions):
                chunks = gfm_to_telegram(_format_question(question))
                text = chunks[0] if chunks else "❓ Question"
                keyboard = _question_keyboard(
                    token,
                    index,
                    question["options"],
                    custom=customs[index],
                    multiple=multiples[index],
                )
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="MarkdownV2",
                    reply_markup=keyboard,
                    **topic_kwargs,
                )
                note_sent(getattr(msg, "message_id", None))
                pending_questions.set_message(token, index, getattr(msg, "message_id", None), text)
            answers = await asyncio.gather(*futures)
        except asyncio.CancelledError:
            pending_questions.discard(token)
            await backend.reject_question(request.request_id, directory=directory)
            raise
        except Exception:
            logger.exception("failed to ask the agent's question in Telegram")
            pending_questions.discard(token)
            await backend.reject_question(request.request_id, directory=directory)
            return
        await backend.reply_question(request.request_id, answers, directory=directory)

    # The live to-do checklist: one message per turn, posted on the first
    # todowrite and edited in place as items advance (ported from iu).
    todo_message_id: int | None = None
    todo_last_rendered: str | None = None

    async def update_todos(todos: list[Any]) -> None:
        """Post or edit the turn's to-do checklist message.

        The checklist keeps its position (like the reasoning stream) and shows
        the agent's latest to-do state; identical re-renders are skipped. It is
        purely cosmetic — a failed send/edit is logged and must never abort the
        turn."""
        nonlocal todo_message_id, todo_last_rendered
        gfm = _render_todos(todos, rich=rich_messages)
        if not gfm or gfm == todo_last_rendered:
            return
        # Same renderer as the streams: raw GFM in rich mode, escaped otherwise.
        chunks = render(gfm)
        if not chunks:
            return
        todo_last_rendered = gfm
        try:
            if todo_message_id is None:
                # The checklist lands below the progress stream, ending the
                # tool burst — seal the group, like any other message send.
                close_open_group()
                todo_message_id = await transport.send_message(chunks[0])
            else:
                await transport.edit_message(todo_message_id, chunks[0])
        except Exception:
            logger.debug("to-do checklist update failed", exc_info=True)

    async def note_retry(detail: str | None) -> None:
        """Tell the user the turn is being retried (e.g. provider rate limit).

        Some failures (rate limits, transient 5xx) are retried internally without
        ending the turn, so it can stall for minutes with no visible output.
        Surface a single notice per turn — enough to explain the silence and
        point at ``/cancel`` — without spamming one per attempt.
        """
        nonlocal retry_noticed
        if retry_noticed:
            return
        retry_noticed = True
        # The notice lands below the progress stream: seal the tool group.
        close_open_group()
        body = "⏳ The model provider is rate-limited — retrying…"
        if detail:
            body += f"\n{detail}"
        body += "\nThis can take a while; send /cancel to stop waiting."
        try:
            msg = await bot.send_message(chat_id=chat_id, text=body, **topic_kwargs)
            note_sent(getattr(msg, "message_id", None))
        except Exception:
            logger.debug("failed to post retry notice", exc_info=True)

    turn = TurnRequest(
        directory=directory,
        prompt=prompt,
        session_id=session_id,
        provider=provider,
        model=model,
        effort=effort,
        files=files,
        allowed_tools=allowed_tools or [],
        additional_directories=additional_directories or [],
        mcp=mcp or {},
        chat_id=chat_id,
        thread_id=thread_id,
        follow_ups=follow_ups,
    )

    async def finalize_step() -> None:
        """Commit the current step's bubbles and reset for the next one.

        A streaming-input backend emits :class:`TurnStepFinished` when it folds a
        mid-turn follow-up into the live turn: this step's answer is done, so make
        its reasoning/answer drafts permanent and clear the accumulators. The
        render lock keeps the periodic flusher from racing the finalize."""
        nonlocal answer_message_id
        async with render_lock:
            # Seal the open tool group into the finalized bubble, then forget
            # all group state: the step's parts are gone, so a call that
            # completes after this must open a fresh group, not resurrect (and
            # re-send) one that was already committed.
            close_open_group()
            group_calls.clear()
            group_entries.clear()
            call_group.clear()
            if reasoning_draft.text.strip():
                await reasoning_draft.finalize()
                reasoning_draft.reset()
            if answer_draft.text.strip():
                await answer_draft.finalize(latest_message_id=last_sent_id)
                answer_draft.reset()
            reasoning_parts.clear()
            answer_parts.clear()
            answer_message_id = None

    async def consume() -> None:
        nonlocal order, error_text, answer_message_id, sid, open_group_key, group_count
        nonlocal background_tasks
        async for event in backend.run_turn(turn):
            if isinstance(event, SessionStarted):
                if sid != event.session_id:
                    sid = event.session_id
                    if on_session_started is not None:
                        on_session_started(sid)

            elif isinstance(event, TextUpdated):
                if event.text:
                    close_open_group()
                if event.message_id != answer_message_id:
                    # Text from a new step: what the answer draft holds was an
                    # earlier step's narration, not the answer. Demote it to the
                    # progress stream (it keeps its arrival order, so it
                    # interleaves with the tool lines it narrates) and start the
                    # answer over with the new step's text.
                    if answer_parts:
                        for pid, (pos, _kind, prev) in answer_parts.items():
                            reasoning_parts[pid] = (pos, "narration", prev)
                        answer_parts.clear()
                        reasoning_draft.set_text(_join_stream(reasoning_parts))
                    answer_message_id = event.message_id
                if event.part_id in answer_parts:
                    answer_parts[event.part_id] = (
                        answer_parts[event.part_id][0],
                        "text",
                        event.text,
                    )
                else:
                    answer_parts[event.part_id] = (order, "text", event.text)
                    order += 1
                answer_draft.set_text(_join_stream(answer_parts))

            elif isinstance(event, ReasoningUpdated):
                if event.text:
                    close_open_group()
                if event.part_id in reasoning_parts:
                    reasoning_parts[event.part_id] = (
                        reasoning_parts[event.part_id][0],
                        "text",
                        event.text,
                    )
                else:
                    reasoning_parts[event.part_id] = (order, "text", event.text)
                    order += 1
                reasoning_draft.set_text(_join_stream(reasoning_parts))

            elif isinstance(event, ToolUpdated):
                if event.tool == Tool.TODOWRITE:
                    # The to-do list gets its own live checklist message
                    # instead of a tool line, in both stream modes.
                    todos = event.input.get("todos")
                    if isinstance(todos, list):
                        await update_todos(todos)
                    continue
                if tool_stream == "full":
                    # Legacy stream: reserve a slot at the call's arrival position
                    # (so the tool line interleaves before any later text), but
                    # only render once the call finishes.
                    key = f"tool:{event.call_id}"
                    if key not in reasoning_parts:
                        reasoning_parts[key] = (order, "tool", "")
                        order += 1
                    if event.status in ("completed", "error"):
                        rendered = _render_tool_part(
                            event.tool,
                            event.input,
                            event.status,
                            event.output,
                            event.error,
                            directory,
                        )
                        reasoning_parts[key] = (reasoning_parts[key][0], "tool", rendered)
                        reasoning_draft.set_text(_join_stream(reasoning_parts))
                    continue
                # Collapsed stream: fold the call into the open group (opening
                # one if needed); the group renders as one updating summary line
                # and seals into its final form when the burst ends.
                group_key = call_group.get(event.call_id)
                if group_key is None:
                    if open_group_key is None:
                        group_count += 1
                        open_group_key = f"toolgroup:{group_count}"
                        group_calls[open_group_key] = []
                    group_key = open_group_key
                    call_group[event.call_id] = group_key
                    group_calls[group_key].append(event.call_id)
                group_entries[event.call_id] = (
                    event.tool,
                    event.input,
                    event.status,
                    event.output,
                    event.error,
                )
                if event.status == "error":
                    # A failure is loud: pull it out of the group and render it
                    # standalone in the full form (command + output tail); the
                    # calls that follow start a fresh group.
                    if event.call_id in group_calls.get(group_key, ()):
                        group_calls[group_key].remove(event.call_id)
                    prior = reasoning_parts.get(f"tool:{event.call_id}")
                    pos = prior[0] if prior is not None else order
                    if prior is None:
                        order += 1
                    reasoning_parts[f"tool:{event.call_id}"] = (
                        pos,
                        "tool",
                        _render_tool_part(
                            event.tool,
                            event.input,
                            event.status,
                            event.output,
                            event.error,
                            directory,
                        ),
                    )
                    if group_key == open_group_key:
                        open_group_key = None
                    reasoning_draft.set_text(_join_stream(reasoning_parts))
                refresh_group(group_key)

            elif isinstance(event, PermissionRequested):
                # Handle in a child task so a slow user decision doesn't stall the
                # event loop (the turn stays busy while a permission is pending).
                if pending is None:
                    continue
                ptask = asyncio.create_task(handle_permission(event))
                permission_tasks.add(ptask)
                ptask.add_done_callback(permission_tasks.discard)

            elif isinstance(event, QuestionAsked):
                qtask = asyncio.create_task(request_questions(event))
                question_tasks.add(qtask)
                qtask.add_done_callback(question_tasks.discard)

            elif isinstance(event, BackgroundTasksChanged):
                background_tasks = tuple(event.tasks)

            elif isinstance(event, RetryNotice):
                await note_retry(event.detail)

            elif isinstance(event, TurnStepFinished):
                await finalize_step()

            elif isinstance(event, TurnFailed):
                close_open_group()
                error_text = event.message
                break

            elif isinstance(event, TurnFinished):
                close_open_group()
                break
        close_open_group()

    consume_task = asyncio.create_task(consume())
    try:
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing", **topic_kwargs)
        except Exception:
            pass

        try:
            await consume_task
        except Exception as exc:
            # A failed turn must still finalize a real message (ADR-0010): fold the
            # error into the reply instead of letting it bubble out and skip
            # finalize() below.
            logger.exception("streaming the reply failed")
            error_text = error_text or str(exc) or exc.__class__.__name__

        # A turn that died mid-burst still finalizes: seal the group so the
        # progress bubble commits in its final form, not the live summary.
        close_open_group()
        if error_text:
            base = _join_stream(answer_parts)
            prefix = f"{base}\n\n" if base.strip() else ""
            answer_draft.set_text(f"{prefix}⚠️ {error_text}")
    finally:
        # Stop the flusher and the consumer before finalizing so neither races the
        # real message, and so a leftover task can't outlive the turn. Pending
        # approval tasks are cancelled too; each rejects its request on the way
        # out so OpenCode isn't left blocked on an answer that will never come.
        streaming = False
        if not consume_task.done():
            consume_task.cancel()
        for ptask in list(permission_tasks):
            if not ptask.done():
                ptask.cancel()
        for qtask in list(question_tasks):
            if not qtask.done():
                qtask.cancel()
        await asyncio.gather(
            flush_task, consume_task, *permission_tasks, *question_tasks, return_exceptions=True
        )

    # Anything the agent left running rides along in the answer rather than as its
    # own message: the answer has to end the turn (see _drop_if_stale), and a
    # notice sent after it would bury it.
    if background_tasks:
        notice = _render_background_notice(background_tasks)
        base = answer_draft.text.rstrip()
        answer_draft.set_text(f"{base}\n\n{notice}" if base else notice)

    # Replace ephemeral drafts with real, persistent messages. Reasoning/progress
    # is intentionally separate from the answer; only emit the answer fallback if
    # the turn produced nothing visible at all. The reasoning stream keeps its
    # position; the answer must end the turn, so its finalize gets the last sent
    # id and re-sends at the bottom if anything landed below its bubble.
    if reasoning_draft.text.strip():
        await reasoning_draft.finalize()
    if answer_draft.text.strip() or not reasoning_draft.text.strip():
        await answer_draft.finalize(latest_message_id=last_sent_id)
