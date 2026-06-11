"""Ephemeral markdown content for the Mini App viewer.

Mini App launch links in groups must travel as ``t.me/...?startapp=<param>``
with a ~64-char ``[A-Za-z0-9_-]`` budget, so the viewer loads content by a
short id rather than a file path. This store maps those ids to snapshots: the
plan text behind a "View plan" button, or a markdown file the agent delivered
via ``send_file``. Snapshots (not paths) make the button independent of later
file edits or deletion.

Entries are in-memory and expire after :data:`CONTENT_TTL_S` (mirroring
open-shrimp's preview store); expired siblings are purged lazily on ``put``.
A tapped button after expiry surfaces as the viewer's "content not found or
expired" state — the designed degradation, since Telegram buttons are
immutable.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

#: Seconds an entry stays retrievable after ``put``.
CONTENT_TTL_S = 3600.0

#: Cap per entry; markdown beyond this is cut with a truncation notice. Keeps a
#: runaway plan or report from pinning memory in a long-lived process.
MAX_CONTENT_BYTES = 1_048_576

_TRUNCATION_NOTICE = "\n\n…(truncated)"


@dataclass(frozen=True)
class ContentEntry:
    title: str
    content: str
    created: float


class ContentStore:
    """In-memory id → markdown snapshot map with a TTL.

    ``clock`` is injectable for tests; it must be monotonic (the default is
    :func:`time.monotonic`), so entries never expire early on wall-clock jumps.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, ContentEntry] = {}

    def put(self, title: str, content: str) -> str:
        now = self._clock()
        expired = [k for k, v in self._entries.items() if now - v.created > CONTENT_TTL_S]
        for key in expired:
            del self._entries[key]
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            budget = MAX_CONTENT_BYTES - len(_TRUNCATION_NOTICE.encode("utf-8"))
            content = (
                content.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
                + _TRUNCATION_NOTICE
            )
        content_id = uuid.uuid4().hex[:12]
        self._entries[content_id] = ContentEntry(title=title, content=content, created=now)
        return content_id

    def get(self, content_id: str) -> ContentEntry | None:
        entry = self._entries.get(content_id)
        if entry is None:
            return None
        if self._clock() - entry.created > CONTENT_TTL_S:
            del self._entries[content_id]
            return None
        return entry
