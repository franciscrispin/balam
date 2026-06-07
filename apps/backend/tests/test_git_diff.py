"""Tests for the working-tree diff parser (balam.git_diff)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from balam.git_diff import NotAGitRepo, detect_language, get_hunks, parse_diff

# A canned unified diff (deterministic — no git needed) covering add/delete/context.
_DIFF = """\
diff --git a/apps/backend/src/balam/streamer.py b/apps/backend/src/balam/streamer.py
index 1111111..2222222 100644
--- a/apps/backend/src/balam/streamer.py
+++ b/apps/backend/src/balam/streamer.py
@@ -12,4 +12,5 @@ def draft(text):
     trimmed = text.strip()
-    return trimmed
+    if not trimmed:
+        return None
+    return trimmed[:MAX_LEN]
"""


def test_parse_diff_classifies_lines_with_numbers() -> None:
    hunks = parse_diff(_DIFF)
    assert len(hunks) == 1
    hunk = hunks[0]
    assert hunk.file_path == "apps/backend/src/balam/streamer.py"
    assert hunk.language == "python"
    assert hunk.is_binary is False
    assert hunk.is_empty is False
    assert hunk.hunk_header.startswith("@@ -12,4 +12,5 @@")

    kinds = [(line.type, line.old_no, line.new_no) for line in hunk.lines]
    assert ("context", 12, 12) in kinds
    assert ("delete", 13, None) in kinds
    assert ("add", None, 13) in kinds
    # Numbering advances independently on each side.
    adds = [line for line in hunk.lines if line.type == "add"]
    assert [line.new_no for line in adds] == [13, 14, 15]


def test_parse_diff_empty_input() -> None:
    assert parse_diff("") == []
    assert parse_diff("\n  \n") == []


def test_parse_diff_stable_ids_unique_and_reproducible() -> None:
    first = parse_diff(_DIFF)
    again = parse_diff(_DIFF)
    assert first[0].id == again[0].id  # reproducible
    assert len({h.id for h in first}) == len(first)  # unique


def test_parse_diff_binary_file() -> None:
    diff = (
        "diff --git a/logo.png b/logo.png\n"
        "new file mode 100644\n"
        "Binary files /dev/null and b/logo.png differ\n"
    )
    (hunk,) = parse_diff(diff)
    assert hunk.is_binary is True
    assert hunk.lines == []
    assert hunk.hunk_header == "(binary)"


def test_parse_diff_new_empty_file() -> None:
    diff = "diff --git a/__init__.py b/__init__.py\nnew file mode 100644\n"
    (hunk,) = parse_diff(diff)
    assert hunk.is_empty is True
    assert hunk.lines == []


def test_detect_language() -> None:
    assert detect_language("a/b/c.ts") == "typescript"
    assert detect_language("x.py") == "python"
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("README") == "text"


async def test_get_hunks_on_temp_repo(git_repo: Path) -> None:
    # Modify the tracked file and add an untracked one.
    (git_repo / "hello.py").write_text("def hello():\n    return 2\n")
    (git_repo / "extra.txt").write_text("brand new\n")

    hunks = await get_hunks(str(git_repo))
    by_path = {h.file_path: h for h in hunks}

    assert "hello.py" in by_path
    modified = by_path["hello.py"]
    assert any(line.type == "delete" and "return 1" in line.content for line in modified.lines)
    assert any(line.type == "add" and "return 2" in line.content for line in modified.lines)

    # Untracked file surfaces as an addition.
    assert "extra.txt" in by_path
    assert any(line.type == "add" for line in by_path["extra.txt"].lines)


async def test_get_hunks_clean_tree_is_empty(git_repo: Path) -> None:
    assert await get_hunks(str(git_repo)) == []


async def test_get_hunks_rejects_non_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NotAGitRepo):
        await get_hunks(str(plain))


def test_git_repo_fixture_is_a_repo(git_repo: Path) -> None:
    # Sanity: the fixture really initialized a repo (guards the other tests).
    out = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "true"
