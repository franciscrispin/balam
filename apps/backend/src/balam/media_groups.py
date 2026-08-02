"""Telegram albums → one turn.

An album is sent as *several* messages that share a ``media_group_id``, and the
Bot API (checked at 10.2) offers no signal that the last one has arrived — no
index within the group, no total count, no "is last" flag, and no separate
update. Waiting a moment for the siblings is therefore the only way to see a
whole album at once, and this module holds that wait:
:class:`MediaGroupBuffer` parks a group's messages so a timer can flush them as
a single turn.

Getting the wait *too short* is safe by construction. A straggler that misses
the window is just an ordinary mid-turn message, so it folds into the running
turn as a follow-up — which is exactly what every photo after the first did
before this existed. The failure mode is the old behavior, not a lost photo.
"""

from __future__ import annotations

from typing import Any

#: How long to hold an album's first message before dispatching the group. The
#: messages come from one ``sendMediaGroup`` call and land back to back, so this
#: only has to outlast Telegram's own delivery spread.
DEBOUNCE_SECONDS = 1.5


class MediaGroupBuffer:
    """The messages of each in-flight album, keyed by ``media_group_id``."""

    def __init__(self) -> None:
        self._groups: dict[str, list[Any]] = {}

    def add(self, group_id: str, message: Any) -> bool:
        """Park ``message`` under its group.

        Returns whether this message *opened* the group, which is the caller's
        cue to schedule the flush — so one timer runs per album, not per photo.
        """
        group = self._groups.setdefault(group_id, [])
        opened = not group
        group.append(message)
        return opened

    def take(self, group_id: str) -> list[Any]:
        """Remove and return the group's messages in arrival order (``[]`` if the
        group is unknown, e.g. a second flush for one already dispatched)."""
        return self._groups.pop(group_id, [])
