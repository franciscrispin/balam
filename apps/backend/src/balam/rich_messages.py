"""Bot API 10.1 Rich Messages — send GFM to Telegram without MarkdownV2 escaping.

Every reply goes out through here.
Telegram parses GitHub-Flavored Markdown natively via ``sendRichMessage`` and
``InputRichMessage.markdown`` (Bot API 10.1, 2026-06-11), so the agent's output
skips the escaping pass in :mod:`balam.markdown`. Rich messages also carry
structure MarkdownV2 has no way to express — tables, headings, task lists,
``<details>`` collapsibles — and lift the length cap from 4096 to 32768
characters.

Near-as-is, though, not as-is: the two dialects disagree in both directions, so
two things are reconciled on the way out, in :func:`_rich_payload`. Where
Telegram reads *more* than the agent meant, :func:`escape_math_delimiters` — a
``$…$`` pair is LaTeX to Telegram and two prices to everyone else. Where it
reads *less*, :func:`separate_glued_tables` — GitHub lets a table start on the
line after a paragraph, Telegram wants a blank line first and otherwise shows
the pipes.

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


def _fenced_segments(markdown: str) -> list[tuple[bool, list[str]]]:
    """Split ``markdown`` into runs of lines, each flagged ``is_fenced_code``.

    A fenced block runs from its opening fence line through its closing fence
    line inclusive; an unclosed fence runs to the end (truncation mid-stream
    leaves one, and treating the tail as code is the safe direction for every
    caller — a missed fix-up, never a corrupted code block).
    """
    segments: list[tuple[bool, list[str]]] = []
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

    return segments


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
    return "\n".join(
        "\n".join(lines) if is_code else _escape_dollars_outside_codespans("\n".join(lines))
        for is_code, lines in _fenced_segments(markdown)
    )


#: A GFM table delimiter row: cells of hyphens with optional alignment colons,
#: separated by pipes, outer pipes optional. Callers also require at least one
#: pipe, so a bare ``---`` — a setext heading underline or a thematic break — is
#: never taken for one.
_DELIMITER_CELL = r"[ \t]*:?-+:?[ \t]*"
_TABLE_DELIMITER_RE = re.compile(
    rf"^ {{0,3}}\|?{_DELIMITER_CELL}(?:\|{_DELIMITER_CELL})*\|?[ \t]*$"
)


def _is_table_delimiter_row(line: str) -> bool:
    return "|" in line and _TABLE_DELIMITER_RE.match(line) is not None


def separate_glued_tables(markdown: str) -> str:
    """Put a blank line between a paragraph and a table glued to its last line.

    The agent writes tables GitHub-style — the header row on the line right
    after the sentence introducing it, no blank line between::

        Here are the results:
        | Metric | Value |
        |---|---|

    GitHub's GFM lets a table interrupt a paragraph (the paragraph's last line
    becomes the header row), so the agent never learns to leave the gap.
    Telegram's rich-message parser does not: the header row stays paragraph
    text, the rows below follow it, and the user sees pipes and dashes where a
    table should be. Nothing fails — the payload is accepted — which is how
    every table the bot sent went out this way unnoticed.

    Normalizing here beats asking the agent to leave the blank line: the model
    drifts, and nothing would tell us when it did.

    Detection is a delimiter row (``|---|---|``) whose previous line has pipes
    and whose line before *that* is non-blank; the blank line goes in before
    the header row. Fenced code is skipped — a markdown example inside a code
    block is code, not a table to fix. The line before the header may be a
    closing fence: a blank line after a fence changes nothing for GitHub and is
    what Telegram wants. Applied per payload, so mid-stream a header row that
    has arrived without its delimiter is left alone until the next edit brings
    the delimiter along.
    """
    lines: list[str] = []
    is_code: list[bool] = []
    for fenced, segment in _fenced_segments(markdown):
        lines.extend(segment)
        is_code.extend([fenced] * len(segment))

    out: list[str] = []
    for i, line in enumerate(lines):
        header_glued_to_paragraph = (
            0 < i < len(lines) - 1
            and not is_code[i]
            and not is_code[i + 1]
            and "|" in line
            and _is_table_delimiter_row(lines[i + 1])
            and bool(lines[i - 1].strip())
        )
        if header_glued_to_paragraph:
            out.append("")
        out.append(line)
    return "\n".join(out)


def _rich_payload(markdown: str) -> dict[str, Any]:
    # skip_entity_detection stops Telegram from auto-linkifying bare URLs, @names
    # and #tags inside agent output (code identifiers turn into stray links).
    # Every rich send/edit/draft funnels through here, so reconciling the agent's
    # GFM with Telegram's here is what makes it impossible for one path to miss
    # it. Order is immaterial: one only inserts blank lines, the other only
    # touches ``$`` outside code.
    return {
        "markdown": escape_math_delimiters(separate_glued_tables(markdown)),
        "skip_entity_detection": True,
    }


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
