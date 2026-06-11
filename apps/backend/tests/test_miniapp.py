"""Tests for Mini App launch links and buttons (balam.miniapp)."""

from __future__ import annotations

from collections.abc import Callable

from balam.config import Config
from balam.content_store import ContentStore
from balam.miniapp import (
    make_plan_view_button,
    markdown_button,
    markdown_start_param,
    mini_app_reply,
)


def test_markdown_start_param_fits_telegram_budget() -> None:
    param = markdown_start_param("0123456789ab")
    assert param == "markdown__c_0123456789ab"
    assert len(param) <= 64
    assert all(c.isalnum() or c in "_-" for c in param)


def test_markdown_button_direct_link(make_config: Callable[..., Config]) -> None:
    config = make_config(
        balam_public_url="https://balam.example.com", balam_miniapp_shortname="balamapp"
    )
    button = markdown_button(config, "balam_bot", "0123456789ab", "📋 View plan")
    assert button is not None
    assert button.url == "https://t.me/balam_bot/balamapp?startapp=markdown__c_0123456789ab"


def test_markdown_button_plain_url_without_shortname(make_config: Callable[..., Config]) -> None:
    config = make_config(balam_public_url="https://balam.example.com")
    button = markdown_button(config, "balam_bot", "0123456789ab", "📖 Preview")
    assert button is not None
    assert button.url == "https://balam.example.com/?view=markdown&content=0123456789ab"
    assert button.web_app is None  # web_app buttons are rejected in groups


def test_markdown_button_none_without_public_url(make_config: Callable[..., Config]) -> None:
    assert markdown_button(make_config(), "balam_bot", "0123456789ab", "x") is None


def test_make_plan_view_button_stores_and_links(make_config: Callable[..., Config]) -> None:
    config = make_config(
        balam_public_url="https://balam.example.com", balam_miniapp_shortname="balamapp"
    )
    store = ContentStore()
    plan_view = make_plan_view_button(config, store, "balam_bot")
    button = plan_view("plan.md", "# The plan")
    assert button is not None
    content_id = button.url.rsplit("markdown__c_", 1)[1]
    entry = store.get(content_id)
    assert entry is not None
    assert entry.title == "plan.md"
    assert entry.content == "# The plan"


def test_mini_app_reply_direct_link(make_config: Callable[..., Config]) -> None:
    # The /diff transport moved here from bot.py unchanged: direct link preferred.
    config = make_config(
        balam_public_url="https://balam.example.com", balam_miniapp_shortname="balamapp"
    )
    text, keyboard = mini_app_reply(
        config, "diff", "balam", bot_username="balam_bot", is_private=False
    )
    assert "balam" in text
    assert keyboard is not None
    button = keyboard.inline_keyboard[0][0]
    assert button.url == "https://t.me/balam_bot/balamapp?startapp=diff__balam"


def test_mini_app_reply_localhost_text_only(make_config: Callable[..., Config]) -> None:
    text, keyboard = mini_app_reply(
        make_config(), "diff", "balam", bot_username=None, is_private=False
    )
    assert keyboard is None
    assert "BALAM_PUBLIC_URL" in text


def test_mini_app_reply_context_free_direct_link(make_config: Callable[..., Config]) -> None:
    # A context-free view (/browser) sends the bare view as start_param: a
    # placeholder would land in the frontend's shared launch context and break
    # the other views (the diff view 404s on an unknown context name).
    config = make_config(
        balam_public_url="https://balam.example.com", balam_miniapp_shortname="balamapp"
    )
    text, keyboard = mini_app_reply(
        config, "browser", None, bot_username="balam_bot", is_private=False, heading="Live:"
    )
    assert text == "Live:"
    button = keyboard.inline_keyboard[0][0]
    assert button.url == "https://t.me/balam_bot/balamapp?startapp=browser"


def test_mini_app_reply_context_free_url_has_no_context_param(
    make_config: Callable[..., Config],
) -> None:
    config = make_config(balam_public_url="https://balam.example.com")
    _text, keyboard = mini_app_reply(
        config, "browser", None, bot_username=None, is_private=True, heading="Live:"
    )
    button = keyboard.inline_keyboard[0][0]
    assert button.web_app.url == "https://balam.example.com/?view=browser"
