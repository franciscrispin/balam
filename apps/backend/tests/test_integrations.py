"""Tests for integrations (ADR-0018): posting through Balam and relaying taps.

The service is faked with an ``httpx.MockTransport`` and Telegram with a small
recording bot, so every path runs offline. The trust boundary is the real one
(:func:`balam.auth.callback_authorized`), reached through the real handler.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler

from balam.bot import build_application
from balam.config import Config
from balam.contexts import ContextConfig, ContextsConfig, IntegrationConfig
from balam.integrations import (
    CALLBACK_PREFIX,
    SIGNATURE_HEADER,
    IntegrationError,
    IntegrationPost,
    Integrations,
    handle_integration_callback,
    tapper_problems,
)
from balam.router import Router, TopicRef
from balam.server import create_app
from balam.store import SessionStore
from balam.topics import auto_name_topic

OWNER = 424242
GUEST = 515151
STRANGER = 999999
SUPERGROUP = -1001234567890
SECRET = "s3cret-for-tests-only-0123456789"


# --- fakes ----------------------------------------------------------------------


class FakeBot:
    """Records what Balam asks Telegram to do; message ids count up from 100."""

    def __init__(self) -> None:
        self._next_id = 100
        self._next_thread = 700
        self.sent: list[dict[str, Any]] = []
        self.photos: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.topics: list[str] = []
        self.deleted: list[int] = []
        self.renamed: list[str] = []
        #: When set, every send_message is refused with this BadRequest text.
        self.refuse_text: str | None = None
        #: Thread ids that fail as "deleted" on the next send.
        self.gone_threads: set[int] = set()

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def create_forum_topic(self, *, chat_id: int, name: str) -> SimpleNamespace:
        self.topics.append(name)
        self._next_thread += 1
        return SimpleNamespace(message_thread_id=self._next_thread, name=name)

    async def send_photo(self, *, chat_id: int, photo: bytes, **kwargs: Any) -> SimpleNamespace:
        self.photos.append({"chat_id": chat_id, "photo": photo, **kwargs})
        return SimpleNamespace(message_id=self._id())

    async def send_message(self, *, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        thread = kwargs.get("message_thread_id")
        if thread in self.gone_threads:
            self.gone_threads.discard(thread)
            raise BadRequest("Message thread not found")
        if self.refuse_text is not None:
            raise BadRequest(self.refuse_text)
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=self._id())

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)

    async def delete_message(self, *, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)

    async def edit_forum_topic(self, *, chat_id: int, message_thread_id: int, name: str) -> None:
        self.renamed.append(name)


class FakeService:
    """The integration: answers taps through an httpx.MockTransport."""

    def __init__(self, reply: Callable[[dict], httpx.Response] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.signatures_ok: list[bool] = []
        self._reply = reply or (lambda body: httpx.Response(200, json={"text": "Recorded."}))
        self.release = asyncio.Event()
        self.release.set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.signatures_ok.append(
            hmac.compare_digest(request.headers.get(SIGNATURE_HEADER, ""), expected)
        )
        payload = json.loads(body)
        self.calls.append(payload)
        await self.release.wait()
        return self._reply(payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeQuery:
    def __init__(self, data: str, user_id: int, chat_id: int, message_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, full_name=f"User {user_id}")
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, **_: object) -> None:
        self.answers.append(text)


def _integration(**overrides: Any) -> IntegrationConfig:
    base: dict[str, Any] = {
        "url": "http://127.0.0.1:8770/balam",
        "secret": "${TEST_INTEGRATION_SECRET}",
        "topic": "Payments",
    }
    base.update(overrides)
    return IntegrationConfig.model_validate(base)


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_INTEGRATION_SECRET", SECRET)


def _setup(
    service: FakeService | None = None,
    *,
    tappers: list[int] | None = None,
    forum: bool = True,
    timeout_s: float = 5.0,
    answer_within_s: float = 5.0,
) -> tuple[Integrations, FakeBot, SessionStore, FakeService]:
    service = service or FakeService()
    bot = FakeBot()
    store = SessionStore(":memory:")
    integrations = Integrations(
        {"payments": _integration(tappers=tappers or [])},
        store,
        bot,
        SUPERGROUP if forum else OWNER,
        forum=forum,
        timeout_s=timeout_s,
        answer_within_s=answer_within_s,
        transport=service.transport,
    )
    return integrations, bot, store, service


def _card(**overrides: Any) -> IntegrationPost:
    base: dict[str, Any] = {
        "text": "Record S$1,500 deposit on INV900?",
        "buttons": [
            {"label": "Approve", "action": "approve", "data": {"match": 7}},
            {"label": "Not this one", "action": "reject", "data": {"match": 7}},
        ],
    }
    base.update(overrides)
    return IntegrationPost.model_validate(base)


def _callback_data(bot: FakeBot, index: int = 0, *, message: int = -1) -> str:
    markup = bot.sent[message]["reply_markup"]
    return markup.inline_keyboard[index][0].callback_data


def _env(integrations: Integrations, *, user_ids: tuple[int, ...] = (OWNER, GUEST)):
    config = SimpleNamespace(
        allowed_telegram_user_id=OWNER,
        allowed_user_ids=user_ids,
        allowed_telegram_chat_id=SUPERGROUP,
    )
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"config": config, "integrations": integrations})
    )


async def _tap(
    integrations: Integrations, data: str, user_id: int, message_id: int, chat_id: int = SUPERGROUP
) -> FakeQuery:
    query = FakeQuery(data, user_id, chat_id, message_id)
    await handle_integration_callback(SimpleNamespace(callback_query=query), _env(integrations))
    return query


# --- config ---------------------------------------------------------------------


def test_secret_is_read_from_the_environment() -> None:
    cfg = _integration()
    assert cfg.secret.get_secret_value() == SECRET
    assert SECRET not in repr(cfg)


def test_a_literal_secret_in_config_yaml_is_refused() -> None:
    with pytest.raises(ValidationError, match="from .env"):
        _integration(secret=SECRET)


def test_a_secret_mixing_literal_text_and_a_variable_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMPTY_VAR", "")
    with pytest.raises(ValidationError, match="single"):
        _integration(secret="literal-secret-in-yaml${EMPTY_VAR}")
    with pytest.raises(ValidationError, match="single"):
        _integration(secret="${TEST_INTEGRATION_SECRET}${EMPTY_VAR}")


def test_a_short_secret_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_INTEGRATION_SECRET", "short")
    with pytest.raises(ValidationError, match="at least 16"):
        _integration()


def test_an_unset_secret_variable_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_INTEGRATION_SECRET")
    with pytest.raises(ValidationError, match="not set"):
        _integration()


@pytest.mark.parametrize(
    "url", ["https://example.com/hook", "http://10.0.0.5:8770/x", "ftp://127.0.0.1/x"]
)
def test_only_local_urls_are_accepted(url: str) -> None:
    with pytest.raises(ValidationError, match="this machine"):
        _integration(url=url)


def test_integrations_block_is_optional_and_names_are_checked() -> None:
    contexts = {"balam": ContextConfig(directory="/tmp", description="t")}
    assert ContextsConfig(default_context="balam", contexts=contexts).integrations == {}
    with pytest.raises(ValidationError, match="integration name"):
        ContextsConfig(
            default_context="balam",
            contexts=contexts,
            integrations={"pay ments": _integration()},
        )


def test_tappers_must_be_on_the_allowlist() -> None:
    configs = {"payments": _integration(tappers=[OWNER, STRANGER])}
    problems = tapper_problems(configs, (OWNER, GUEST))
    assert len(problems) == 1 and str(STRANGER) in problems[0]
    assert tapper_problems({"payments": _integration(tappers=[GUEST])}, (OWNER, GUEST)) == []


# --- post -----------------------------------------------------------------------


async def test_post_makes_the_topic_once_and_stores_the_buttons() -> None:
    integrations, bot, store, _ = _setup()

    first = await integrations.post("payments", _card())
    second = await integrations.post("payments", _card())

    assert bot.topics == ["Payments"]
    assert first["thread_id"] == second["thread_id"] == bot.sent[0]["message_thread_id"]
    buttons = store.integration_buttons(SUPERGROUP, first["message_id"])
    assert [(b.label, b.action, json.loads(b.data)) for b in buttons] == [
        ("Approve", "approve", {"match": 7}),
        ("Not this one", "reject", {"match": 7}),
    ]
    for i in (0, 1):
        data = _callback_data(bot, i, message=0)
        assert data.startswith(CALLBACK_PREFIX)
        assert len(data.encode()) <= 64


async def test_post_names_its_own_topic_or_none() -> None:
    integrations, bot, _, _ = _setup()
    await integrations.post("payments", _card(topic="Exceptions"))
    assert bot.topics == ["Exceptions"]


async def test_post_in_a_private_chat_ignores_topics() -> None:
    integrations, bot, _, _ = _setup(forum=False)
    result = await integrations.post("payments", _card())
    assert bot.topics == []
    assert result["thread_id"] is None
    assert "message_thread_id" not in bot.sent[0]


async def test_post_with_an_image_sends_the_photo_then_the_card() -> None:
    integrations, bot, _, _ = _setup()
    image = base64.b64encode(b"\x89PNG fake").decode()
    result = await integrations.post("payments", _card(image=image))
    assert bot.photos[0]["photo"] == b"\x89PNG fake"
    assert result["image_message_id"] == 101
    assert result["message_id"] == 102


async def test_a_refused_card_takes_its_photo_back() -> None:
    integrations, bot, store, _ = _setup()
    bot.refuse_text = "Can't parse entities"
    image = base64.b64encode(b"\x89PNG fake").decode()

    for _ in range(3):
        with pytest.raises(IntegrationError) as exc:
            await integrations.post(
                "payments", _card(text="<b>bad", parse_mode="HTML", image=image)
            )
        assert exc.value.status == 502

    # Each retry's photo was deleted again, so none is left alone in the topic.
    assert len(bot.photos) == 3
    assert bot.deleted == [101, 102, 103]
    assert bot.sent == []


async def test_an_integration_topic_is_not_renamed_by_a_first_message(tmp_path: Path) -> None:
    integrations, bot, store, _ = _setup()
    contexts = ContextsConfig(
        default_context="balam",
        contexts={"balam": ContextConfig(directory=str(tmp_path), description="t")},
    )
    router = Router(store, None, contexts)  # type: ignore[arg-type]
    posted = await integrations.post("payments", _card())
    ref = TopicRef(chat_id=SUPERGROUP, thread_id=posted["thread_id"], title="Payments")

    await auto_name_topic(bot, router, ref, "balam", "why is INV900 still unpaid?")

    assert bot.renamed == []


async def test_post_rejects_an_image_that_is_not_base64() -> None:
    integrations, _, _, _ = _setup()
    with pytest.raises(IntegrationError) as exc:
        await integrations.post("payments", _card(image="not base64!"))
    assert exc.value.status == 422


async def test_a_deleted_topic_is_made_again() -> None:
    integrations, bot, store, _ = _setup()
    first = await integrations.post("payments", _card())
    bot.gone_threads.add(first["thread_id"])

    second = await integrations.post("payments", _card())

    assert bot.topics == ["Payments", "Payments"]
    assert second["thread_id"] != first["thread_id"]
    assert store.integration_topic(SUPERGROUP, "Payments") == second["thread_id"]


async def test_edit_replaces_text_and_buttons_and_kills_old_tokens() -> None:
    integrations, bot, store, service = _setup()
    posted = await integrations.post("payments", _card())
    old = _callback_data(bot)

    await integrations.post(
        "payments",
        _card(
            message_id=posted["message_id"],
            text="Now with the screenshot.",
            buttons=[{"label": "Approve", "action": "approve", "data": 8}],
        ),
    )

    edit = bot.edits[-1]
    assert edit["message_id"] == posted["message_id"]
    assert edit["text"] == "Now with the screenshot."
    assert [b.data for b in store.integration_buttons(SUPERGROUP, posted["message_id"])] == ["8"]
    query = await _tap(integrations, old, OWNER, posted["message_id"])
    assert query.answers == ["This has already been handled."]
    assert service.calls == []


async def test_edit_of_a_message_it_did_not_post_is_refused() -> None:
    integrations, _, _, _ = _setup()
    with pytest.raises(IntegrationError) as exc:
        await integrations.post("payments", _card(message_id=12345))
    assert exc.value.status == 404


# --- tap ------------------------------------------------------------------------


async def test_allowed_tap_reaches_the_service_once_and_applies_its_reply() -> None:
    service = FakeService(
        lambda body: httpx.Response(
            200,
            json={
                "text": "Recorded by User 424242.",
                "buttons": [{"label": "Undo", "action": "undo"}],
                "toast": "Done",
            },
        )
    )
    integrations, bot, store, _ = _setup(service)
    posted = await integrations.post("payments", _card())

    await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])
    await integrations.drain()

    assert service.calls == [
        {
            "action": "approve",
            "data": {"match": 7},
            "user": {"id": OWNER, "name": f"User {OWNER}"},
            "message_id": posted["message_id"],
        }
    ]
    assert service.signatures_ok == [True]
    final = bot.edits[-1]
    assert final["text"] == "Recorded by User 424242."
    assert [row[0].text for row in final["reply_markup"].inline_keyboard] == ["Undo"]
    assert [b.action for b in store.integration_buttons(SUPERGROUP, posted["message_id"])] == [
        "undo"
    ]


async def test_tap_removes_buttons_and_shows_working_before_the_service_answers() -> None:
    integrations, bot, _, service = _setup()
    service.release.clear()
    posted = await integrations.post("payments", _card())

    query = FakeQuery(_callback_data(bot), OWNER, SUPERGROUP, posted["message_id"])
    task = await integrations.tap(query)

    working = bot.edits[-1]
    assert working["reply_markup"] is None
    assert "Working…" in working["text"] and "tapped Approve" in working["text"]
    assert working["text"].startswith("Record S$1,500 deposit on INV900?")
    assert task is not None and not task.done()
    service.release.set()
    await integrations.drain()
    assert query.answers == [None]


async def test_second_tap_is_impossible() -> None:
    integrations, bot, store, service = _setup()
    service.release.clear()
    posted = await integrations.post("payments", _card())
    approve, reject = _callback_data(bot, 0), _callback_data(bot, 1)

    await _tap(integrations, approve, OWNER, posted["message_id"])
    # While the first is still with the service, and again after it answered,
    # neither the same button nor its sibling does anything.
    during = await _tap(integrations, reject, GUEST, posted["message_id"])
    service.release.set()
    await integrations.drain()
    after = await _tap(integrations, approve, OWNER, posted["message_id"])
    await integrations.drain()

    assert len(service.calls) == 1
    assert during.answers == after.answers == ["This has already been handled."]
    # The reply carried no buttons, so none are live.
    assert store.integration_buttons(SUPERGROUP, posted["message_id"]) == []


async def test_concurrent_taps_reach_the_service_once() -> None:
    integrations, bot, _, service = _setup()
    posted = await integrations.post("payments", _card())
    data = _callback_data(bot)

    await asyncio.gather(
        *(_tap(integrations, data, uid, posted["message_id"]) for uid in (OWNER, GUEST, OWNER))
    )
    await integrations.drain()

    assert len(service.calls) == 1


@pytest.mark.parametrize(
    ("user_id", "chat_id"),
    [(STRANGER, SUPERGROUP), (OWNER, -1009999999999)],
    ids=["stranger", "wrong-chat"],
)
async def test_disallowed_tap_does_nothing(user_id: int, chat_id: int) -> None:
    integrations, bot, store, service = _setup()
    posted = await integrations.post("payments", _card())
    edits_before = len(bot.edits)

    query = await _tap(integrations, _callback_data(bot), user_id, posted["message_id"], chat_id)
    await integrations.drain()

    assert query.answers == [None]
    assert service.calls == []
    assert len(bot.edits) == edits_before
    assert len(store.integration_buttons(SUPERGROUP, posted["message_id"])) == 2


async def test_allowed_user_who_is_not_a_tapper_is_turned_away() -> None:
    integrations, bot, store, service = _setup(tappers=[OWNER])
    posted = await integrations.post("payments", _card())

    query = await _tap(integrations, _callback_data(bot), GUEST, posted["message_id"])
    await integrations.drain()

    assert query.answers == ["Someone else needs to tap this one."]
    assert service.calls == []
    assert len(store.integration_buttons(SUPERGROUP, posted["message_id"])) == 2
    # The tapper still can.
    await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])
    await integrations.drain()
    assert len(service.calls) == 1


async def test_a_token_from_another_message_is_refused() -> None:
    integrations, bot, _, service = _setup()
    await integrations.post("payments", _card())
    other = await integrations.post("payments", _card())

    query = await _tap(integrations, _callback_data(bot, message=0), OWNER, other["message_id"])

    assert query.answers == ["This button is not valid here."]
    assert service.calls == []


def _failing(kind: str) -> Callable[[dict], httpx.Response]:
    def reply(_body: dict) -> httpx.Response:
        if kind == "500":
            return httpx.Response(500, text="boom")
        if kind == "garbage":
            return httpx.Response(200, text="not json")
        raise httpx.ConnectError("refused")

    return reply


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("500", "it answered HTTP 500"),
        ("garbage", "its answer could not be read"),
        ("down", "it could not be reached"),
    ],
)
async def test_a_failing_service_is_reported_and_nothing_is_tappable(
    kind: str, reason: str
) -> None:
    integrations, bot, store, service = _setup(FakeService(_failing(kind)))
    posted = await integrations.post("payments", _card())

    query = await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])
    await integrations.drain()

    final = bot.edits[-1]
    assert reason in final["text"]
    assert "payments failed" in final["text"]
    assert final["text"].startswith("Record S$1,500 deposit on INV900?")
    assert final["reply_markup"] is None
    assert store.integration_buttons(SUPERGROUP, posted["message_id"]) == []
    assert query.answers == ["payments failed."]
    again = await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])
    assert again.answers == ["This has already been handled."]
    assert len(service.calls) == 1


async def test_a_slow_service_times_out_into_the_failure_path() -> None:
    # MockTransport does not apply client timeouts, so the transport raises the
    # timeout httpx would.
    integrations, bot, _, _ = _setup()
    integrations._transport = httpx.MockTransport(_raise_timeout)
    posted = await integrations.post("payments", _card())

    await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])
    await integrations.drain()

    assert "it did not answer in time" in bot.edits[-1]["text"]


async def test_a_slow_reply_answers_the_tap_in_time_and_drops_its_toast() -> None:
    service = FakeService(lambda body: httpx.Response(200, json={"text": "Done.", "toast": "Ok"}))
    integrations, bot, _, _ = _setup(service, answer_within_s=0.01)
    service.release.clear()
    posted = await integrations.post("payments", _card())

    query = await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])
    await asyncio.sleep(0.05)
    # Telegram's spinner is cleared while the service is still working.
    assert query.answers == [None]
    service.release.set()
    await integrations.drain()

    assert bot.edits[-1]["text"] == "Done."
    assert query.answers == [None]


async def test_drain_gives_up_after_its_timeout() -> None:
    integrations, bot, _, service = _setup()
    service.release.clear()
    posted = await integrations.post("payments", _card())
    await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])

    await asyncio.wait_for(integrations.drain(timeout_s=0.05), timeout=2)

    assert "Working…" in bot.edits[-1]["text"]
    service.release.set()
    await integrations.drain()
    assert bot.edits[-1]["text"] == "Recorded."


def _raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


async def test_markdown_cards_get_escaped_notes() -> None:
    integrations, bot, _, service = _setup()
    service.release.clear()
    posted = await integrations.post("payments", _card(text="*Deposit*", parse_mode="MarkdownV2"))

    await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])

    working = bot.edits[-1]
    assert working["parse_mode"] == "MarkdownV2"
    assert r"\(User 424242 tapped Approve\)" in working["text"]
    service.release.set()
    await integrations.drain()


# --- the HTTP endpoint ----------------------------------------------------------


def _client(
    make_config: Callable[..., Config], integrations: Integrations, *, peer: str = "127.0.0.1"
) -> TestClient:
    contexts = ContextsConfig(
        default_context="balam",
        contexts={"balam": ContextConfig(directory="/tmp", description="t")},
    )
    router = Router(SessionStore(":memory:"), None, contexts)  # type: ignore[arg-type]
    app = create_app(make_config(), router, integrations=integrations)
    return TestClient(app, client=(peer, 50000))


def _bearer(secret: str = SECRET) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


_BODY = {"text": "Hello", "buttons": [{"label": "OK", "action": "ok"}]}


def test_endpoint_posts_with_the_right_secret(make_config: Callable[..., Config]) -> None:
    integrations, bot, _, _ = _setup()
    resp = _client(make_config, integrations).post(
        "/integrations/payments/messages", json=_BODY, headers=_bearer()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["message_id"] == 101
    assert bot.sent[0]["text"] == "Hello"


def test_endpoint_edits_a_posted_message(make_config: Callable[..., Config]) -> None:
    integrations, bot, _, _ = _setup()
    client = _client(make_config, integrations)
    posted = client.post("/integrations/payments/messages", json=_BODY, headers=_bearer()).json()
    resp = client.post(
        "/integrations/payments/messages",
        json={"text": "Edited", "message_id": posted["message_id"]},
        headers=_bearer(),
    )
    assert resp.status_code == 200
    assert bot.edits[-1]["text"] == "Edited"


@pytest.mark.parametrize("headers", [{}, _bearer("wrong-secret-wrong-secret"), {"X": "y"}])
def test_endpoint_refuses_a_bad_secret(
    make_config: Callable[..., Config], headers: dict[str, str]
) -> None:
    integrations, bot, _, _ = _setup()
    resp = _client(make_config, integrations).post(
        "/integrations/payments/messages", json=_BODY, headers=headers
    )
    assert resp.status_code == 401
    assert bot.sent == []


def test_endpoint_is_loopback_only(make_config: Callable[..., Config]) -> None:
    integrations, bot, _, _ = _setup()
    remote = _client(make_config, integrations, peer="203.0.113.9")
    assert (
        remote.post("/integrations/payments/messages", json=_BODY, headers=_bearer()).status_code
        == 404
    )
    tunnelled = _client(make_config, integrations).post(
        "/integrations/payments/messages",
        json=_BODY,
        headers={**_bearer(), "cf-connecting-ip": "203.0.113.9"},
    )
    assert tunnelled.status_code == 404
    assert bot.sent == []


def test_endpoint_unknown_integration_and_bad_body(make_config: Callable[..., Config]) -> None:
    integrations, _, _, _ = _setup()
    client = _client(make_config, integrations)
    assert (
        client.post("/integrations/other/messages", json=_BODY, headers=_bearer()).status_code
        == 404
    )
    bad = client.post(
        "/integrations/payments/messages",
        json={"text": "", "buttons": [{"label": "x"}]},
        headers=_bearer(),
    )
    assert bad.status_code == 422
    not_json = client.post("/integrations/payments/messages", content=b"{oops", headers=_bearer())
    assert not_json.status_code == 422


def test_endpoint_absent_without_integrations(make_config: Callable[..., Config]) -> None:
    contexts = ContextsConfig(
        default_context="balam",
        contexts={"balam": ContextConfig(directory="/tmp", description="t")},
    )
    router = Router(SessionStore(":memory:"), None, contexts)  # type: ignore[arg-type]
    client = TestClient(create_app(make_config(), router), client=("127.0.0.1", 50000))
    resp = client.post("/integrations/payments/messages", json=_BODY, headers=_bearer())
    assert resp.status_code in (404, 405)


# --- existing callbacks are unaffected --------------------------------------------


def test_int_prefix_routes_only_to_the_integration_handler(
    make_config: Callable[..., Config],
) -> None:
    app = build_application(
        make_config(allowed_telegram_chat_id=SUPERGROUP), backend=None, router=None
    )  # type: ignore[arg-type]
    handlers = [
        h for h in app.handlers[0] if isinstance(h, CallbackQueryHandler) and h.pattern is not None
    ]
    for data, expected in (
        ("appr:allow:t", "^appr:"),
        ("qst:t:0:1", "^qst:"),
        ("qstd:t:0", "^qstd:"),
        ("qstc:t:0", "^qstc:"),
        ("del:t:1", "^del:"),
        ("sch:t:1", "^sch:"),
        ("int:abc", "^int:"),
    ):
        first = next(h for h in handlers if h.pattern.match(data))
        assert first.pattern.pattern == expected, data
    integration = next(h for h in handlers if h.pattern.pattern == "^int:")
    assert integration.callback is handle_integration_callback


async def test_integration_handler_ignores_other_callbacks() -> None:
    integrations, _, _, service = _setup()
    query = await _tap(integrations, "appr:allow:token", OWNER, 1)
    assert query.answers == []
    assert service.calls == []


async def test_tap_without_integrations_configured_just_answers() -> None:
    query = FakeQuery("int:whatever", OWNER, SUPERGROUP, 1)
    config = SimpleNamespace(
        allowed_telegram_user_id=OWNER, allowed_user_ids=(OWNER,), allowed_telegram_chat_id=None
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": config}))
    await handle_integration_callback(SimpleNamespace(callback_query=query), context)
    assert query.answers == [None]


# --- over a real socket -------------------------------------------------------------


async def test_tap_reaches_a_real_local_service_on_an_ephemeral_port() -> None:
    """The same relay with no MockTransport: a FastAPI stand-in for the service,
    served by uvicorn on 127.0.0.1 with a port the OS picks."""
    received: list[tuple[dict, bool]] = []
    service = FastAPI()

    @service.post("/balam")
    async def balam(request: Request) -> dict:
        body = await request.body()
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        received.append((json.loads(body), request.headers.get(SIGNATURE_HEADER) == expected))
        return {"text": "Recorded.", "toast": "Recorded"}

    server = uvicorn.Server(
        uvicorn.Config(service, host="127.0.0.1", port=0, log_level="error", ws="none")
    )
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        bot = FakeBot()
        integrations = Integrations(
            {"payments": _integration(url=f"http://127.0.0.1:{port}/balam")},
            SessionStore(":memory:"),
            bot,
            SUPERGROUP,
            forum=True,
        )
        posted = await integrations.post("payments", _card())
        query = await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])
        await integrations.drain()
    finally:
        server.should_exit = True
        await serving

    assert received == [
        (
            {
                "action": "approve",
                "data": {"match": 7},
                "user": {"id": OWNER, "name": f"User {OWNER}"},
                "message_id": posted["message_id"],
            },
            True,
        )
    ]
    assert bot.edits[-1]["text"] == "Recorded."
    assert query.answers == ["Recorded"]


async def test_a_reply_telegram_refuses_leaves_nothing_tappable() -> None:
    service = FakeService(
        lambda body: httpx.Response(
            200,
            json={
                "text": "*bad",
                "parse_mode": "MarkdownV2",
                "buttons": [{"label": "Again", "action": "approve"}],
            },
        )
    )
    integrations, bot, store, _ = _setup(service)
    posted = await integrations.post("payments", _card())

    async def refuse_markdown(**kwargs: Any) -> None:
        if kwargs.get("parse_mode") == "MarkdownV2":
            raise BadRequest("Can't parse entities")
        bot.edits.append(kwargs)

    bot.edit_message_text = refuse_markdown  # type: ignore[method-assign]
    query = await _tap(integrations, _callback_data(bot), OWNER, posted["message_id"])
    await integrations.drain()

    assert "Working…" in bot.edits[-1]["text"]
    assert store.integration_buttons(SUPERGROUP, posted["message_id"]) == []
    assert query.answers == [None]
