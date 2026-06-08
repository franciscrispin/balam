"""Working-tree diff → structured hunks for the Mini App diff viewer (ADR-0003).

The diff viewer is the flagship Mini App surface. The backend does the parsing
(`git diff` of a context's working directory) and hands the frontend pre-parsed,
syntax-highlight-ready hunks; the frontend only renders + highlights them. The
shapes here ARE the API contract — FastAPI emits them into the OpenAPI schema,
from which the frontend's TypeScript types are generated (ADR-0003), so they
mirror ``packages/shared``'s ``DiffHunk``/``HunkLine`` field-for-field.

Read-only: every command is a plain ``git diff`` (no index writes), scoped to the
context ``directory``. Adapted from the open-shrimp reference (ADR-0011); trimmed
to a single repo (no submodules/pagination) for this slice.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: File extension → Shiki language id for syntax highlighting.
_EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".sql": "sql",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".toml": "toml",
    ".xml": "xml",
    ".lua": "lua",
    ".php": "php",
    ".swift": "swift",
    ".proto": "protobuf",
}


class HunkLine(BaseModel):
    """A single line within a diff hunk. ``old_no``/``new_no`` are null where the
    line is absent on that side (added lines have no old number, etc.)."""

    type: Literal["context", "add", "delete"]
    old_no: int | None
    new_no: int | None
    content: str


class DiffHunk(BaseModel):
    """One contiguous hunk of a file's diff, pre-parsed for the frontend."""

    id: str
    file_path: str
    #: Shiki language id (e.g. "typescript", "python"); "text" when unknown.
    language: str
    is_binary: bool
    is_empty: bool
    #: The ``@@ -a,b +c,d @@`` header (or a synthetic label for binary/empty files).
    hunk_header: str
    lines: list[HunkLine]


def detect_language(file_path: str) -> str:
    """Map a path to a Shiki language id by extension (or special basename)."""
    basename = file_path.rsplit("/", 1)[-1].lower()
    if basename in ("dockerfile", "containerfile"):
        return "dockerfile"
    if basename == "makefile":
        return "makefile"
    dot_idx = file_path.rfind(".")
    if dot_idx == -1:
        return "text"
    # Lower-case the extension so e.g. README.MD / Main.PY still highlight.
    return _EXT_TO_LANGUAGE.get(file_path[dot_idx:].lower(), "text")


def _hunk_id(file_path: str, hunk_header: str, lines: list[HunkLine]) -> str:
    """Stable, deterministic id for a hunk — used as a React key, so it must be
    unique across the result and reproducible across requests."""
    parts = [file_path, "\n", hunk_header, "\n"]
    parts.extend(f"{line.type}:{line.content}\n" for line in lines)
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:16]


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_BINARY_RE = re.compile(r"^Binary files .* and .* differ$")
_LARGE_FILE_RE = re.compile(r"^Large file \(.+\) skipped$")


def parse_diff(diff_text: str) -> list[DiffHunk]:
    """Parse unified ``git diff`` output into :class:`DiffHunk` objects."""
    hunks: list[DiffHunk] = []
    if not diff_text.strip():
        return hunks

    lines = diff_text.split("\n")
    i = 0
    while i < len(lines):
        header_match = _DIFF_HEADER_RE.match(lines[i])
        if not header_match:
            i += 1
            continue

        # Destination ("b") path is the canonical file path.
        file_path = header_match.group(2)
        i += 1

        is_new_file = False
        is_deleted_file = False
        is_binary = False

        # Extended header lines (mode changes, binary marker, …) until the body.
        while i < len(lines) and not lines[i].startswith("---") and not lines[i].startswith("@@"):
            if lines[i].startswith("new file mode"):
                is_new_file = True
            elif lines[i].startswith("deleted file mode"):
                is_deleted_file = True
            elif _BINARY_RE.match(lines[i]) or _LARGE_FILE_RE.match(lines[i]):
                is_binary = True
            if _DIFF_HEADER_RE.match(lines[i]):
                break
            i += 1

        if is_binary:
            hunks.append(
                DiffHunk(
                    id=_hunk_id(file_path, "(binary)", []),
                    file_path=file_path,
                    language=detect_language(file_path),
                    is_binary=True,
                    is_empty=False,
                    hunk_header="(binary)",
                    lines=[],
                )
            )
            continue

        # Skip the ---/+++ file lines.
        while i < len(lines) and (lines[i].startswith("---") or lines[i].startswith("+++")):
            i += 1

        # No hunk body: a genuinely new/deleted *empty* file (e.g. __init__.py)
        # still deserves a card; a bare mode change does not.
        if i >= len(lines) or not _HUNK_HEADER_RE.match(lines[i]):
            if is_new_file or is_deleted_file:
                hunks.append(
                    DiffHunk(
                        id=_hunk_id(file_path, "(empty file)", []),
                        file_path=file_path,
                        language=detect_language(file_path),
                        is_binary=False,
                        is_empty=True,
                        hunk_header="(empty file)",
                        lines=[],
                    )
                )
            continue

        # Parse each hunk body for this file.
        while i < len(lines):
            hunk_match = _HUNK_HEADER_RE.match(lines[i])
            if not hunk_match:
                break

            hunk_header = lines[i]
            old_no = int(hunk_match.group(1))
            new_no = int(hunk_match.group(3))
            i += 1

            hunk_lines: list[HunkLine] = []
            while i < len(lines):
                line = lines[i]
                if _HUNK_HEADER_RE.match(line) or _DIFF_HEADER_RE.match(line):
                    break
                if line.startswith("+"):
                    hunk_lines.append(
                        HunkLine(type="add", old_no=None, new_no=new_no, content=line[1:])
                    )
                    new_no += 1
                elif line.startswith("-"):
                    hunk_lines.append(
                        HunkLine(type="delete", old_no=old_no, new_no=None, content=line[1:])
                    )
                    old_no += 1
                elif line.startswith(" "):
                    hunk_lines.append(
                        HunkLine(type="context", old_no=old_no, new_no=new_no, content=line[1:])
                    )
                    old_no += 1
                    new_no += 1
                elif line == "\\ No newline at end of file":
                    pass  # git marker, not a content line
                elif line == "":
                    # A blank line is the end of the diff, or a context line whose
                    # single trailing space git stripped.
                    if (
                        i + 1 >= len(lines)
                        or _HUNK_HEADER_RE.match(lines[i + 1])
                        or _DIFF_HEADER_RE.match(lines[i + 1])
                    ):
                        i += 1
                        break
                    hunk_lines.append(
                        HunkLine(type="context", old_no=old_no, new_no=new_no, content="")
                    )
                    old_no += 1
                    new_no += 1
                i += 1

            if hunk_lines:
                hunks.append(
                    DiffHunk(
                        id=_hunk_id(file_path, hunk_header, hunk_lines),
                        file_path=file_path,
                        language=detect_language(file_path),
                        is_binary=False,
                        is_empty=False,
                        hunk_header=hunk_header,
                        lines=hunk_lines,
                    )
                )

    return hunks


async def _run_git(cwd: str, *args: str) -> tuple[str, str, int]:
    """Run a git command as an async subprocess → ``(stdout, stderr, returncode)``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(errors="replace"), stderr.decode(errors="replace"), proc.returncode or 0


async def _untracked_files(cwd: str) -> list[str]:
    stdout, _, rc = await _run_git(cwd, "ls-files", "--others", "--exclude-standard")
    if rc != 0:
        return []
    return [f for f in stdout.strip().split("\n") if f]


def _is_binary_file(path: Path, sample: int = 8192) -> bool:
    """Git's heuristic: a null byte in the first chunk means binary."""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(sample)
    except OSError:
        return False


_MAX_UNTRACKED_DIFF_SIZE = 1_000_000  # 1 MB

#: Cap on concurrent ``git diff --no-index`` subprocesses. A working tree with a
#: non-ignored ``node_modules``/``build`` dir can list thousands of untracked
#: files; without a bound we'd fork one git process per file at once.
_UNTRACKED_DIFF_CONCURRENCY = 16


def _classify_untracked(cwd: str, files: list[str]) -> tuple[list[str], list[str]]:
    """Split untracked files into ``(text_files, synthetic_headers)``.

    Pure blocking I/O (``stat`` + a binary sniff per file), so callers run it via
    a thread to keep it off the event loop. Files that vanished between
    ``ls-files`` and now (TOCTOU — the agent may be editing this very tree) are
    skipped: a gone file has no diff.
    """
    text_files: list[str] = []
    synthetic: list[str] = []
    for file_path in files:
        full = Path(cwd) / file_path
        try:
            size = full.stat().st_size
        except OSError:
            continue  # listed but gone now — skip
        if _is_binary_file(full):
            synthetic.append(
                f"diff --git a/{file_path} b/{file_path}\n"
                f"new file mode 100644\n"
                f"Binary files /dev/null and b/{file_path} differ\n"
            )
        elif size > _MAX_UNTRACKED_DIFF_SIZE:
            synthetic.append(
                f"diff --git a/{file_path} b/{file_path}\n"
                f"new file mode 100644\n"
                f"Large file ({size / 1_000_000:.1f} MB) skipped\n"
            )
        else:
            text_files.append(file_path)
    return text_files, synthetic


async def _diff_untracked(cwd: str, files: list[str]) -> str:
    """Unified diff for untracked files, without touching the index.

    Binary and oversized files get a synthetic header (so they show as a card,
    not a wall of bytes); the rest are diffed via ``git diff --no-index``, capped
    at :data:`_UNTRACKED_DIFF_CONCURRENCY` concurrent subprocesses.
    """
    if not files:
        return ""

    # Classification is blocking stat/read I/O — offload so it doesn't stall the
    # shared event loop (bot, OpenCode SSE, other HTTP) on a big untracked tree.
    text_files, synthetic = await asyncio.to_thread(_classify_untracked, cwd, files)

    sem = asyncio.Semaphore(_UNTRACKED_DIFF_CONCURRENCY)

    async def _one(file_path: str) -> str:
        async with sem:
            stdout, _, rc = await _run_git(
                cwd, "diff", "--no-index", "--no-color", "-U3", "--", "/dev/null", file_path
            )
        # --no-index exits 1 when files differ — that is the expected case here.
        if rc not in (0, 1):
            logger.warning("git diff --no-index failed for %s (rc=%d)", file_path, rc)
            return ""
        return stdout

    text_diffs = await asyncio.gather(*[_one(f) for f in text_files])
    return "\n".join(synthetic + [d for d in text_diffs if d])


class NotAGitRepo(ValueError):
    """Raised when the context directory is not inside a git repository."""


async def get_hunks(directory: str) -> list[DiffHunk]:
    """All working-tree hunks for ``directory``: staged + unstaged + untracked.

    Read-only. Staged hunks come first so a file's changes stay contiguous.
    Raises :class:`NotAGitRepo` when ``directory`` is not a git working tree.
    """
    _, _, rc = await _run_git(directory, "rev-parse", "--git-dir")
    if rc != 0:
        raise NotAGitRepo(f"not inside a git repository: {directory}")

    unstaged_task = asyncio.ensure_future(
        _run_git(directory, "diff", "--no-color", "-U3", "--ignore-submodules=dirty")
    )
    staged_task = asyncio.ensure_future(
        _run_git(directory, "diff", "--cached", "--no-color", "-U3", "--ignore-submodules=dirty")
    )
    untracked = await _untracked_files(directory)
    untracked_task = (
        asyncio.ensure_future(_diff_untracked(directory, untracked)) if untracked else None
    )

    unstaged_out, unstaged_err, unstaged_rc = await unstaged_task
    staged_out, staged_err, staged_rc = await staged_task
    untracked_out = await untracked_task if untracked_task is not None else ""

    if unstaged_rc != 0:
        logger.warning("git diff failed in %s: %s", directory, unstaged_err.strip())
    if staged_rc != 0:
        logger.warning("git diff --cached failed in %s: %s", directory, staged_err.strip())

    return parse_diff(staged_out) + parse_diff(unstaged_out) + parse_diff(untracked_out)
