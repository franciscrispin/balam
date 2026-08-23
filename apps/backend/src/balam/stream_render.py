"""Render a turn's parts into the text and keyboards Telegram will show.

Pure functions only: each takes agent-side data (a tool call, a todo list, an
approval request) and returns a string or a keyboard. Nothing here sends,
edits or deletes a message — that is :mod:`balam.streamer`'s job.

The split matters because the two halves fail differently. A bug here shows the
owner the wrong words; a bug in the transport half loses the answer or leaves it
stranded above a later message. Keeping the rendering pure means it can be
tested by comparing strings, with no transport double in sight.

Tool display labels come from the canonical registry in :mod:`balam.tools`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from balam.agent.events import BackgroundTask
from balam.approvals import Choice, is_edit
from balam.tools import DISPLAY_BY_WIRE, Tool

logger = logging.getLogger(__name__)

#: Caps on inline Bash output, matching open-shrimp. Full output goes to the
#: Mini App later (Tier 2/3); for now we inline-truncate, keeping the tail.
BASH_OUTPUT_MAX_LINES = 50
BASH_OUTPUT_MAX_CHARS = 1500


#: One streamed fragment: ``(arrival_order, kind, rendered_text)`` where ``kind``
#: is ``"text"`` (assistant prose), ``"tool"`` (a rendered tool-call line),
#: ``"toolgroup"`` (a closed group of calls, an expandable-quote block), or
#: ``"narration"`` (an earlier step's interim text, demoted to progress).
StreamPart = tuple[int, str, str]

#: A tool call's latest state inside a group:
#: ``(tool, input, status, output, error)``.
GroupEntry = tuple[str, dict[str, Any], str, Any, Any]


def _join_stream(parts: dict[str, StreamPart]) -> str:
    """Render the session's text and tool parts as one GFM string, in arrival
    order. Consecutive text fragments concatenate (they are deltas of one
    message); tool lines and demoted narration blocks are set off from their
    neighbours by separators."""
    out = ""
    prev_kind: str | None = None
    for _order, kind, text in sorted(parts.values(), key=lambda p: p[0]):
        if not text:
            continue
        if out:
            if prev_kind != kind:  # kind transition (text↔tool↔narration)
                out = out.rstrip("\n") + "\n\n"
            elif kind == "tool":  # group consecutive tool lines
                out = out.rstrip("\n") + "\n"
            elif kind in ("narration", "toolgroup"):
                # Blocks from different steps / adjacent quote blocks: a blank
                # line, or GFM would lazily continue one blockquote into the next.
                out = out.rstrip("\n") + "\n\n"
            # text after text: concatenate the deltas, no separator
        out += text
        prev_kind = kind
    return out


def _relpath(path: str, directory: str | None) -> str:
    """Show *path* relative to the context *directory* when it lives under it;
    otherwise return it unchanged (e.g. an absolute path outside the workspace)."""
    if not directory or not path:
        return path
    try:
        rel = os.path.relpath(path, directory)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


#: apply_patch envelope headers; the path follows the prefix. Used only to render
#: a readable tool line (the boundary check uses the permission metadata instead).
_APPLY_PATCH_HEADERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")


def _apply_patch_files(patch_text: str) -> list[str]:
    """File paths an apply_patch envelope touches, for display."""
    out: list[str] = []
    for line in patch_text.splitlines():
        for prefix in _APPLY_PATCH_HEADERS:
            if line.startswith(prefix):
                path = line[len(prefix) :].strip()
                if path:
                    out.append(path)
                break
    return out


def _tool_summary(tool: str, tool_input: dict[str, Any], directory: str | None) -> str:
    """A one-line argument summary for a tool call (paths shown workspace-relative)."""
    if tool in (Tool.READ, Tool.EDIT, Tool.WRITE):
        return _relpath(tool_input.get("filePath", ""), directory)
    if tool == Tool.LIST:
        return _relpath(tool_input.get("path", ""), directory)
    if tool == Tool.GLOB:
        return tool_input.get("pattern", "")
    if tool == Tool.GREP:
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"{pattern} in {_relpath(path, directory)}" if path else pattern
    if tool == Tool.APPLY_PATCH:
        # The raw patchText envelope is huge and breaks MarkdownV2; show the files
        # it touches instead (parsed from the envelope headers).
        paths = _apply_patch_files(tool_input.get("patchText", ""))
        return ", ".join(_relpath(p, directory) for p in paths)
    if tool == Tool.WEBFETCH:
        return tool_input.get("url", "")
    if tool in (Tool.TASK, Tool.AGENT):
        return tool_input.get("description", "") or tool_input.get("subagent_type", "")
    # Generic: first string-valued argument, capped.
    for value in tool_input.values():
        if isinstance(value, str) and value:
            return value[:80]
    return ""


def _truncate_output(text: str) -> str:
    """Truncate tool output to the inline caps, keeping the most recent tail."""
    text = text.strip()
    lines = text.splitlines()
    truncated = False
    if len(lines) > BASH_OUTPUT_MAX_LINES:
        lines = lines[-BASH_OUTPUT_MAX_LINES:]
        truncated = True
    result = "\n".join(lines)
    if len(result) > BASH_OUTPUT_MAX_CHARS:
        result = result[-BASH_OUTPUT_MAX_CHARS:]
        truncated = True
    return f"…(truncated)\n{result}" if truncated else result


def _coerce_output(output: Any) -> str:
    """Flatten an OpenCode tool ``output``/``error`` payload to plain text."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(
            block.get("text", "")
            for block in output
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return "" if output is None else str(output)


def _render_tool_part(
    tool: str,
    tool_input: dict[str, Any],
    status: str,
    output: Any,
    error: Any,
    directory: str | None,
) -> str:
    """Render a terminal tool part as a compact GFM line for the stream.

    Bash is special-cased to show its natural-language ``description`` and the
    command in a fenced block; successful output is omitted (it is the noise the
    stream drowns in, and the full output is available in the Mini App), and only
    a failed call's tail is kept. Everything else is a one-liner like
    ``🔧 Read src/foo.py``.
    """
    if tool == Tool.BASH:
        command = tool_input.get("command", "")
        description = tool_input.get("description", "")
        line = f"🔧 Bash — {description}" if description else "🔧 Bash"
        if command:
            line += f"\n```\n{command}\n```"
        # Only a failed call's output is actionable. ``status == "error"`` is the
        # backends' authoritative signal (SDK: tool_result.is_error; OpenCode:
        # tool-state status), with the payload in ``error`` (``output`` as a
        # fallback, since OpenCode may carry partial output alongside the error).
        if status == "error":
            body = _truncate_output(_coerce_output(error) or _coerce_output(output))
            if body:
                line += f"\n```\n{body}\n```"
        return line

    display = DISPLAY_BY_WIRE.get(tool, tool)
    summary = _tool_summary(tool, tool_input, directory)
    line = f"🔧 {display}"
    if summary:
        line += f" `{summary}`"
    if status == "error":
        line += " ⚠️"
    return line


def _render_todos(todos: list[Any]) -> str:
    """Render a todowrite ``todos`` list as the GFM checklist for the live
    progress message. Both backends put the list in the tool input with
    ``content`` per item (``text`` kept as a defensive fallback); items without
    text are skipped, an unknown status gets an unchecked box, and a cancelled
    item is struck through. Returns ``""`` when nothing is renderable.

    Emits a **native task list** under a heading — Telegram draws real
    checkboxes (Bot API 10.1), so only the states a checkbox can't express keep
    an icon: in-progress stays 🔄 and cancelled stays struck through, both
    unchecked since neither is done.
    """
    lines = ["### 📋 Progress", ""]
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        label = str(todo.get("content") or todo.get("text") or "").strip()
        if not label:
            continue
        status = str(todo.get("status") or "")
        if status == "cancelled":
            label = f"~~{label}~~"
        box = "- [x] " if status == "completed" else "- [ ] "
        lines.append(f"{box}🔄 {label}" if status == "in_progress" else f"{box}{label}")
    # The header costs two entries (heading + blank line).
    return "\n".join(lines) if len(lines) > 2 else ""


def _render_background_notice(tasks: Sequence[BackgroundTask]) -> str:
    """The turn-end notice naming background work that was cut short.

    The turn is held open while background work runs, so reaching turn end with
    tasks still live means the wait is over before the work was: the hold ran out
    of time, or the topic was evicted to let another one wait (ADR-0017), or the
    turn failed. Either way the CLI process — and with it these tasks — is being
    torn down. That is worth saying: the user was likely told a report was coming,
    and it is not.
    """
    if not tasks:
        return ""
    heading = (
        "⚙️ **1 background task was still running:**"
        if len(tasks) == 1
        else f"⚙️ **{len(tasks)} background tasks were still running:**"
    )
    lines = [heading]
    lines += [f"- `{task.description}`" for task in tasks]
    # Blank line first, or GFM reads the closing sentence as a lazy continuation
    # of the last bullet and folds it into the list.
    lines += [
        "",
        "The turn ran out of time to wait, so they stop here. For work that "
        "should outlive the conversation, ask for it detached (`setsid nohup … &`).",
    ]
    return "\n".join(lines)


def _group_phrase(entries: list[GroupEntry], *, running: bool) -> str:
    """The one-line summary of a burst of tool calls, in Claude Code's collapsed
    vocabulary: comma-joined per-category counts ("Ran 3 commands, read a file"),
    present tense while calls are still ``running``, past tense once finished.
    Reads and edits are deduplicated by file path."""
    commands = 0
    reads: set[str] = set()
    edits: set[str] = set()
    searches = lists = fetches = websearches = tasks = others = 0
    for i, (tool, tool_input, _status, _output, _error) in enumerate(entries):
        if tool == Tool.BASH:
            commands += 1
        elif tool == Tool.READ:
            reads.add(tool_input.get("filePath") or f"read#{i}")
        elif tool in (Tool.EDIT, Tool.WRITE):
            edits.add(tool_input.get("filePath") or f"edit#{i}")
        elif tool == Tool.APPLY_PATCH:
            edits.update(_apply_patch_files(tool_input.get("patchText", "")) or [f"patch#{i}"])
        elif tool in (Tool.GREP, Tool.GLOB):
            searches += 1
        elif tool == Tool.LIST:
            lists += 1
        elif tool == Tool.WEBFETCH:
            fetches += 1
        elif tool == "websearch":
            websearches += 1
        elif tool in (Tool.TASK, Tool.AGENT):
            tasks += 1
        else:
            others += 1

    def count(past: str, now: str, n: int, noun: str, noun_plural: str | None = None) -> str:
        verb = now if running else past
        if n == 1:
            return f"{verb} a {noun}"
        return f"{verb} {n} {noun_plural or noun + 's'}"

    parts: list[str] = []
    if commands:
        parts.append(count("ran", "running", commands, "command"))
    if reads:
        parts.append(count("read", "reading", len(reads), "file"))
    if edits:
        parts.append(count("edited", "editing", len(edits), "file"))
    if searches:
        parts.append(count("searched for", "searching for", searches, "pattern"))
    if lists:
        parts.append(count("listed", "listing", lists, "directory", "directories"))
    if fetches:
        parts.append(count("fetched", "fetching", fetches, "page"))
    if websearches:
        verb = "searching" if running else "searched"
        parts.append(f"{verb} the web" + (f" {websearches} times" if websearches > 1 else ""))
    if tasks:
        parts.append(count("delegated", "delegating", tasks, "task"))
    if others:
        parts.append(count("made", "making", others, "tool call"))
    if not parts:
        return ""
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


def _group_detail_line(tool: str, tool_input: dict[str, Any], directory: str | None) -> str:
    """One compact line inside a closed group's expandable quote. No fenced
    blocks — Telegram disallows them inside a blockquote — so Bash degrades to
    its command in a codespan."""
    if tool == Tool.BASH:
        summary = str(tool_input.get("command") or tool_input.get("description") or "")
    else:
        summary = _tool_summary(tool, tool_input, directory)
    summary = " ".join(summary.split()).replace("`", "'")
    if len(summary) > 80:
        summary = summary[:79] + "…"
    display = DISPLAY_BY_WIRE.get(tool, tool)
    return f"🔧 {display} `{summary}`" if summary else f"🔧 {display}"


def _render_tool_group(
    entries: list[GroupEntry], *, active: bool, directory: str | None
) -> tuple[str, str]:
    """Render a group of consecutive tool calls; returns ``(kind, text)``.

    An *active* group (still absorbing calls) is a plain summary line that ticks
    up in place — Telegram re-collapses a ``<details>`` block on every edit, so
    the collapsed form only appears once the group closes. A closed group
    renders its finished calls: one call keeps the legacy single-line form
    (Bash with its fenced command); several fold into the summary line plus one
    compact line per call inside a native ``<details>`` block (Bot API 10.1).
    """
    if active:
        running = any(status not in ("completed", "error") for _t, _i, status, _o, _e in entries)
        phrase = _group_phrase(entries, running=running)
        return ("tool", f"🔧 {phrase}…" if phrase else "")
    finished = [e for e in entries if e[2] in ("completed", "error")]
    if not finished:
        return ("tool", "")
    if len(finished) == 1:
        tool, tool_input, status, output, error = finished[0]
        return ("tool", _render_tool_part(tool, tool_input, status, output, error, directory))
    summary = f"🔧 {_group_phrase(finished, running=False)}"
    details = [_group_detail_line(t, i, directory) for t, i, _s, _o, _e in finished]
    # Each call needs a list marker: consecutive plain lines inside <details>
    # collapse into one run-on paragraph. Codespans keep shell metacharacters
    # (`<`, `>`, `&`) out of the HTML parser — verified against the live API.
    body = "\n".join(f"- {line}" for line in details)
    return ("toolgroup", f"<details><summary>{summary}</summary>\n\n{body}\n\n</details>")


def _format_approval_request(
    tool: str, tool_input: dict[str, Any], directory: str | None, category: str | None = None
) -> str:
    """A GFM prompt asking the user to approve one tool call.

    Bash shows the command; file tools show the (workspace-relative) path; other
    tools fall back to the generic argument summary — the same vocabulary as the
    inline tool lines so a prompt reads like the stream around it.
    """
    display = DISPLAY_BY_WIRE.get(tool, tool)
    header = f"🔐 Allow **{display}**?"
    if category and category != tool:
        header += f"\nPermission: `{category}`"
    if tool == Tool.BASH:
        command = tool_input.get("command", "")
        # The tool's natural-language ``description`` (e.g. "Install acli via apt
        # repository") is the *reason* for the call — surface it so the prompt
        # explains what it's approving, not just the raw command.
        description = tool_input.get("description", "")
        body = header
        if description:
            body += f"\n_{description}_"
        if command:
            body += f"\n```\n{command}\n```"
        return body
    summary = _tool_summary(tool, tool_input, directory)
    return f"{header}\n`{summary}`" if summary else header


def _approval_keyboard(token: str, category: str) -> InlineKeyboardMarkup:
    """The inline keyboard for an approval prompt. Edit requests (category
    ``edit`` — edit/write/apply_patch) also offer "accept all edits" so the user
    can stop being asked for in-workspace edits."""
    rows = [
        [
            InlineKeyboardButton("Allow once", callback_data=f"appr:{Choice.ALLOW.value}:{token}"),
            InlineKeyboardButton("Deny", callback_data=f"appr:{Choice.DENY.value}:{token}"),
        ]
    ]
    if is_edit(category):
        rows.append(
            [
                InlineKeyboardButton(
                    "Accept all edits", callback_data=f"appr:{Choice.ALL.value}:{token}"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _question_keyboard(
    token: str,
    question_index: int,
    options: list[dict[str, Any]],
    *,
    custom: bool = True,
    multiple: bool = False,
    selected_indexes: set[int] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    selected_indexes = selected_indexes or set()
    for option_index, option in enumerate(options):
        label = str(option.get("label") or f"Option {option_index + 1}")
        if multiple:
            label = f"{'☑' if option_index in selected_indexes else '☐'} {label}"
        rows.append(
            [
                InlineKeyboardButton(
                    label[:64], callback_data=f"qst:{token}:{question_index}:{option_index}"
                )
            ]
        )
    if multiple:
        rows.append([InlineKeyboardButton("Done", callback_data=f"qstd:{token}:{question_index}")])
    if custom:
        rows.append(
            [
                InlineKeyboardButton(
                    "Type your own answer", callback_data=f"qstc:{token}:{question_index}"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _format_question(question: dict[str, Any]) -> str:
    header = str(question.get("header") or "Question")
    prompt = str(question.get("question") or "Choose one option.")
    lines = [f"❓ **{header}**", prompt]
    options = question.get("options")
    if isinstance(options, list) and options:
        lines.append("")
        for option in options:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "Option")
            description = str(option.get("description") or "")
            lines.append(f"- **{label}** — {description}" if description else f"- **{label}**")
    return "\n".join(lines)
