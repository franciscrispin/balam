"""Integrations: local services that post buttons through Balam (ADR-0018).

Telegram hands each bot's updates to exactly one consumer, and Balam is it. So a
service on this machine that wants to ask the team something with buttons (a
payment to approve, say) cannot listen for the taps itself. Instead it is named
in ``config.yaml`` (:class:`balam.contexts.IntegrationConfig`) and Balam relays:

  1. **Post.** The service calls ``POST /integrations/<name>/messages`` on
     Balam's loopback server (:mod:`balam.server`) with its secret as a bearer
     token. Balam sends the message, stores it and its buttons in SQLite
     (:mod:`balam.store`), and puts only ``int:<token>`` in each button's
     callback data, so the payload is never squeezed into Telegram's 64 bytes.
     The same call, given a ``message_id``, edits a message it posted earlier.
  2. **Tap.** :func:`handle_integration_callback` re-checks the trust boundary
     (ADR-0008) and the integration's ``tappers``, spends every button on the
     message in one synchronous step, shows "Working..." at once, then POSTs the
     tap to the service's ``url``, signed with the secret. The service's answer
     becomes the message's new text and buttons.

A tap never reaches the agent: it goes straight to the service, with no LLM turn
in between. Nothing here is specific to any one service.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from balam.auth import callback_authorized
from balam.config import Config
from balam.contexts import IntegrationConfig
from balam.markdown import escape_markdown_v2
from balam.store import IntegrationButtonRow, IntegrationMessageRow, SessionStore
from balam.telegram_utils import thread_kwargs

logger = logging.getLogger(__name__)

#: Callback data prefix for an integration's button: ``int:<token>``.
CALLBACK_PREFIX = "int:"

#: Header carrying the tap's signature: ``sha256=<hex HMAC of the body>``.
SIGNATURE_HEADER = "X-Balam-Signature"

#: How long a service has to answer a tap. Long work should answer at once
#: ("Recording...") and edit the message later through the post endpoint.
TAP_TIMEOUT_S = 30.0

#: Telegram only takes an answer to a tap for a short while, and shows a
#: spinner until then. A reply inside this window gets its toast; a slower one
#: has the query answered blank when the window closes, and its toast is dropped
#: (the message edit still carries the outcome).
ANSWER_WITHIN_S = 10.0

#: How long a stopping Balam waits for taps still with their service.
DRAIN_ON_STOP_S = 10.0

#: Room is left under Telegram's 4096 so the "Working..." and failure notes
#: always fit below the text.
MAX_TEXT = 3800
MAX_BUTTONS = 8
MAX_DATA_BYTES = 2048
#: Telegram's limit for a photo sent by upload.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class IntegrationButton(BaseModel):
    """A button as the service describes it. ``data`` stays in Balam's SQLite
    and comes back verbatim with the tap."""

    label: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    data: Any = None

    @field_validator("data")
    @classmethod
    def _data_is_small_json(cls, value: Any) -> Any:
        try:
            encoded = json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("data must be JSON") from exc
        if len(encoded.encode()) > MAX_DATA_BYTES:
            raise ValueError(f"data must be at most {MAX_DATA_BYTES} bytes of JSON")
        return value


class IntegrationPost(BaseModel):
    """The body of ``POST /integrations/<name>/messages``."""

    text: str = Field(min_length=1, max_length=MAX_TEXT)
    #: Base64 of a photo, sent just above the text. New messages only.
    image: str | None = None
    buttons: list[IntegrationButton] = Field(default_factory=list, max_length=MAX_BUTTONS)
    #: Forum topic to post in, made if missing; defaults to the configured one.
    topic: str | None = Field(default=None, min_length=1, max_length=128)
    #: Edit this earlier message (one this integration posted) instead.
    message_id: int | None = None
    parse_mode: Literal["HTML", "MarkdownV2"] | None = None


class IntegrationReply(BaseModel):
    """What a service answers a tap with: the message's new text and buttons,
    and an optional toast for the person who tapped."""

    text: str = Field(min_length=1, max_length=MAX_TEXT)
    buttons: list[IntegrationButton] = Field(default_factory=list, max_length=MAX_BUTTONS)
    toast: str | None = Field(default=None, max_length=200)
    parse_mode: Literal["HTML", "MarkdownV2"] | None = None


class IntegrationError(Exception):
    """A post that cannot be carried out, with the HTTP status to answer."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def sign(secret: str, body: bytes) -> str:
    """The :data:`SIGNATURE_HEADER` value for ``body``. A service checks it with
    the same HMAC and a constant-time compare."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _note(text: str, parse_mode: str | None) -> str:
    """Escape a line Balam adds under a service's text, for that text's mode."""
    if parse_mode == "MarkdownV2":
        return escape_markdown_v2(text)
    if parse_mode == "HTML":
        return html.escape(text, quote=False)
    return text


def _keyboard(buttons: list[IntegrationButtonRow]) -> InlineKeyboardMarkup | None:
    """One button per row: labels like "Approve S$1,500 deposit" need the width."""
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(b.label, callback_data=f"{CALLBACK_PREFIX}{b.token}")]
            for b in buttons
        ]
    )


def _decode_image(image: str) -> bytes:
    try:
        data = base64.b64decode(image, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise IntegrationError(422, "image must be base64") from exc
    if not data:
        raise IntegrationError(422, "image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise IntegrationError(422, f"image is larger than {MAX_IMAGE_BYTES} bytes")
    return data


def _thread_gone(exc: BadRequest) -> bool:
    """Someone deleted the integration's topic since Balam made it."""
    return "thread not found" in str(exc).lower()


def _who(user: Any) -> str:
    return getattr(user, "full_name", None) or getattr(user, "first_name", None) or str(user.id)


class Integrations:
    """The relay between configured services and Telegram (ADR-0018).

    Messages go to the workspace chat: the forum supergroup when
    ``ALLOWED_TELEGRAM_CHAT_ID`` is set (``forum=True``, and topics apply), else
    the owner's private chat (topics are ignored there).
    """

    def __init__(
        self,
        configs: dict[str, IntegrationConfig],
        store: SessionStore,
        bot: Any,
        chat_id: int,
        *,
        forum: bool,
        timeout_s: float = TAP_TIMEOUT_S,
        answer_within_s: float = ANSWER_WITHIN_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._configs = configs
        self._store = store
        self._bot = bot
        self._chat_id = chat_id
        self._forum = forum
        self._timeout_s = timeout_s
        self._answer_within_s = answer_within_s
        # Tests hand in an httpx.MockTransport standing in for the service.
        self._transport = transport
        # Two posts naming a new topic at once must not make it twice.
        self._topic_lock = asyncio.Lock()
        # Tap deliveries run off the update loop (see tap); anchored here so the
        # loop's weak references cannot drop one mid-flight.
        self._tasks: set[asyncio.Task] = set()

    def get(self, name: str) -> IntegrationConfig | None:
        return self._configs.get(name)

    def secret_matches(self, name: str, presented: str) -> bool:
        cfg = self._configs.get(name)
        if cfg is None or not presented:
            return False
        return hmac.compare_digest(presented.encode(), cfg.secret.get_secret_value().encode())

    async def drain(self, timeout_s: float | None = None) -> None:
        """Wait for every tap delivery in flight: in tests, and when Balam stops
        (:mod:`balam.app`), so a message is not left on "Working..." needlessly.
        With ``timeout_s``, give up after that long; what is left is cancelled
        with the loop and its message stays on "Working..." with no buttons."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while self._tasks:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                logger.warning("stopping with %s tap(s) still in flight", len(self._tasks))
                return
            await asyncio.wait(list(self._tasks), timeout=remaining)

    # --- post / edit ---------------------------------------------------------

    async def post(self, name: str, msg: IntegrationPost) -> dict[str, Any]:
        """Send a new message, or edit one this integration posted earlier."""
        cfg = self._configs.get(name)
        if cfg is None:
            raise IntegrationError(404, f"unknown integration {name!r}")
        if msg.message_id is not None:
            return await self._edit_posted(name, msg)
        return await self._post_new(name, cfg, msg)

    async def _post_new(
        self, name: str, cfg: IntegrationConfig, msg: IntegrationPost
    ) -> dict[str, Any]:
        topic = msg.topic or cfg.topic
        image = _decode_image(msg.image) if msg.image else None
        tokens = [secrets.token_urlsafe(12) for _ in msg.buttons]
        keyboard = _keyboard(self._button_rows(0, tokens, msg.buttons))

        for attempt in (1, 2):
            thread_id = await self._thread_for(topic)
            try:
                photo = None
                if image is not None:
                    photo = await self._bot.send_photo(
                        chat_id=self._chat_id, photo=image, **thread_kwargs(thread_id)
                    )
                sent = await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=msg.text,
                    parse_mode=msg.parse_mode,
                    reply_markup=keyboard,
                    **thread_kwargs(thread_id),
                )
                break
            except BadRequest as exc:
                if attempt == 1 and topic and thread_id is not None and _thread_gone(exc):
                    # The topic was deleted by hand: forget it and make a new one.
                    self._store.forget_integration_topic(self._chat_id, topic)
                    continue
                await self._unsend(photo)
                raise IntegrationError(502, f"Telegram refused the message: {exc}") from exc
            except TelegramError as exc:
                await self._unsend(photo)
                raise IntegrationError(502, f"Telegram refused the message: {exc}") from exc

        message_id = sent.message_id
        image_message_id = photo.message_id if photo is not None else None
        self._store.add_integration_message(
            IntegrationMessageRow(
                chat_id=self._chat_id,
                message_id=message_id,
                integration=name,
                thread_id=thread_id,
                text=msg.text,
                parse_mode=msg.parse_mode,
                image_message_id=image_message_id,
                created_at=int(time.time() * 1000),
            )
        )
        self._store.replace_integration_buttons(
            self._chat_id, message_id, self._button_rows(message_id, tokens, msg.buttons)
        )
        logger.info("integration %s posted message %s", name, message_id)
        return {
            "message_id": message_id,
            "chat_id": self._chat_id,
            "thread_id": thread_id,
            "image_message_id": image_message_id,
        }

    async def _unsend(self, photo: Any) -> None:
        """Take back a photo whose text was refused, so it is not left alone in
        the topic (it may be a customer's screenshot) and a retry does not add
        another copy. Best effort: the post has failed either way."""
        if photo is None:
            return
        try:
            await self._bot.delete_message(chat_id=self._chat_id, message_id=photo.message_id)
        except TelegramError:
            logger.warning("could not delete orphan photo %s", photo.message_id, exc_info=True)

    async def _edit_posted(self, name: str, msg: IntegrationPost) -> dict[str, Any]:
        assert msg.message_id is not None
        row = self._store.get_integration_message(self._chat_id, msg.message_id)
        if row is None or row.integration != name:
            raise IntegrationError(404, f"message {msg.message_id} was not posted by {name!r}")
        if msg.image is not None:
            raise IntegrationError(422, "an edit cannot change the image")
        buttons = self._button_rows(
            row.message_id, [secrets.token_urlsafe(12) for _ in msg.buttons], msg.buttons
        )
        # The old tokens die before Telegram is asked, so even if the edit fails
        # the old keyboard cannot be tapped.
        self._store.replace_integration_buttons(row.chat_id, row.message_id, buttons)
        try:
            await self._edit(row, msg.text, msg.parse_mode, buttons)
        except TelegramError as exc:
            raise IntegrationError(502, f"Telegram refused the edit: {exc}") from exc
        return {
            "message_id": row.message_id,
            "chat_id": row.chat_id,
            "thread_id": row.thread_id,
            "image_message_id": row.image_message_id,
        }

    async def _thread_for(self, topic: str | None) -> int | None:
        """The thread to post in: the named topic, made the first time it is
        needed, or ``None`` (General, or a private chat)."""
        if not topic or not self._forum:
            return None
        async with self._topic_lock:
            thread_id = self._store.integration_topic(self._chat_id, topic)
            if thread_id is not None:
                return thread_id
            try:
                created = await self._bot.create_forum_topic(chat_id=self._chat_id, name=topic)
            except TelegramError as exc:
                raise IntegrationError(
                    502,
                    f"could not make topic {topic!r}: {exc}. The bot needs the "
                    "'Manage Topics' admin right in a forum supergroup.",
                ) from exc
            self._store.set_integration_topic(self._chat_id, topic, created.message_thread_id)
            # The topic keeps the integration's name: without this, the first
            # message someone types in it would auto-rename it like any topic.
            self._store.mark_auto_named(self._chat_id, created.message_thread_id)
            logger.info("made integration topic %r (%s)", topic, created.message_thread_id)
            return created.message_thread_id

    def _button_rows(
        self, message_id: int, tokens: list[str], buttons: list[IntegrationButton]
    ) -> list[IntegrationButtonRow]:
        return [
            IntegrationButtonRow(
                token=token,
                chat_id=self._chat_id,
                message_id=message_id,
                position=i,
                label=b.label,
                action=b.action,
                data=json.dumps(b.data),
            )
            for i, (token, b) in enumerate(zip(tokens, buttons, strict=True))
        ]

    async def _edit(
        self,
        row: IntegrationMessageRow,
        text: str,
        parse_mode: str | None,
        buttons: list[IntegrationButtonRow],
    ) -> None:
        """Edit a posted message and remember its new text. Telegram refuses an
        edit that changes nothing; that is not a failure."""
        try:
            await self._bot.edit_message_text(
                chat_id=row.chat_id,
                message_id=row.message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=_keyboard(buttons),
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        self._store.set_integration_message_text(row.chat_id, row.message_id, text, parse_mode)

    # --- tap -------------------------------------------------------------------

    async def tap(self, query: Any) -> asyncio.Task | None:
        """Handle a tap the trust boundary already let through.

        Everything up to "Working..." happens here, before returning: the
        ``tappers`` check, spending the buttons, and the edit that removes them.
        The delivery to the service runs as a task so a slow service never stalls
        the bot's update loop; it is returned for tests to await.
        """
        token = (query.data or "")[len(CALLBACK_PREFIX) :]
        button = self._store.get_integration_button(token)
        if button is None:
            await query.answer("This has already been handled.")
            return None
        row = self._store.get_integration_message(button.chat_id, button.message_id)
        message = getattr(query, "message", None)
        if row is None or (
            message is not None
            and (message.chat.id != row.chat_id or message.message_id != row.message_id)
        ):
            await query.answer("This button is not valid here.")
            return None
        cfg = self._configs.get(row.integration)
        if cfg is None:
            await query.answer(f"{row.integration} is no longer set up in Balam.")
            return None
        user = query.from_user
        if cfg.tappers and user.id not in cfg.tappers:
            await query.answer("Someone else needs to tap this one.")
            return None
        # The one step that makes a second tap impossible: synchronous, no await.
        if self._store.claim_integration_button(token) is None:
            await query.answer("This has already been handled.")
            return None

        who = _who(user)
        logger.info("integration %s: %s tapped %s", row.integration, user.id, button.action)
        working = f"{row.text}\n\n" + _note(
            f"⏳ Working… ({who} tapped {button.label})", row.parse_mode
        )
        try:
            await self._bot.edit_message_text(
                chat_id=row.chat_id,
                message_id=row.message_id,
                text=working,
                parse_mode=row.parse_mode,
                reply_markup=None,
            )
        except TelegramError:
            # The buttons are already spent in the store; a stale keyboard left
            # on screen only answers "already handled".
            logger.warning("could not show Working… on message %s", row.message_id, exc_info=True)

        payload = {
            "action": button.action,
            "data": json.loads(button.data),
            "user": {"id": user.id, "name": who},
            "message_id": row.message_id,
        }
        task = asyncio.create_task(self._deliver(query, row, cfg, payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _deliver(
        self,
        query: Any,
        row: IntegrationMessageRow,
        cfg: IntegrationConfig,
        payload: dict[str, Any],
    ) -> None:
        """POST the tap to the service and put its answer on the message. On any
        failure the message says so and gets no buttons back: the service may
        have acted, so nothing is offered twice."""
        call = asyncio.ensure_future(self._call(cfg, payload))
        done, _ = await asyncio.wait({call}, timeout=self._answer_within_s)
        answered = not done
        if answered:
            # Clear the spinner while Telegram still takes an answer.
            await self._answer(query, None)
        result = await call
        toast: str | None
        try:
            if isinstance(result, IntegrationReply):
                buttons = self._button_rows(
                    row.message_id,
                    [secrets.token_urlsafe(12) for _ in result.buttons],
                    result.buttons,
                )
                self._store.replace_integration_buttons(row.chat_id, row.message_id, buttons)
                await self._edit(row, result.text, result.parse_mode, buttons)
                toast = result.toast
            else:
                logger.warning("integration %s failed a tap: %s", row.integration, result)
                note = (
                    f"⚠️ {row.integration} failed: {result}. The buttons are not "
                    "restored, so nothing can run twice. Check it before acting on this."
                )
                await self._edit(
                    row, f"{row.text}\n\n{_note(note, row.parse_mode)}", row.parse_mode, []
                )
                toast = f"{row.integration} failed."
        except Exception:
            # Typically Telegram refusing the service's text (bad markup). The
            # message is left on "Working..." with no live buttons, never with
            # buttons that could run the action again.
            logger.exception("could not update message %s after a tap", row.message_id)
            self._store.replace_integration_buttons(row.chat_id, row.message_id, [])
            toast = None
        if not answered:
            await self._answer(query, toast)

    @staticmethod
    async def _answer(query: Any, text: str | None) -> None:
        try:
            await query.answer(text)
        except TelegramError:
            # The message edit is what carries the outcome; a lost answer only
            # loses the toast.
            logger.debug("could not answer the tap", exc_info=True)

    async def _call(
        self, cfg: IntegrationConfig, payload: dict[str, Any]
    ) -> IntegrationReply | str:
        """Deliver one tap. Returns the parsed reply, or why it failed."""
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(cfg.secret.get_secret_value(), body),
        }
        # trust_env=False: the url is loopback, and a proxy from the environment
        # must never carry a signed tap off this machine.
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport, trust_env=False
            ) as client:
                response = await client.post(cfg.url, content=body, headers=headers)
        except httpx.TimeoutException:
            return "it did not answer in time"
        except httpx.HTTPError:
            return "it could not be reached"
        if not response.is_success:
            return f"it answered HTTP {response.status_code}"
        try:
            return IntegrationReply.model_validate(response.json())
        except (ValueError, ValidationError):
            return "its answer could not be read"


async def handle_integration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on an integration's button (``int:<token>``).

    ``CallbackQueryHandler`` carries no filter, so the ADR-0008 trust boundary is
    re-checked here first, exactly as for approvals: an allowed user, in the
    configured chat. Anyone else gets a bare answer and nothing happens.
    """
    query = update.callback_query
    if query is None or not (query.data or "").startswith(CALLBACK_PREFIX):
        return
    config: Config = context.application.bot_data["config"]
    integrations: Integrations | None = context.application.bot_data.get("integrations")
    if integrations is None or not callback_authorized(query, config):
        await query.answer()
        return
    await integrations.tap(query)


def tapper_problems(
    configs: dict[str, IntegrationConfig], allowed_user_ids: tuple[int, ...]
) -> list[str]:
    """Tappers outside the allowlist could never tap; say so at boot rather than
    leave someone wondering why their taps do nothing."""
    return [
        f"integrations.{name}.tappers: {uid} is not an allowed Telegram user "
        "(ALLOWED_TELEGRAM_USER_ID / ADDITIONAL_TELEGRAM_USER_IDS)"
        for name, cfg in configs.items()
        for uid in cfg.tappers
        if uid not in allowed_user_ids
    ]
