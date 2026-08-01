"""Per-topic registry of in-flight agent turns (for ``/cancel``) and the queue of
messages waiting behind them.

``stream_reply`` is launched as a background task per incoming message; we record
that handle here, keyed by ``(chat_id, thread_key)``, so ``/cancel`` can find and
abort the turn running in a topic, and ``/status`` can report whether one is in
flight. A topic maps to one session and runs at most one turn at a time
(ADR-0009), so a single running slot per key is enough.

Because OpenCode runs one turn per session, a message that arrives while a turn
is still streaming must **not** fire a second prompt at the same session — that
collides and silently drops the message. Instead the message is parked in the
topic's FIFO queue (:class:`TurnJob`) and run when the current turn finishes.

Keying mirrors :class:`balam.store.SessionStore`: the General topic's absent
``message_thread_id`` normalizes to thread id ``0`` so the key is always a
concrete integer pair.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from telegram import Message
from telegram.ext import ContextTypes

from balam.agent.backend import AgentBackend, FollowUp, FollowUpChannel
from balam.approvals import PendingApprovals, PendingQuestions
from balam.attachments import PromptFile
from balam.config import Config
from balam.router import Router, TopicRef
from balam.store import SessionStore
from balam.streamer import stream_reply
from balam.telegram_utils import thread_kwargs
from balam.topics import auto_name_topic, topic_title

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    """A running turn: its streaming task plus what ``/cancel`` needs to abort it
    server-side (the OpenCode session and the context directory scoping it).

    ``follow_ups`` is the channel the bot offers mid-turn messages onto for a
    streaming-input backend to fold into this live turn (Claude Code-style);
    ``None`` when the backend can't (OpenCode), so those messages fall through to
    the topic queue instead."""

    task: asyncio.Task[None]
    session_id: str
    directory: str
    follow_ups: FollowUpChannel | None = None


@dataclass
class TurnJob:
    """A queued message waiting to run as a turn. Everything ``stream_reply`` needs
    is captured at enqueue time (the session is already resolved) so draining the
    queue is synchronous — the running slot is handed to the next job without an
    ``await`` in between, leaving no window for a concurrent message to slip a
    second turn onto the same session.

    Deliberately *not* captured: the plan-agent choice. ``_start_turn`` derives it
    from the topic's plan-mode flag when the job actually runs, so a message
    queued behind a turn respects a plan approval or ``/plan off`` that happened
    while it waited."""

    prompt: str
    #: ``None`` for an SDK topic awaiting its first turn (the id is minted then).
    session_id: str | None
    directory: str
    provider: str | None
    model: str | None
    effort: str | None
    allowed_dirs: list[str]
    files: list[PromptFile]
    #: Context capabilities forwarded to a stateless backend (SDK) per turn; the
    #: OpenCode backend applied these at session creation and ignores them here.
    allowed_tools: list[str] = field(default_factory=list)
    additional_directories: list[str] = field(default_factory=list)
    mcp: dict[str, Any] = field(default_factory=dict)
    #: Nobody is watching this turn — a scheduled run (ADR-0016). Approval and
    #: question keyboards are refused rather than awaited, since neither has a
    #: timeout and an unanswered one would hold the topic's running slot forever.
    #: A property of the *turn*, not the topic: the owner's reply in the morning's
    #: topic starts from a ``Message`` and is attended like any other.
    unattended: bool = False


class TurnRegistry:
    def __init__(self) -> None:
        self._turns: dict[tuple[int, int], Turn] = {}
        self._queues: dict[tuple[int, int], deque[TurnJob]] = {}

    @staticmethod
    def _key(chat_id: int, thread_id: int | None) -> tuple[int, int]:
        return (chat_id, SessionStore.thread_key(thread_id))

    def register(
        self,
        chat_id: int,
        thread_id: int | None,
        task: asyncio.Task[None],
        session_id: str,
        directory: str,
        follow_ups: FollowUpChannel | None = None,
    ) -> None:
        """Record the turn now running in a topic (overwriting any stale entry)."""
        self._turns[self._key(chat_id, thread_id)] = Turn(
            task=task, session_id=session_id, directory=directory, follow_ups=follow_ups
        )

    def get(self, chat_id: int, thread_id: int | None) -> Turn | None:
        """The turn currently running in a topic, or ``None`` if idle."""
        return self._turns.get(self._key(chat_id, thread_id))

    def clear(self, chat_id: int, thread_id: int | None, task: asyncio.Task[None]) -> None:
        """Drop the topic's entry once ``task`` finishes — but only if it is still
        the registered one, so a turn that started after this one isn't evicted by
        a late ``finally`` from the older turn."""
        key = self._key(chat_id, thread_id)
        existing = self._turns.get(key)
        if existing is not None and existing.task is task:
            del self._turns[key]

    def enqueue(self, chat_id: int, thread_id: int | None, job: TurnJob) -> int:
        """Append ``job`` to the topic's queue; return its 1-based position."""
        queue = self._queues.setdefault(self._key(chat_id, thread_id), deque())
        queue.append(job)
        return len(queue)

    def pop_next(self, chat_id: int, thread_id: int | None) -> TurnJob | None:
        """Remove and return the topic's next queued job, or ``None`` if empty."""
        key = self._key(chat_id, thread_id)
        queue = self._queues.get(key)
        if not queue:
            return None
        job = queue.popleft()
        if not queue:
            del self._queues[key]
        return job

    def queue_len(self, chat_id: int, thread_id: int | None) -> int:
        """How many messages are queued behind the topic's running turn."""
        queue = self._queues.get(self._key(chat_id, thread_id))
        return len(queue) if queue else 0

    def clear_queue(self, chat_id: int, thread_id: int | None) -> int:
        """Drop every queued job for a topic; return how many were dropped."""
        queue = self._queues.pop(self._key(chat_id, thread_id), None)
        return len(queue) if queue else 0


async def notify_error(bot: Any, chat_id: int, thread_id: int | None, exc: Exception) -> None:
    """Post a short error notice into the topic (ADR-0009 edge), swallowing any
    delivery failure so it never masks the original error."""
    try:
        await bot.send_message(chat_id=chat_id, text=f"⚠️ {exc}", **thread_kwargs(thread_id))
    except Exception:
        logger.debug("failed to deliver error notice", exc_info=True)


async def submit_turn(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    files: list[PromptFile],
    *,
    thread_id: int | None,
    queued_reply: str = "⏳ Queued (#{position}) — I'll run this after the current turn finishes.",
    prompt_prefix: str = "",
) -> None:
    """Run ``text`` as the topic's turn, or park it in the topic's queue when a
    turn is already streaming.

    The message-bound dispatch tail: it owns everything that needs a
    :class:`~telegram.Message` — the topic title, the follow-up acknowledgement,
    and the queued-turn reply. Resolving the session and building the job is
    :func:`resolve_turn_job`; :func:`start_prompt` is the same path for a caller
    with no message (the scheduled runs of ADR-0016).

    ``thread_id`` is explicit because a General message has already been rehomed
    into a freshly created topic by the time it gets here; ``queued_reply`` is
    formatted with the job's 1-based queue ``position``. ``prompt_prefix`` is
    prepended only to the agent-facing prompt (forward/reply header) — topic
    auto-naming still uses the owner's own ``text``.
    """
    turns: TurnRegistry = context.application.bot_data["turns"]
    chat_id = message.chat_id

    job = await resolve_turn_job(
        context,
        chat_id,
        thread_id,
        text,
        title=topic_title(message, thread_id),
        files=files,
        prompt_prefix=prompt_prefix,
    )
    if job is None:
        return

    # A message that lands while a turn is still streaming can't fire a second
    # prompt at the same session — one turn per topic (ADR-0009). Two paths, both
    # decided with no ``await`` between the check and the act so the running
    # turn's teardown can't race in and lose the message:
    #
    #  * Streaming-input backend (the SDK): fold it into the LIVE turn so the
    #    agent picks it up at its next step (Claude Code-style). ``offer`` returns
    #    False only if that turn is already closing, in which case we fall through
    #    to the queue and it runs as the next turn.
    #  * Otherwise (OpenCode): park it in the topic's FIFO queue; the running turn
    #    drains it when it finishes.
    running = turns.get(chat_id, thread_id)
    if running is not None:
        backend: AgentBackend = context.application.bot_data["backend"]
        if (
            backend.supports_streaming_input
            and running.follow_ups is not None
            and running.follow_ups.offer(FollowUp(prompt=text, files=files))
        ):
            await message.reply_text("📨 Sent — I'll pick this up in the current turn.")
            return
        position = turns.enqueue(chat_id, thread_id, job)
        await message.reply_text(queued_reply.format(position=position))
        return

    start_turn(context, chat_id, thread_id, job)


async def resolve_turn_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    thread_id: int | None,
    text: str,
    *,
    title: str,
    files: list[PromptFile] | None = None,
    prompt_prefix: str = "",
    unattended: bool = False,
) -> TurnJob | None:
    """Resolve the topic's session, auto-name the topic if it still needs one, and
    package everything a turn needs. ``None`` (with a notice already posted in the
    topic) if the session couldn't be resolved at all — OpenCode down, and so on.

    Split out of :func:`submit_turn` so a caller with no originating message can
    build the same job: the queue/follow-up decision above needs the job in hand
    before it can choose, so it can't simply delegate to :func:`start_prompt`.
    ``prompt_prefix`` is prepended only to the agent-facing prompt (the
    forward/reply header) — auto-naming still uses the owner's own ``text``.
    """
    router: Router = context.application.bot_data["router"]
    files = files or []
    try:
        ref = TopicRef(chat_id=chat_id, thread_id=thread_id, title=title)
        resolved = await router.resolve(ref)
        await auto_name_topic(
            context.bot,
            router,
            ref,
            resolved.context_name,
            text,
            has_files=bool(files),
        )
    except Exception as exc:
        # Couldn't even resolve the session (OpenCode down, etc.) — report and stop.
        logger.exception("failed to resolve session")
        await notify_error(context.bot, chat_id, thread_id, exc)
        return None

    return TurnJob(
        prompt=f"{prompt_prefix}{text}" if prompt_prefix else text,
        session_id=resolved.session_id,
        directory=resolved.directory,
        provider=resolved.provider,
        model=resolved.model,
        effort=resolved.effort,
        allowed_dirs=[resolved.directory, *resolved.additional_directories],
        files=files,
        allowed_tools=resolved.allowed_tools,
        additional_directories=resolved.additional_directories,
        mcp=resolved.mcp,
        unattended=unattended,
    )


async def start_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    thread_id: int | None,
    prompt: str,
    *,
    title: str,
    unattended: bool = False,
) -> bool:
    """Run ``prompt`` as a turn in an existing topic, with no originating message.

    The message-free half of :func:`submit_turn`, used by the scheduled path
    (ADR-0016). It deliberately has no queue branch: a scheduled run targets a
    *brand-new* topic, so it can never collide with a turn already running there.
    Returns whether the turn started.
    """
    job = await resolve_turn_job(
        context, chat_id, thread_id, prompt, title=title, unattended=unattended
    )
    if job is None:
        return False
    start_turn(context, chat_id, thread_id, job)
    return True


def start_turn(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    thread_id: int | None,
    job: TurnJob,
) -> None:
    """Run ``job`` as the topic's turn in a background task, then hand the running
    slot to the next queued message when it finishes.

    The turn runs as a background task registered in the turn registry, so the
    message handler returns immediately and a concurrent ``/cancel`` update can
    interrupt it (PTB processes updates sequentially, so awaiting in the handler
    would block ``/cancel``).
    """
    backend: AgentBackend = context.application.bot_data["backend"]
    turns: TurnRegistry = context.application.bot_data["turns"]
    pending: PendingApprovals = context.application.bot_data["pending"]
    router: Router = context.application.bot_data["router"]

    # Mid-turn messages fold into this live turn only on a streaming-input backend
    # (the SDK). On OpenCode the channel stays None, so they queue instead.
    follow_ups = FollowUpChannel() if backend.supports_streaming_input else None

    # Config is optional here so unit tests of the bot path can omit it; the
    # streamer's defaults then apply.
    config: Config | None = context.application.bot_data.get("config")

    async def run() -> None:
        cancelled = False
        try:
            await stream_reply(
                bot=context.bot,
                backend=backend,
                session_id=job.session_id,
                chat_id=chat_id,
                thread_id=thread_id,
                prompt=job.prompt,
                directory=job.directory,
                provider=job.provider,
                model=job.model,
                effort=job.effort,
                pending=pending,
                pending_questions=context.application.bot_data.setdefault(
                    "pending_questions", PendingQuestions()
                ),
                allowed_dirs=job.allowed_dirs,
                additional_directories=job.additional_directories,
                allowed_tools=job.allowed_tools,
                mcp=job.mcp,
                files=job.files,
                on_session_started=lambda sid: router.persist_session(chat_id, thread_id, sid),
                follow_ups=follow_ups,
                tool_stream=config.tool_stream if config is not None else "collapsed",
                rich_messages=config.rich_messages if config is not None else False,
                unattended=job.unattended,
            )
        except asyncio.CancelledError:
            cancelled = True  # /cancel aborted the turn; don't auto-run queued work.
            raise
        except Exception as exc:
            logger.exception("failed to handle message")
            await notify_error(context.bot, chat_id, thread_id, exc)
        finally:
            # Release the slot and hand it straight to the next queued message.
            # clear → pop → start_turn run without an ``await`` between them, so
            # the slot never blinks empty and a concurrent message can't slip a
            # second turn onto the same session.
            turns.clear(chat_id, thread_id, task)
            next_job = None if cancelled else turns.pop_next(chat_id, thread_id)
            if next_job is not None:
                start_turn(context, chat_id, thread_id, next_job)

    task = asyncio.create_task(run())
    turns.register(chat_id, thread_id, task, job.session_id, job.directory, follow_ups)


def abort_turn(
    turn: Any, backend: AgentBackend, tasks: set[asyncio.Task[None]]
) -> asyncio.Task[None] | None:
    """Cancel a running turn locally and abort it on the backend (best-effort).

    Cancelling the local task stops streaming; the abort tells the backend to
    stop generating. The abort runs as a background task so callers needn't await
    the round-trip before replying — but it is anchored in ``tasks`` (with a done
    callback that removes it) because the event loop keeps only a *weak*
    reference to a bare task: an unanchored one can be garbage-collected
    mid-flight, dropping the abort. ``None`` when there is no turn (or no session
    id yet, e.g. an SDK turn that hasn't minted one — cancelling the task is
    enough to tear down its query)."""
    if turn is None:
        return None
    turn.task.cancel()
    if not turn.session_id:
        return None
    task = asyncio.create_task(backend.abort(turn.session_id, directory=turn.directory))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task
