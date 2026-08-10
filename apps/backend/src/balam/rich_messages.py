"""Bot API 10.1 Rich Messages — send GFM to Telegram without MarkdownV2 escaping.

Every reply goes out through here.
Telegram parses GitHub-Flavored Markdown natively via ``sendRichMessage`` and
``InputRichMessage.markdown`` (Bot API 10.1, 2026-06-11), so the agent's output
skips the escaping pass in :mod:`balam.markdown`. Rich messages also carry
structure MarkdownV2 has no way to express — tables, headings, task lists,
``<details>`` collapsibles — and lift the length cap from 4096 to 32768
characters.

Near-as-is, though, not as-is: Telegram's dialect is a *superset* of the agent's,
so one construct still has to be escaped on the way out. See
:func:`escape_math_delimiters` — ``$…$`` is LaTeX to Telegram and a pair of
prices to everyone else.

python-telegram-bot does not wrap these methods: upstream paused Bot API 10.1
work on 2026-06-18 pending an internal refactor and closed the community PRs, so
support is queued for PTB v23 (issue #5261). Until then we call the endpoints
through :meth:`telegram.Bot.do_api_request`, which exists for exactly this and
still runs the request through PTB's transport, rate limiter and retry handling.

Every entry point falls back to the MarkdownV2 path on failure, so a payload
Telegram rejects (``RICH_MESSAGE_EMPTY``) degrades to the old rendering rather
than dropping the message.
"""

from __future__ import annotations

import logging
import re
import warnings
from typing import Any

from telegram.error import RetryAfter
from telegram.warnings import PTBUserWarning

from balam.markdown import gfm_to_telegram, split_message

logger = logging.getLogger(__name__)

# PTB *does* wrap editMessageText, so it nudges callers away from
# do_api_request — but its wrapper predates Bot API 10.1 and has no way to pass
# ``rich_message`` (and requires ``text``, which 10.1 makes optional). The raw
# call is the only route, so drop the nudge rather than log it once per edit.
warnings.filterwarnings(
    "ignore",
    message=r'.*do_api_request\("editMessageText".*',
    category=PTBUserWarning,
)

#: Telegram's cap on rich message text (Bot API 10.1, "Rich Message Limits").
RICH_MAX_LENGTH = 32768


def chunk_rich(text: str) -> list[str]:
    """Split GFM into ≤:data:`RICH_MAX_LENGTH` chunks, code-block-aware.

    A :data:`balam.streamer.Renderer` for rich mode: the transport wants raw GFM,
    so unlike :func:`balam.markdown.gfm_to_telegram` this only enforces the
    length cap. At 32768 characters an agent reply is virtually always one chunk.
    """
    return split_message(text, RICH_MAX_LENGTH)


#: An opening or closing code fence: up to three spaces of indent, then three or
#: more backticks/tildes. Group 2 is the rest of the line — an *info string* on an
#: opening fence (```` ```python ````), empty on a closing one.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _find_closing_backticks(text: str, start: int, run: int) -> int:
    """Index of the next run of *exactly* ``run`` backticks at or after ``start``,
    or ``-1``. A longer run does not close a shorter one (CommonMark)."""
    i, n = start, len(text)
    while i < n:
        if text[i] == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            if j - i == run:
                return i
            i = j
        else:
            i += 1
    return -1


def _escape_dollars_outside_codespans(text: str) -> str:
    """Backslash-escape every ``$`` in ``text`` that is not inside a code span.

    Walks the text so a stray backtick cannot run away: an unclosed span is
    treated as literal backticks and its ``$`` still get escaped.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "\\" and i + 1 < n:
            # An existing escape (``\$``, ``\\``) is already correct — copy the
            # pair through so it never becomes ``\\$``.
            out.append(text[i : i + 2])
            i += 2
        elif char == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            close = _find_closing_backticks(text, j, j - i)
            if close == -1:
                out.append(text[i:j])
                i = j
            else:
                out.append(text[i : close + (j - i)])
                i = close + (j - i)
        elif char == "$":
            out.append("\\$")
            i += 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def escape_math_delimiters(markdown: str) -> str:
    """Escape ``$`` so Telegram does not read agent prose as LaTeX.

    Telegram's rich-message parser implements the GFM **math extension**: a
    ``$…$`` pair becomes a ``mathematical_expression``, rendered in serif italics
    with whitespace collapsed, ``-`` turned into ``−``, and any markdown inside it
    left as literal text. Two prices in one paragraph are enough to trigger it —
    ``S$3 back per in-store bill of **S$10+**`` renders as
    ``S`` + math(``3 back per in-store bill of **S``) + ``10+**``.

    Escaping is deliberately unconditional rather than only for ``$`` that would
    pair up: a lone ``\\$`` renders as a plain ``$``, so there is no cost, and the
    pairing rules are Telegram's to change. The trade is that genuine LaTeX from
    the agent stops rendering as math — the right call for a bot whose replies
    quote far more prices than integrals.

    Code is skipped, and that exclusion is load-bearing in both directions:
    ``$`` inside a code span or fenced block is already immune to the math
    extension, and a backslash there is **kept literally** — escaping ``echo
    $PATH`` would show the user ``echo \\$PATH``.
    """
    segments: list[tuple[bool, list[str]]] = []  # (is_fenced_code, lines)
    fence: str | None = None

    for line in markdown.split("\n"):
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                segments.append((True, [line]))
            elif segments and not segments[-1][0]:
                segments[-1][1].append(line)
            else:
                segments.append((False, [line]))
            continue
        segments[-1][1].append(line)
        # A closing fence is the same character, at least as long, and alone on
        # its line — otherwise ```` ```python ```` would close ```` ``` ````.
        if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
            if not match.group(2).strip():
                fence = None

    return "\n".join(
        "\n".join(lines) if is_code else _escape_dollars_outside_codespans("\n".join(lines))
        for is_code, lines in segments
    )


def _rich_payload(markdown: str) -> dict[str, Any]:
    # skip_entity_detection stops Telegram from auto-linkifying bare URLs, @names
    # and #tags inside agent output (code identifiers turn into stray links).
    # Every rich send/edit/draft funnels through here, so escaping the math
    # delimiters here is what makes it impossible for one path to miss it.
    return {"markdown": escape_math_delimiters(markdown), "skip_entity_detection": True}


async def send_rich_message(
    bot: Any,
    *,
    chat_id: int,
    markdown: str,
    thread_kwargs: dict[str, Any] | None = None,
    reply_markup: Any = None,
) -> int | None:
    """Send ``markdown`` as a rich message; return its message id.

    Raises on flood control (the caller's rate limiter already retried) and
    returns ``None`` if the response carries no message id.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": _rich_payload(markdown),
        **(thread_kwargs or {}),
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = await bot.do_api_request("sendRichMessage", api_kwargs=payload)
    if isinstance(result, dict):
        message_id = result.get("message_id")
        return int(message_id) if message_id is not None else None
    return getattr(result, "message_id", None)


async def edit_rich_message(
    bot: Any,
    *,
    chat_id: int,
    message_id: int,
    markdown: str,
) -> None:
    """Replace a message's content with rich ``markdown`` (``editMessageText``).

    "message is not modified" is benign — an identical render — and swallowed.
    """
    try:
        await bot.do_api_request(
            "editMessageText",
            api_kwargs={
                "chat_id": chat_id,
                "message_id": message_id,
                "rich_message": _rich_payload(markdown),
            },
        )
    except RetryAfter:
        raise
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        raise


async def send_rich_draft(
    bot: Any,
    *,
    chat_id: int,
    draft_id: int,
    markdown: str,
    thread_kwargs: dict[str, Any] | None = None,
) -> None:
    """Stream a partial rich message (``sendRichMessageDraft``).

    Private chats only — a forum supergroup rejects this with
    ``TEXTDRAFT_PEER_INVALID``, which the caller treats as "switch to live-edit
    streaming" exactly as it does for plain ``sendMessageDraft``.
    """
    await bot.do_api_request(
        "sendRichMessageDraft",
        api_kwargs={
            "chat_id": chat_id,
            "draft_id": draft_id,
            "rich_message": _rich_payload(markdown),
            **(thread_kwargs or {}),
        },
    )


def markdown_v2_fallback(markdown: str) -> list[str]:
    """Render GFM the old way, for when Telegram rejects the rich payload."""
    return gfm_to_telegram(markdown)
