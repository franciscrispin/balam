"""Invariants of the canonical tool registry (:mod:`balam.tools`).

The registry exists so four consumers stop keeping their own copy of the tool
vocabulary. These tests guard the properties those consumers rely on, so a new
:data:`~balam.tools.REGISTRY` entry cannot quietly break one of them.
"""

from __future__ import annotations

from balam.tools import (
    CATEGORY_BY_SDK_NAME,
    DISPLAY_BY_WIRE,
    FILE_PATH_CATEGORIES,
    MUTATING_TOOLS,
    REGISTRY,
    WIRE_BY_SDK_NAME,
    Permission,
    Tool,
)


def test_wire_names_are_unique() -> None:
    """One entry per wire name — two would make the derived dicts lossy."""
    wires = [spec.wire for spec in REGISTRY]
    assert len(wires) == len(set(wires))


def test_every_tool_has_a_display_label() -> None:
    assert all(spec.display for spec in REGISTRY)
    assert set(DISPLAY_BY_WIRE) == {spec.wire for spec in REGISTRY}


def test_sdk_names_map_to_exactly_one_tool() -> None:
    """An SDK name appearing under two specs would resolve arbitrarily."""
    sdk_names = [name for spec in REGISTRY for name in spec.sdk_names]
    assert len(sdk_names) == len(set(sdk_names))


def test_sdk_maps_agree_on_their_keys() -> None:
    """Both SDK-keyed lookups must cover the same names, or a tool would render
    under one vocabulary and be permission-checked under another."""
    assert set(WIRE_BY_SDK_NAME) == set(CATEGORY_BY_SDK_NAME)


def test_file_mutations_share_the_edit_permission() -> None:
    """OpenCode folds edit/write/apply_patch into the single ``edit`` category."""
    assert MUTATING_TOOLS == {Tool.EDIT, Tool.WRITE, Tool.APPLY_PATCH}
    assert all(
        CATEGORY_BY_SDK_NAME[name] == Permission.EDIT
        for name in ("Edit", "Write", "MultiEdit", "NotebookEdit")
    )


def test_agent_is_an_alias_for_the_task_permission() -> None:
    """``agent`` is a distinct tool but gates on ``task`` — the asymmetry the
    registry exists to record."""
    agent = next(spec for spec in REGISTRY if spec.wire is Tool.AGENT)
    assert agent.permission is Permission.TASK


def test_known_sdk_names_translate_to_opencode_wire_names() -> None:
    """The SDK↔OpenCode mapping used to live only in the SDK backend."""
    assert WIRE_BY_SDK_NAME["LS"] == Tool.LIST
    assert WIRE_BY_SDK_NAME["MultiEdit"] == Tool.EDIT
    assert WIRE_BY_SDK_NAME["NotebookEdit"] == Tool.EDIT
    assert WIRE_BY_SDK_NAME["WebSearch"] == Tool.WEBSEARCH


def test_file_path_categories_are_permissions_not_tools() -> None:
    """These key the path-scoping branch in :func:`balam.permissions.build_ruleset`."""
    assert FILE_PATH_CATEGORIES == {
        Permission.READ,
        Permission.EDIT,
        Permission.GLOB,
        Permission.GREP,
        Permission.LIST,
    }
