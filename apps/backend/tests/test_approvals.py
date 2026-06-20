import os

from balam.approvals import (
    EDIT_CATEGORY,
    READ_CATEGORIES,
    Choice,
    PendingApprovals,
    PendingDeletions,
    Verdict,
    decide,
    is_edit,
    is_within,
    request_target_paths,
)

# --- is_within: realpath prefix check -----------------------------------------


def test_is_within_direct_child(tmp_path) -> None:
    base = str(tmp_path)
    assert is_within(os.path.join(base, "src", "foo.py"), [base]) is True


def test_is_within_exact_dir() -> None:
    assert is_within("/work/proj", ["/work/proj"]) is True


def test_is_within_rejects_outside() -> None:
    assert is_within("/etc/passwd", ["/work/proj"]) is False


def test_is_within_rejects_prefix_sibling() -> None:
    # /work/proj2 must NOT count as within /work/proj.
    assert is_within("/work/proj2/x", ["/work/proj"]) is False


def test_is_within_any_of_several_dirs() -> None:
    assert is_within("/extra/lib.py", ["/work/proj", "/extra"]) is True


def test_is_within_resolves_symlink_escape(tmp_path) -> None:
    # A symlink pointing outside the workspace can't smuggle a path inside it.
    base = tmp_path / "work"
    base.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    link = base / "link"
    link.symlink_to(outside)
    assert is_within(str(link / "f.txt"), [str(base)]) is False


def test_is_within_empty_path() -> None:
    assert is_within("", ["/work"]) is False


# --- is_edit: classification by OpenCode permission category ------------------


def test_edit_is_the_only_mutation_category() -> None:
    # edit/write/apply_patch all report category "edit"; reads are their own set.
    assert EDIT_CATEGORY == "edit"
    assert "read" in READ_CATEGORIES


def test_is_edit_true_only_for_edit_category() -> None:
    assert is_edit("edit") is True
    assert is_edit("bash") is False
    assert is_edit("read") is False


# --- decide: the directory-boundary matrix (keyed on category) ----------------

DIRS = ["/work/proj"]


def test_read_in_workspace_auto_allows() -> None:
    assert decide("read", ["/work/proj/a.py"], allowed_dirs=DIRS, accept_all_edits=False) is (
        Verdict.ALLOW
    )


def test_read_out_of_workspace_asks() -> None:
    assert decide("read", ["/etc/hosts"], allowed_dirs=DIRS, accept_all_edits=False) is Verdict.ASK


def test_read_without_a_resolvable_path_asks() -> None:
    assert decide("read", [], allowed_dirs=DIRS, accept_all_edits=False) is Verdict.ASK


def test_glob_in_workspace_allows() -> None:
    assert (
        decide("glob", ["/work/proj"], allowed_dirs=DIRS, accept_all_edits=False) is Verdict.ALLOW
    )


def test_edit_in_workspace_asks_without_accept_all() -> None:
    assert decide("edit", ["/work/proj/a.py"], allowed_dirs=DIRS, accept_all_edits=False) is (
        Verdict.ASK
    )


def test_edit_in_workspace_allows_with_accept_all() -> None:
    assert decide("edit", ["/work/proj/a.py"], allowed_dirs=DIRS, accept_all_edits=True) is (
        Verdict.ALLOW
    )


def test_edit_multifile_with_one_out_of_scope_asks_even_with_accept_all() -> None:
    # A multi-file apply_patch: a single out-of-workspace target still prompts.
    v = decide("edit", ["/work/proj/a.py", "/etc/x"], allowed_dirs=DIRS, accept_all_edits=True)
    assert v is Verdict.ASK


def test_edit_without_paths_asks_even_with_accept_all() -> None:
    assert decide("edit", [], allowed_dirs=DIRS, accept_all_edits=True) is Verdict.ASK


def test_bash_category_always_asks() -> None:
    assert decide("bash", [], allowed_dirs=DIRS, accept_all_edits=True) is Verdict.ASK


def test_unknown_category_asks() -> None:
    assert decide("webfetch", [], allowed_dirs=DIRS, accept_all_edits=True) is Verdict.ASK


# --- request_target_paths: pull paths from the permission request -------------


def test_edit_paths_from_metadata_files() -> None:
    # apply_patch lists every touched file in metadata.files[].filePath.
    meta = {"files": [{"filePath": "/work/proj/a.py"}, {"filePath": "/work/proj/b.py"}]}
    assert request_target_paths("edit", meta, {}, "/work/proj") == [
        "/work/proj/a.py",
        "/work/proj/b.py",
    ]


def test_edit_paths_resolve_relative_metadata_filepath() -> None:
    paths = request_target_paths("edit", {"filepath": "sub/a.py"}, {}, "/work/proj")
    assert paths == ["/work/proj/sub/a.py"]


def test_edit_paths_fall_back_to_tool_input_filepath() -> None:
    paths = request_target_paths("edit", {}, {"filePath": "/work/proj/a.py"}, "/work/proj")
    assert paths == ["/work/proj/a.py"]


def test_read_paths_from_tool_input() -> None:
    assert request_target_paths("read", {}, {"filePath": "/work/proj/a.py"}, "/work/proj") == [
        "/work/proj/a.py"
    ]


def test_glob_path_defaults_to_workspace() -> None:
    assert request_target_paths("glob", {}, {"pattern": "*.py"}, "/work/proj") == ["/work/proj"]


def test_bash_has_no_target_paths() -> None:
    assert request_target_paths("bash", {}, {"command": "ls"}, "/work/proj") == []


# --- PendingApprovals ---------------------------------------------------------


async def test_register_and_resolve_allow() -> None:
    pending = PendingApprovals()
    token, future = pending.register("ses_1")
    assert pending.resolve(token, Choice.ALLOW) is True
    assert await future is Choice.ALLOW


async def test_resolve_all_sets_accept_all_edits() -> None:
    pending = PendingApprovals()
    token, future = pending.register("ses_1")
    assert pending.is_accept_all_edits("ses_1") is False
    pending.resolve(token, Choice.ALL)
    assert await future is Choice.ALL
    assert pending.is_accept_all_edits("ses_1") is True
    # Other sessions are unaffected.
    assert pending.is_accept_all_edits("ses_2") is False


async def test_resolve_unknown_token_returns_false() -> None:
    pending = PendingApprovals()
    assert pending.resolve("nope", Choice.ALLOW) is False


async def test_resolve_twice_returns_false_second_time() -> None:
    pending = PendingApprovals()
    token, _future = pending.register("ses_1")
    assert pending.resolve(token, Choice.DENY) is True
    assert pending.resolve(token, Choice.ALLOW) is False


async def test_discard_makes_token_unresolvable() -> None:
    pending = PendingApprovals()
    token, _future = pending.register("ses_1")
    pending.discard(token)
    assert pending.resolve(token, Choice.ALLOW) is False


# --- PendingDeletions: /delete topic picker -----------------------------------


def test_deletions_register_starts_with_nothing_selected() -> None:
    pending = PendingDeletions()
    token = pending.register(100, [(5, "First"), (7, "Second")])
    assert pending.chat_id(token) == 100
    assert pending.entries(token) == [(5, "First", False), (7, "Second", False)]
    assert pending.selected_thread_ids(token) == []


def test_deletions_toggle_flips_and_reports_selection_in_order() -> None:
    pending = PendingDeletions()
    token = pending.register(100, [(5, "First"), (7, "Second")])
    assert pending.toggle(token, 7) is True
    assert pending.toggle(token, 5) is True
    assert pending.toggle(token, 7) is False  # toggling again unselects
    # Reported in display order, not selection order.
    assert pending.selected_thread_ids(token) == [5]
    assert pending.entries(token) == [(5, "First", True), (7, "Second", False)]


def test_deletions_toggle_unknown_thread_or_token_returns_none() -> None:
    pending = PendingDeletions()
    token = pending.register(100, [(5, "First")])
    assert pending.toggle(token, 999) is None
    assert pending.toggle("nope", 5) is None


def test_deletions_discard_expires_the_token() -> None:
    pending = PendingDeletions()
    token = pending.register(100, [(5, "First")])
    pending.discard(token)
    assert pending.entries(token) is None
    assert pending.selected_thread_ids(token) is None
    assert pending.chat_id(token) is None
