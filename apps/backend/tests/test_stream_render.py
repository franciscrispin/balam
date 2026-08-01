"""The pure rendering half of the stream (:mod:`balam.stream_render`).

Splitting rendering out of the transport made these testable by comparing
strings, with no transport double involved — so this covers the per-tool
branches directly rather than driving a whole turn to reach one of them.

What is being pinned is what the owner *reads*: the one-line argument summary
under each tool call, and the phrase summarising a collapsed burst of calls. A
wrong branch here silently shows the wrong file, or says "searched" for
something that was never a search.
"""

from __future__ import annotations

from balam.stream_render import (
    _apply_patch_files,
    _group_phrase,
    _relpath,
    _render_tool_part,
    _tool_summary,
)
from balam.tools import Tool

WORKSPACE = "/home/owner/proj"


def summary(tool: str, tool_input: dict[str, object], directory: str | None = WORKSPACE) -> str:
    return _tool_summary(tool, tool_input, directory)


class TestRelpath:
    def test_paths_inside_the_workspace_are_shown_relative(self) -> None:
        assert _relpath(f"{WORKSPACE}/src/app.py", WORKSPACE) == "src/app.py"

    def test_paths_outside_the_workspace_stay_absolute(self) -> None:
        # An additional_directories path must stay recognisable as elsewhere.
        assert _relpath("/etc/hosts", WORKSPACE) == "/etc/hosts"

    def test_no_workspace_leaves_the_path_alone(self) -> None:
        assert _relpath("/etc/hosts", None) == "/etc/hosts"

    def test_empty_path_stays_empty(self) -> None:
        assert _relpath("", WORKSPACE) == ""


class TestToolSummary:
    def test_file_tools_summarise_to_their_path(self) -> None:
        for tool in (Tool.READ, Tool.EDIT, Tool.WRITE):
            assert summary(tool, {"filePath": f"{WORKSPACE}/src/app.py"}) == "src/app.py"

    def test_list_uses_path_not_file_path(self) -> None:
        assert summary(Tool.LIST, {"path": f"{WORKSPACE}/src"}) == "src"

    def test_glob_shows_its_pattern(self) -> None:
        assert summary(Tool.GLOB, {"pattern": "**/*.py"}) == "**/*.py"

    def test_grep_without_a_path_is_just_the_pattern(self) -> None:
        assert summary(Tool.GREP, {"pattern": "TODO"}) == "TODO"

    def test_grep_with_a_path_reads_as_pattern_in_place(self) -> None:
        assert summary(Tool.GREP, {"pattern": "TODO", "path": f"{WORKSPACE}/src"}) == "TODO in src"

    def test_webfetch_shows_the_url(self) -> None:
        assert summary(Tool.WEBFETCH, {"url": "https://example.com"}) == "https://example.com"

    def test_task_prefers_description_over_subagent_type(self) -> None:
        assert summary(Tool.TASK, {"description": "audit deps", "subagent_type": "x"}) == (
            "audit deps"
        )

    def test_task_falls_back_to_subagent_type(self) -> None:
        assert summary(Tool.AGENT, {"subagent_type": "Explore"}) == "Explore"

    def test_apply_patch_lists_the_files_it_touches(self) -> None:
        # The raw patchText envelope is huge and breaks MarkdownV2, so the
        # summary is the file list parsed out of its headers.
        patch = (
            "*** Begin Patch\n"
            f"*** Update File: {WORKSPACE}/a.py\n"
            f"*** Add File: {WORKSPACE}/b.py\n"
            "*** End Patch\n"
        )
        assert summary(Tool.APPLY_PATCH, {"patchText": patch}) == "a.py, b.py"

    def test_unknown_tool_falls_back_to_its_first_string_argument(self) -> None:
        assert summary("mcp__server__thing", {"n": 3, "q": "hello"}) == "hello"

    def test_fallback_caps_a_long_argument(self) -> None:
        assert len(summary("mcp__server__thing", {"q": "x" * 500})) == 80

    def test_no_usable_argument_summarises_to_nothing(self) -> None:
        assert summary("mcp__server__thing", {"n": 3}) == ""


class TestApplyPatchFiles:
    def test_parses_every_envelope_header_kind(self) -> None:
        patch = (
            "*** Add File: a.py\n*** Update File: b.py\n*** Delete File: c.py\n*** Move to: d.py\n"
        )
        assert _apply_patch_files(patch) == ["a.py", "b.py", "c.py", "d.py"]

    def test_ignores_body_lines_and_blank_paths(self) -> None:
        patch = "*** Add File: a.py\n+some added line\n*** Update File: \n"
        assert _apply_patch_files(patch) == ["a.py"]

    def test_empty_envelope_yields_nothing(self) -> None:
        assert _apply_patch_files("") == []


def entry(tool: str, tool_input: dict[str, object] | None = None) -> tuple:
    """A GroupEntry: (tool, input, status, output, error)."""
    return (tool, tool_input or {}, "completed", None, None)


class TestGroupPhrase:
    """The one-line summary standing in for a collapsed burst of tool calls."""

    def test_a_single_read_reads_as_one_file(self) -> None:
        assert _group_phrase([entry(Tool.READ, {"filePath": "a.py"})], running=False) == (
            "Read a file"
        )

    def test_grep_and_glob_both_count_as_searches(self) -> None:
        phrase = _group_phrase(
            [entry(Tool.GREP, {"pattern": "x"}), entry(Tool.GLOB, {"pattern": "*.py"})],
            running=False,
        )
        assert phrase == "Searched for 2 patterns"

    def test_categories_are_comma_joined_in_a_fixed_order(self) -> None:
        phrase = _group_phrase(
            [
                entry(Tool.BASH, {"command": "ls"}),
                entry(Tool.READ, {"filePath": "a.py"}),
                entry(Tool.GREP, {"pattern": "x"}),
            ],
            running=False,
        )
        assert phrase == "Ran a command, read a file, searched for a pattern"

    def test_counts_listings(self) -> None:
        assert "list" in _group_phrase([entry(Tool.LIST, {"path": "."})], running=False).lower()

    def test_counts_web_fetches(self) -> None:
        phrase = _group_phrase([entry(Tool.WEBFETCH, {"url": "https://x"})], running=False)
        assert "fetch" in phrase.lower()

    def test_counts_web_searches(self) -> None:
        phrase = _group_phrase([entry(Tool.WEBSEARCH, {"query": "x"})], running=False)
        assert "web" in phrase.lower()

    def test_counts_subagent_tasks(self) -> None:
        phrase = _group_phrase([entry(Tool.TASK, {"description": "d"})], running=False)
        assert phrase != ""

    def test_edits_are_deduplicated_by_path(self) -> None:
        # The same file edited twice is one changed file, not two.
        same = _group_phrase(
            [entry(Tool.EDIT, {"filePath": "a.py"}), entry(Tool.EDIT, {"filePath": "a.py"})],
            running=False,
        )
        different = _group_phrase(
            [entry(Tool.EDIT, {"filePath": "a.py"}), entry(Tool.EDIT, {"filePath": "b.py"})],
            running=False,
        )
        assert same == "Edited a file"
        assert different == "Edited 2 files"

    def test_write_counts_as_an_edit(self) -> None:
        assert _group_phrase([entry(Tool.WRITE, {"filePath": "a.py"})], running=False) == (
            "Edited a file"
        )

    def test_apply_patch_contributes_its_files_to_the_edit_count(self) -> None:
        patch = "*** Update File: a.py\n*** Add File: b.py\n"
        phrase = _group_phrase([entry(Tool.APPLY_PATCH, {"patchText": patch})], running=False)
        assert phrase == "Edited 2 files"

    def test_an_edit_without_a_path_still_counts_once(self) -> None:
        # Dedup keys on the path; a missing one must not collapse distinct calls.
        phrase = _group_phrase([entry(Tool.EDIT), entry(Tool.EDIT)], running=False)
        assert phrase == "Edited 2 files"

    def test_unknown_tools_still_produce_a_phrase(self) -> None:
        assert _group_phrase([entry("mcp__server__thing")], running=False) != ""

    def test_running_and_finished_phrasings_differ(self) -> None:
        calls = [entry(Tool.GREP, {"pattern": "x"})]
        assert _group_phrase(calls, running=True) != _group_phrase(calls, running=False)


class TestRenderToolPart:
    def test_bash_shows_its_description_and_command(self) -> None:
        line = _render_tool_part(
            Tool.BASH,
            {"command": "ls -la", "description": "list files"},
            "completed",
            None,
            None,
            WORKSPACE,
        )
        assert "list files" in line and "ls -la" in line

    def test_successful_bash_output_is_omitted(self) -> None:
        # Successful output is the noise the stream drowns in; the Mini App has it.
        line = _render_tool_part(
            Tool.BASH, {"command": "ls"}, "completed", "lots of output", None, WORKSPACE
        )
        assert "lots of output" not in line

    def test_failed_bash_keeps_the_error_tail(self) -> None:
        line = _render_tool_part(
            Tool.BASH, {"command": "ls"}, "error", None, "boom: no such file", WORKSPACE
        )
        assert "boom: no such file" in line

    def test_non_bash_tools_render_as_a_labelled_one_liner(self) -> None:
        line = _render_tool_part(
            Tool.READ, {"filePath": f"{WORKSPACE}/a.py"}, "completed", None, None, WORKSPACE
        )
        assert "Read" in line and "a.py" in line

    def test_a_failed_call_is_marked(self) -> None:
        ok = _render_tool_part(Tool.READ, {"filePath": "a.py"}, "completed", None, None, None)
        bad = _render_tool_part(Tool.READ, {"filePath": "a.py"}, "error", None, None, None)
        assert bad != ok

    def test_an_unknown_tool_falls_back_to_its_wire_name(self) -> None:
        line = _render_tool_part("mcp__srv__do", {"q": "x"}, "completed", None, None, None)
        assert "mcp__srv__do" in line
