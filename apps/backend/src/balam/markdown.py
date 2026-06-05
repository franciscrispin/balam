"""GFM → Telegram MarkdownV2 converter (ADR-0010).

The OpenCode agent emits GitHub-Flavored Markdown; Telegram renders a stricter
``MarkdownV2`` dialect with aggressive escaping rules. We parse the GFM to an AST
with ``mistune`` and walk it to emit Telegram-compatible markup, then split the
result into ≤4096-char messages at natural boundaries (code-block-aware).

The approach follows ~/projects/open-shrimp's ``markdown.py`` as a worked example.
"""

from __future__ import annotations

import re
from typing import Any

import mistune

#: Telegram's hard limit on message text length (characters).
TELEGRAM_MAX_LENGTH = 4096

# Characters that must be escaped in MarkdownV2 outside code spans/blocks.
_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"
_ESCAPE_RE = re.compile(r"([" + re.escape(_ESCAPE_CHARS) + r"])")


def _escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _ESCAPE_RE.sub(r"\\\1", text)


def _plain_text(token: dict[str, Any]) -> str:
    """Extract plain text from a token subtree (no formatting)."""
    if isinstance(token.get("raw"), str):
        return token["raw"]
    children = token.get("children")
    if isinstance(children, str):
        return children
    if isinstance(children, list):
        return "".join(_plain_text(child) for child in children)
    return ""


class TelegramRenderer(mistune.BaseRenderer):
    """Render mistune AST tokens into Telegram MarkdownV2 strings."""

    NAME = "telegram"

    def render_token(self, token: dict[str, Any], state: Any) -> str:
        ttype = token["type"]
        if ttype == "table":  # needs raw token access for cell text
            return self._render_table(token, state)

        func = self._get_method(ttype)
        attrs = token.get("attrs", {})
        if "raw" in token:
            children = token["raw"]
        elif "children" in token:
            children = self.render_tokens(token["children"], state)
        else:
            return func(**attrs) if attrs else func()
        return func(children, **attrs) if attrs else func(children)

    # ── Block-level ──

    def text(self, text: str) -> str:
        return _escape(text)

    def paragraph(self, text: str) -> str:
        return text + "\n\n"

    def heading(self, text: str, **attrs: Any) -> str:
        return f"*{text}*\n\n"

    def blank_line(self) -> str:
        return ""

    def thematic_break(self) -> str:
        return _escape("---") + "\n\n"

    def block_code(self, code: str, **attrs: Any) -> str:
        info = attrs.get("info", "")
        code = code.rstrip("\n")
        if info:
            return f"```{info}\n{code}\n```\n\n"
        return f"```\n{code}\n```\n\n"

    def block_quote(self, text: str) -> str:
        # Telegram ends a blockquote at a bare ">" line, so drop empty lines that
        # would otherwise split one quote into several with unquoted gaps.
        non_empty = [line for line in text.strip().split("\n") if line]
        quoted = "\n".join(">" + line for line in non_empty)
        return quoted + "\n\n"

    def list(self, text: str, **attrs: Any) -> str:
        return text + "\n"

    def list_item(self, text: str) -> str:
        return _escape("- ") + text.strip() + "\n"

    def block_text(self, text: str) -> str:
        return text

    def block_error(self, text: str) -> str:
        return ""

    # ── Tables → monospace preformatted ──

    def _render_table(self, token: dict[str, Any], state: Any) -> str:
        rows: list[list[str]] = []
        for child in token.get("children", []):
            if child["type"] == "table_head":
                rows.append([_plain_text(cell) for cell in child.get("children", [])])
            elif child["type"] == "table_body":
                for table_row in child.get("children", []):
                    rows.append([_plain_text(cell) for cell in table_row.get("children", [])])

        if not rows:
            return ""

        col_count = max(len(r) for r in rows)
        col_widths = [0] * col_count
        for r in rows:
            for i, cell in enumerate(r):
                col_widths[i] = max(col_widths[i], len(cell))

        lines: list[str] = []
        for idx, r in enumerate(rows):
            padded = [(r[i] if i < len(r) else "").ljust(col_widths[i]) for i in range(col_count)]
            lines.append(" | ".join(padded))
            if idx == 0:
                lines.append("-+-".join("-" * w for w in col_widths))

        return "```\n" + "\n".join(lines) + "\n```\n\n"

    # Stubs so mistune's table plugin finds the methods; real work is above.
    def table(self, text: str) -> str:  # pragma: no cover
        return text

    def table_head(self, text: str) -> str:  # pragma: no cover
        return text

    def table_body(self, text: str) -> str:  # pragma: no cover
        return text

    def table_row(self, text: str) -> str:  # pragma: no cover
        return text

    def table_cell(self, text: str, **attrs: Any) -> str:  # pragma: no cover
        return text

    # ── Inline-level ──

    def emphasis(self, text: str) -> str:
        return f"_{text}_"

    def strong(self, text: str) -> str:
        return f"*{text}*"

    def codespan(self, code: str) -> str:
        return f"`{code}`"

    def link(self, text: str, **attrs: Any) -> str:
        url = attrs.get("url", "")
        escaped_url = url.replace("\\", "\\\\").replace(")", "\\)")
        return f"[{text}]({escaped_url})"

    def image(self, text: str, **attrs: Any) -> str:
        return text or ""  # strip images, keep alt text

    def linebreak(self) -> str:
        return "\n"

    def softbreak(self) -> str:
        return "\n"

    def inline_html(self, html: str) -> str:
        return ""

    def block_html(self, html: str) -> str:
        return ""

    def strikethrough(self, text: str) -> str:
        return f"~{text}~"


def _is_inside_code_block(text: str, position: int) -> tuple[bool, str]:
    """Whether ``position`` in rendered text sits inside a ``` fence.

    Returns ``(is_inside, fence)`` where ``fence`` is the opening fence line
    (e.g. ``` ```python ```) so the next chunk can reopen it.
    """
    inside = False
    fence = "```"
    i = 0
    while i < position:
        if text[i : i + 3] == "```":
            if not inside:
                end = text.find("\n", i)
                if end == -1 or end > position:
                    end = position
                fence = text[i:end]
                inside = True
            else:
                inside = False
            i += 3
        else:
            i += 1
    return inside, fence


def split_message(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split rendered text into ≤``max_length`` chunks at natural boundaries.

    Prefers paragraph then line breaks. If a split lands inside a fenced code
    block, the current chunk is closed with ``` and the next reopens the fence.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining.strip())
            break

        split_at = remaining.rfind("\n\n", 0, max_length)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, max_length)
        if split_at <= 0:
            split_at = max_length

        inside, fence = _is_inside_code_block(remaining, split_at)
        # When the split lands inside a fence, the chunk gains a closing ``` and
        # the next chunk re-opens with `fence`. Both cost space, so account for
        # them: keep the closed chunk within the cap, and — crucially — guarantee
        # progress. A natural boundary right after the opening fence (e.g. a
        # single code line longer than the cap) would re-add the fence and never
        # shrink `remaining`, looping forever; fall back to a hard cut then.
        suffix = "\n```" if inside else ""
        reopen = f"{fence}\n" if inside else ""
        max_chunk = max_length - len(suffix)
        if split_at > max_chunk or split_at <= len(reopen):
            split_at = max_chunk

        if inside:
            chunk = remaining[:split_at].rstrip() + suffix
            rest = reopen + remaining[split_at:].lstrip("\n")
        else:
            chunk = remaining[:split_at].strip()
            rest = remaining[split_at:].lstrip("\n")

        chunks.append(chunk)
        remaining = rest

    return [c for c in chunks if c]


_MARKDOWN = mistune.create_markdown(
    renderer=TelegramRenderer(),
    plugins=["strikethrough", "table"],
)


def gfm_to_telegram(text: str) -> list[str]:
    """Convert GFM to Telegram MarkdownV2, returning ≤4096-char message chunks."""
    rendered = _MARKDOWN(text)
    return split_message(rendered)
