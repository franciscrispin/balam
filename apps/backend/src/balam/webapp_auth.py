"""Telegram Mini App ``initData`` authentication (ADR-0008).

The Mini App runs inside Telegram's webview, which hands the page a signed
``initData`` string. Every Mini App API request must prove it carries valid
``initData`` (HMAC-SHA256 with the bot token, per Telegram's Web App spec) *and*
that the embedded user is the single allowed owner — the same trust boundary the
bot enforces on incoming updates. The frontend sends it as
``Authorization: tma <initData>`` (Telegram's ``tma`` scheme).

HMAC validation is adapted from the open-shrimp reference (ADR-0011).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qs

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

#: Max age of an ``initData`` payload before it is considered stale. Generous for
#: a single-user local tool, but bounded so a leaked old payload can't be replayed
#: forever.
_MAX_AGE_SECONDS = 24 * 3600

#: The Authorization scheme the frontend uses (Telegram's Mini App convention).
_SCHEME = "tma"


class InitDataError(ValueError):
    """Raised when an ``initData`` payload is missing, malformed, or invalid."""


def _data_check_string(parsed: dict[str, list[str]]) -> str:
    """Telegram's data_check_string: every ``key=value`` except ``hash``, sorted
    by key, newline-joined. Values are the already URL-decoded strings."""
    return "\n".join(f"{key}={parsed[key][0]}" for key in sorted(parsed) if key != "hash")


def _verify_hmac(data_check_string: str, provided_hash: str, bot_token: str) -> bool:
    """Verify the HMAC-SHA256 signature: secret = HMAC("WebAppData", bot_token)."""
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, provided_hash)


def validate_init_data(
    init_data: str, bot_token: str, *, now: int, max_age_seconds: int = _MAX_AGE_SECONDS
) -> dict:
    """Validate a raw ``initData`` query string and return the embedded user dict.

    ``now`` is the current unix time (injected so callers/tests stay deterministic).
    Raises :class:`InitDataError` on any failure — bad signature, stale, or no user.
    """
    if not init_data:
        raise InitDataError("empty initData")

    parsed = parse_qs(init_data, keep_blank_values=True)

    if "hash" not in parsed:
        raise InitDataError("missing hash in initData")
    if not _verify_hmac(_data_check_string(parsed), parsed["hash"][0], bot_token):
        raise InitDataError("invalid initData signature")

    if "auth_date" not in parsed:
        raise InitDataError("missing auth_date in initData")
    try:
        auth_date = int(parsed["auth_date"][0])
    except (ValueError, IndexError) as exc:
        raise InitDataError("invalid auth_date in initData") from exc
    if now - auth_date > max_age_seconds:
        raise InitDataError("initData has expired")

    if "user" not in parsed:
        raise InitDataError("missing user in initData")
    try:
        user = json.loads(parsed["user"][0])
        int(user["id"])  # validate shape early
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise InitDataError("invalid user in initData") from exc
    return user


def is_owner_init_data(
    init_data: str, *, bot_token: str, allowed_user_id: int, now: int | None = None
) -> bool:
    """True iff ``init_data`` is HMAC-valid, fresh, and embeds the allowed owner.

    The WebSocket route's auth (ADR-0006): a browser cannot set an
    ``Authorization`` header on a WebSocket, so the noVNC client sends its
    ``initData`` as the first text frame and the route checks it with this
    instead of :class:`RequireOwner`.
    """
    try:
        user = validate_init_data(
            init_data, bot_token, now=now if now is not None else int(time.time())
        )
    except InitDataError:
        return False
    return int(user["id"]) == allowed_user_id


class RequireOwner:
    """FastAPI dependency: authenticate a Mini App request as the allowed owner.

    Reads the ``Authorization: tma <initData>`` header, validates it, and asserts
    the embedded user id matches ``allowed_user_id``. Returns the owner's user id.
    Raises ``HTTPException(401)`` otherwise. Valid ``initData`` is always required —
    the Mini App is reachable over the internet (ADR-0013), so there is no bypass.
    """

    def __init__(self, *, bot_token: str, allowed_user_id: int) -> None:
        self._bot_token = bot_token
        self._allowed_user_id = allowed_user_id

    def __call__(self, authorization: str | None = Header(default=None)) -> int:
        if not authorization:
            raise HTTPException(status_code=401, detail="missing Authorization header")

        scheme, _, payload = authorization.partition(" ")
        if scheme != _SCHEME or not payload:
            raise HTTPException(status_code=401, detail="malformed Authorization header")

        try:
            user = validate_init_data(payload, self._bot_token, now=int(time.time()))
        except InitDataError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        if int(user["id"]) != self._allowed_user_id:
            raise HTTPException(status_code=403, detail="user not allowed")
        return self._allowed_user_id
