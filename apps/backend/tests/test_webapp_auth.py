"""Tests for Mini App initData authentication (balam.webapp_auth, ADR-0008)."""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from balam.webapp_auth import (
    InitDataError,
    RequireUser,
    is_allowed_init_data,
    validate_init_data,
)
from conftest import BOT_TOKEN, OWNER_ID, make_init_data

_NOW = 1_700_000_000


def test_valid_init_data_returns_user() -> None:
    init = make_init_data(auth_date=_NOW)
    user = validate_init_data(init, BOT_TOKEN, now=_NOW)
    assert int(user["id"]) == OWNER_ID


def test_tampered_hash_rejected() -> None:
    init = make_init_data(auth_date=_NOW)
    tampered = init[:-1] + ("0" if init[-1] != "0" else "1")
    with pytest.raises(InitDataError):
        validate_init_data(tampered, BOT_TOKEN, now=_NOW)


def test_wrong_bot_token_rejected() -> None:
    init = make_init_data(auth_date=_NOW)
    with pytest.raises(InitDataError):
        validate_init_data(init, "999:other-token", now=_NOW)


def test_expired_init_data_rejected() -> None:
    init = make_init_data(auth_date=_NOW)
    with pytest.raises(InitDataError, match="expired"):
        validate_init_data(init, BOT_TOKEN, now=_NOW + 48 * 3600)


def test_empty_init_data_rejected() -> None:
    with pytest.raises(InitDataError):
        validate_init_data("", BOT_TOKEN, now=_NOW)


#: The second person allowed to drive the bot (ADDITIONAL_TELEGRAM_USER_IDS).
GUEST_ID = OWNER_ID + 7


def _owner() -> RequireUser:
    return RequireUser(bot_token=BOT_TOKEN, allowed_user_ids=(OWNER_ID,))


def _shared() -> RequireUser:
    return RequireUser(bot_token=BOT_TOKEN, allowed_user_ids=(OWNER_ID, GUEST_ID))


def test_require_owner_accepts_valid_header() -> None:
    init = make_init_data()  # auth_date = now
    assert _owner()(authorization=f"tma {init}") == OWNER_ID


def test_require_owner_rejects_other_user() -> None:
    init = make_init_data(user_id=OWNER_ID + 1)
    with pytest.raises(HTTPException) as exc:
        _owner()(authorization=f"tma {init}")
    assert exc.value.status_code == 403


def test_require_owner_missing_header_is_401() -> None:
    with pytest.raises(HTTPException) as exc:
        _owner()(authorization=None)
    assert exc.value.status_code == 401


def test_require_owner_malformed_scheme_is_401() -> None:
    init = make_init_data()
    with pytest.raises(HTTPException) as exc:
        _owner()(authorization=f"Bearer {init}")
    assert exc.value.status_code == 401


def test_clock_skew_within_window_ok() -> None:
    init = make_init_data(auth_date=_NOW)
    # auth_date slightly in the "future" relative to now: still valid (not expired).
    assert validate_init_data(init, BOT_TOKEN, now=_NOW - 5)["id"] == OWNER_ID


def test_realistic_now_path() -> None:
    # Exercise the RequireUser path that uses the real wall clock internally.
    init = make_init_data(auth_date=int(time.time()))
    assert _owner()(authorization=f"tma {init}") == OWNER_ID


def test_is_allowed_init_data_accepts_allowed_user() -> None:
    init = make_init_data(auth_date=_NOW)
    assert is_allowed_init_data(init, bot_token=BOT_TOKEN, allowed_user_ids=(OWNER_ID,), now=_NOW)


def test_is_allowed_init_data_rejects_other_user() -> None:
    init = make_init_data(user_id=OWNER_ID + 1, auth_date=_NOW)
    assert not is_allowed_init_data(
        init, bot_token=BOT_TOKEN, allowed_user_ids=(OWNER_ID,), now=_NOW
    )


def test_is_allowed_init_data_rejects_expired() -> None:
    init = make_init_data(auth_date=_NOW)
    assert not is_allowed_init_data(
        init, bot_token=BOT_TOKEN, allowed_user_ids=(OWNER_ID,), now=_NOW + 48 * 3600
    )


def test_is_allowed_init_data_rejects_garbage() -> None:
    assert not is_allowed_init_data(
        "not-init-data", bot_token=BOT_TOKEN, allowed_user_ids=(OWNER_ID,)
    )


def test_is_allowed_init_data_realistic_now_path() -> None:
    # Exercise the default wall-clock branch (now=None), as the WS route uses it.
    init = make_init_data(auth_date=int(time.time()))
    assert is_allowed_init_data(init, bot_token=BOT_TOKEN, allowed_user_ids=(OWNER_ID,))


def test_require_user_accepts_second_allowed_user() -> None:
    init = make_init_data(user_id=GUEST_ID)
    assert _shared()(authorization=f"tma {init}") == GUEST_ID


def test_require_user_returns_the_caller_not_the_owner() -> None:
    # The dependency reports who actually called, so a route can attribute by it.
    init = make_init_data(user_id=GUEST_ID)
    assert _shared()(authorization=f"tma {init}") != OWNER_ID


def test_require_user_still_rejects_a_stranger_when_shared() -> None:
    init = make_init_data(user_id=GUEST_ID + 1)
    with pytest.raises(HTTPException) as exc:
        _shared()(authorization=f"tma {init}")
    assert exc.value.status_code == 403


def test_is_allowed_init_data_accepts_second_user() -> None:
    init = make_init_data(user_id=GUEST_ID, auth_date=_NOW)
    assert is_allowed_init_data(
        init, bot_token=BOT_TOKEN, allowed_user_ids=(OWNER_ID, GUEST_ID), now=_NOW
    )


def test_is_allowed_init_data_rejects_user_outside_the_list() -> None:
    init = make_init_data(user_id=GUEST_ID + 1, auth_date=_NOW)
    assert not is_allowed_init_data(
        init, bot_token=BOT_TOKEN, allowed_user_ids=(OWNER_ID, GUEST_ID), now=_NOW
    )
