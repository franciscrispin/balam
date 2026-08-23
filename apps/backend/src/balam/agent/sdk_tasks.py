"""The CLI's task list, mirrored into Balam's todo vocabulary.

Newer CLI harnesses expose a stateful ``TaskCreate``/``TaskUpdate`` pair instead
of the single-shot ``TodoWrite``: ids are assigned by the harness, and each call
mutates one row rather than restating the whole list. The streamer's live
checklist wants the whole list every time, so this module keeps the per-turn
mirror and rebuilds the full list from it.

:class:`LiveTasks` tracks something different — the *background* work the CLI has
running — which is what ADR-0015 keys the turn-holding decision on.
"""

from __future__ import annotations

import re
from typing import Any

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from balam.agent.events import BackgroundTask

#: The task-list tool family newer CLI harnesses expose *instead of*
#: ``TodoWrite``. They are stateful (create-then-update, ids assigned by the
#: harness), so the backend mirrors them into a per-turn task list and emits
#: synthetic ``todowrite`` events carrying the full list — the streamer's live
#: checklist then works with either vocabulary. The raw calls are kept out of
#: the tool stream (like ``TodoWrite`` itself); only a *failed* call surfaces.
_TASK_TOOLS = frozenset({"TaskCreate", "TaskUpdate"})

#: The task id a TaskCreate result announces ("Task #12 created successfully…").
_TASK_ID_RE = re.compile(r"#(\d+)")

#: The Artifact tool's live-updates watch (armed on every publish) surfaces as a
#: task through this same lifecycle, but unlike a real background job it is
#: meant to run for the rest of the session and never reaches a terminal state.
#: Counting it toward ADR-0015's turn-hold decision pins the topic for the full
#: 30-minute cap after the real work is already done, then reports it as cut
#: short — which is misleading (there is nothing to `setsid` here). It must
#: never enter ``LiveTasks``.
_ARTIFACT_WATCH_RE = re.compile(r"^live updates for artifact\b", re.IGNORECASE)


def _is_artifact_watch(description: str) -> bool:
    return bool(_ARTIFACT_WATCH_RE.match(description.strip()))


class LiveTasks:
    """The tasks the CLI currently has running, tracked from its task messages.

    The CLI *does* publish a whole-set ``background_tasks_changed`` system event,
    but it never reaches an SDK client — it is filtered out of the transport, so
    reading it here was dead code. What does arrive is the per-task lifecycle:
    ``task_started`` when one launches and ``task_updated`` / ``task_notification``
    as it moves. A task's terminal state can come from *either* of the latter two
    (the SDK documents that a background task may report completion only via
    ``task_updated``), so both clear it.

    Foreground work (a subagent the model waits on) shows up here too and simply
    goes terminal before the turn ends, which is why callers only need to read
    this at a turn boundary to learn what would outlive the turn.

    The Artifact tool's live-updates watch is deliberately excluded (see
    ``_is_artifact_watch``): it never goes terminal, so it would otherwise pin
    every turn that publishes an artifact for the full hold cap.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}

    @property
    def tasks(self) -> tuple[BackgroundTask, ...]:
        return tuple(self._tasks.values())

    def started(self, message: TaskStartedMessage) -> bool:
        task_id = (message.task_id or "").strip()
        if not task_id:
            return False
        description = (message.description or "").strip() or task_id
        if _is_artifact_watch(description):
            return False
        self._tasks[task_id] = BackgroundTask(
            task_id=task_id,
            description=description,
            task_type=message.task_type,
        )
        return True

    def updated(self, message: TaskUpdatedMessage) -> bool:
        task_id = (message.task_id or "").strip()
        if not task_id:
            return False
        patch = message.patch if isinstance(message.patch, dict) else {}
        if patch.get("status") in TERMINAL_TASK_STATUSES:
            return self._tasks.pop(task_id, None) is not None
        existing = self._tasks.get(task_id)
        description = str(patch.get("description") or "").strip()
        if existing is not None and description and description != existing.description:
            self._tasks[task_id] = BackgroundTask(
                task_id=task_id,
                description=description,
                task_type=existing.task_type,
            )
            return True
        return False

    def notified(self, message: TaskNotificationMessage) -> bool:
        task_id = (message.task_id or "").strip()
        if not task_id:
            return False
        if message.status in TERMINAL_TASK_STATUSES:
            return self._tasks.pop(task_id, None) is not None
        return False


def _result_text(content: Any) -> str:
    """Flatten a ToolResultBlock ``content`` payload to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _apply_task_result(
    mirror: dict[str, dict[str, str]], tool: str, tool_input: dict[str, Any], content: Any
) -> list[dict[str, str]] | None:
    """Fold one successful TaskCreate/TaskUpdate result into the turn's task
    mirror and return the todowrite-shaped ``todos`` list, or ``None`` when the
    result changed nothing renderable.

    TaskCreate's id only appears in its result text; TaskUpdate carries it in
    the input. A task first seen through an update (created in an earlier turn)
    gets a ``Task #N`` placeholder label unless the update renames it; a
    ``deleted`` status removes the item ("permanently removes the task").
    """
    if tool == "TaskCreate":
        match = _TASK_ID_RE.search(_result_text(content))
        if match is None:
            return None
        task_id = match.group(1)
        label = str(tool_input.get("subject") or "").strip()
        mirror[task_id] = {"content": label or f"Task #{task_id}", "status": "pending"}
    else:  # TaskUpdate
        task_id = str(tool_input.get("taskId") or "").strip()
        if not task_id:
            return None
        status = tool_input.get("status")
        if status == "deleted":
            if mirror.pop(task_id, None) is None:
                return None
        else:
            item = mirror.setdefault(task_id, {"content": f"Task #{task_id}", "status": "pending"})
            subject = str(tool_input.get("subject") or "").strip()
            if subject:
                item["content"] = subject
            if status in ("pending", "in_progress", "completed"):
                item["status"] = status
    return [
        dict(item)
        for _id, item in sorted(mirror.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
    ]
