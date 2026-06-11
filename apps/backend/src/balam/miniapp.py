"""Mini App launch links and buttons (ADR-0003/0013).

Single decision point for how a Mini App view reaches the user. The transport
preference (direct ``t.me/<bot>/<shortname>?startapp=…`` link → ``web_app``
button → plain URL → bare localhost text) exists because Telegram rejects
``web_app`` inline buttons in groups, and Balam lives in a forum supergroup.

The direct-link ``start_param`` is capped at 64 chars of ``[A-Za-z0-9_-]``, so
markdown content travels as a short id (``markdown__c_<id>``) resolved by
``GET /api/markdown/content/{id}`` from :class:`balam.content_store.ContentStore`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from balam.config import Config
from balam.content_store import ContentStore

logger = logging.getLogger(__name__)


def mini_app_url(config: Config, view: str, query: str) -> str:
    """The browser/``web_app`` URL for ``view`` with a pre-encoded ``query`` tail.

    Uses the public HTTPS base when configured (``BALAM_PUBLIC_URL``), else the
    local ``127.0.0.1`` address — which Telegram won't open in-app (ADR-0007) but
    the owner can open in a browser.
    """
    base = config.balam_public_url or f"http://127.0.0.1:{config.balam_port}"
    return f"{base}/?view={view}&{query}"


def mini_app_reply(
    config: Config,
    view: str,
    context_name: str,
    *,
    bot_username: str | None,
    is_private: bool,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """How to surface a Mini App ``view`` bound to ``context_name`` for this chat.

    Returns ``(text, reply_markup)``. Preference order (ADR-0013):

    1. **Direct Mini App link** — opens in Telegram's webview (signed initData) in
       ANY chat type, so it works in the workspace supergroup where ``web_app``
       inline buttons are rejected. Needs a BotFather short name + the bot username;
       ``start_param`` carries view+context as ``"view__context"``.
    2. **``web_app`` button** — also opens in-Telegram (initData, ADR-0008), but
       Telegram allows it ONLY in private chats.
    3. **Plain URL** — opens in the external browser (HTTPS public base set).
    4. **Local URL** — no public base; localhost link Telegram won't open in-app.
    """
    is_public = config.balam_public_url is not None
    shortname = config.balam_miniapp_shortname

    if is_public and shortname and bot_username:
        start_param = f"{view}__{context_name}"
        link = f"https://t.me/{bot_username}/{shortname}?startapp={quote(start_param)}"
        button = InlineKeyboardButton("View changes", url=link)
        return f"Changes in {context_name}:", InlineKeyboardMarkup([[button]])

    url = mini_app_url(config, view, f"context={quote(context_name)}")

    if is_public and is_private:
        button = InlineKeyboardButton("View changes", web_app=WebAppInfo(url=url))
        return f"Changes in {context_name}:", InlineKeyboardMarkup([[button]])

    if is_public:
        # In groups a web_app inline button is rejected (Button_type_invalid); a
        # plain URL button opens in the external browser instead (no initData
        # there, so the Mini App relies on the owner's Telegram session).
        button = InlineKeyboardButton("View changes", url=url)
        text = (
            f"Changes in {context_name}: {url}\n\n"
            "Opens in your browser. (Telegram only allows the in-app Mini App button "
            "in a private chat with the bot.)"
        )
        return text, InlineKeyboardMarkup([[button]])

    text = (
        f"Diff viewer for {context_name}:\n{url}\n\n"
        "Opens in a browser. To open inside Telegram, serve the Mini App from "
        "a public HTTPS URL and set BALAM_PUBLIC_URL (ADR-0013)."
    )
    return text, None


def markdown_start_param(content_id: str) -> str:
    """The ``startapp`` param opening the markdown view on a stored snapshot.

    The ``c_`` prefix marks the second token as a content id (the frontend's
    ``resolveLaunch`` otherwise reads it as a context name, as ``/diff`` does).
    """
    return f"markdown__c_{content_id}"


def markdown_button(
    config: Config,
    bot_username: str | None,
    content_id: str,
    label: str,
) -> InlineKeyboardButton | None:
    """A button opening the Mini App markdown view on a stored snapshot.

    Same transport preference as :func:`mini_app_reply`, minus the ``web_app``
    branch — these buttons ride messages in the workspace group, where Telegram
    rejects ``web_app`` buttons outright (sending one would fail the whole
    message). Returns ``None`` when there is no public URL: a localhost link in
    a button is dead weight, and the content stays reachable once ADR-0013 is
    configured.
    """
    is_public = config.balam_public_url is not None
    shortname = config.balam_miniapp_shortname

    if is_public and shortname and bot_username:
        start_param = markdown_start_param(content_id)
        link = f"https://t.me/{bot_username}/{shortname}?startapp={quote(start_param)}"
        return InlineKeyboardButton(label, url=link)

    if is_public:
        url = mini_app_url(config, "markdown", f"content={quote(content_id)}")
        return InlineKeyboardButton(label, url=url)

    return None


def make_plan_view_button(
    config: Config,
    content_store: ContentStore,
    bot_username: str | None,
) -> Callable[[str, str], InlineKeyboardButton | None]:
    """A ``(title, content) -> button`` factory for the streamer's plan handling.

    Bundles config + store + username so the streamer needs one injectable
    callable (trivially faked in tests): it stores the markdown snapshot and
    returns the launch button, or ``None`` when no public URL is configured.
    """

    def plan_view(title: str, content: str) -> InlineKeyboardButton | None:
        content_id = content_store.put(title, content)
        return markdown_button(config, bot_username, content_id, "📋 View plan")

    return plan_view
