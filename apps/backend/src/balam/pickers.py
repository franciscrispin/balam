"""The paged multi-select picker shared by ``/delete`` and ``/schedule cancel``.

Both commands ask the same question — "which of these rows do you want to act
on?" — over a list too long for one keyboard. The list, the confirm verb and the
callback-data prefixes differ; the checkbox drawing, paging and toggle handling
do not. :class:`PickerStyle` carries the four callback prefixes and the confirm
label, so one implementation serves both and a third picker costs a constant.

Selections live in a :class:`~balam.approvals.PendingPicks` snapshot keyed by a
token, so a stale keyboard from an earlier invocation can never mutate the
current one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from balam.approvals import PendingPicks
from balam.auth import callback_authorized
from balam.config import Config
from balam.telegram_utils import clear_keyboard

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PickerStyle:
    """What distinguishes one paged multi-select picker from another: its four
    callback prefixes and its confirm button's label. ``/delete`` and ``/schedule
    cancel`` share the keyboard, the paging, and :class:`PendingPicks`; only these
    differ, plus what the confirm handler does with the chosen ids."""

    toggle: str
    page: str
    confirm: str
    cancel: str
    confirm_label: str


#: Distinct prefixes per picker — PTB dispatches a callback to the first pattern
#: that matches, so two pickers must never share one.
DELETE_PICKER = PickerStyle("del", "delp", "deld", "delx", "🗑 Delete selected")
SCHEDULE_PICKER = PickerStyle("sch", "schp", "schd", "schx", "🗑 Cancel selected")


def picker_keyboard(
    style: PickerStyle,
    token: str,
    entries: list[tuple[int, str, bool]],
    page: int = 0,
    page_count: int = 1,
    selected_count: int = 0,
) -> InlineKeyboardMarkup:
    """Checklist for the current page (``<toggle>:<token>:<id>``), a Prev/Next
    navigation row when the snapshot spans more than one page
    (``<page>:<token>:<page>``), and the confirm/cancel row. ``selected_count``
    spans the whole snapshot, so the confirm button reflects picks made on other
    pages."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{'☑️' if selected else '☐'} {label}",
                callback_data=f"{style.toggle}:{token}:{item_id}",
            )
        ]
        for item_id, label, selected in entries
    ]
    if page_count > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀ Prev", callback_data=f"{style.page}:{token}:{page - 1}")
            )
        # The indicator points at the current page, so tapping it is a harmless no-op.
        nav.append(
            InlineKeyboardButton(
                f"Page {page + 1}/{page_count}", callback_data=f"{style.page}:{token}:{page}"
            )
        )
        if page < page_count - 1:
            nav.append(
                InlineKeyboardButton("Next ▶", callback_data=f"{style.page}:{token}:{page + 1}")
            )
        rows.append(nav)
    confirm_label = style.confirm_label
    if selected_count:
        confirm_label += f" ({selected_count})"
    rows.append(
        [
            InlineKeyboardButton(confirm_label, callback_data=f"{style.confirm}:{token}"),
            InlineKeyboardButton("Cancel", callback_data=f"{style.cancel}:{token}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def picker_markup(
    style: PickerStyle, picks: PendingPicks, token: str
) -> InlineKeyboardMarkup | None:
    """Build a picker keyboard from current snapshot state, or ``None`` if the
    token expired."""
    entries = picks.entries(token)
    info = picks.page_info(token)
    if entries is None or info is None:
        return None
    page, page_count, _total, selected = info
    return picker_keyboard(style, token, entries, page, page_count, selected)


async def handle_picker_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE, style: PickerStyle, bot_data_key: str
) -> None:
    """Toggle an item's checkbox (``<toggle>:<token>:<id>``). Shared by both
    pickers — only the prefix set and which ``bot_data`` snapshot to read differ."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith(f"{style.toggle}:"):
        return
    config: Config = context.application.bot_data["config"]
    if not callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("Malformed selection.")
        return
    _, token, item_id_raw = parts
    try:
        item_id = int(item_id_raw)
    except ValueError:
        await query.answer("Malformed selection.")
        return

    picks: PendingPicks = context.application.bot_data[bot_data_key]
    state = picks.toggle(token, item_id)
    if state is None:
        await query.answer("This picker has expired.")
        await clear_keyboard(query)
        return
    await query.answer("Selected." if state else "Unselected.")
    await refresh_picker(query, style, picks, token)


async def handle_picker_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE, style: PickerStyle, bot_data_key: str
) -> None:
    """Flip a picker to another page (``<page>:<token>:<page>``). Selections are
    kept in the snapshot, so paging never loses what's already checked."""
    query = update.callback_query
    if query is None or not (query.data or "").startswith(f"{style.page}:"):
        return
    config: Config = context.application.bot_data["config"]
    if not callback_authorized(query, config):
        await query.answer()
        return

    parts = (query.data or "").split(":", 2)
    if len(parts) != 3:
        await query.answer("Malformed request.")
        return
    _, token, page_raw = parts
    try:
        page = int(page_raw)
    except ValueError:
        await query.answer("Malformed request.")
        return

    picks: PendingPicks = context.application.bot_data[bot_data_key]
    if picks.set_page(token, page) is None:
        await query.answer("This picker has expired.")
        await clear_keyboard(query)
        return
    await query.answer()
    await refresh_picker(query, style, picks, token)


async def refresh_picker(query: Any, style: PickerStyle, picks: PendingPicks, token: str) -> None:
    """Redraw a picker's keyboard in place; a failed edit is cosmetic only."""
    markup = picker_markup(style, picks, token)
    message = getattr(query, "message", None)
    if markup is None or message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except Exception:
        logger.debug("failed to refresh %s keyboard", style.toggle, exc_info=True)
