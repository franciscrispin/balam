"""Inbound Telegram attachments → prompt files (tier-1 plan §4).

Two jobs, deliberately split:

*Collection* (:func:`collect_attachments`) turns whatever media a Telegram
message carries into :class:`PromptFile` records holding the raw bytes as a
``data:`` URL. It accepts **every** downloadable kind — photo, document, video,
audio, voice, video note, animation, sticker — because a bot that silently drops
a voice note is worse than one that says it cannot transcribe it.

*Persistence* (:func:`save_attachments`) writes those bytes into the workspace so
the agent's own tools can reach them. This is what makes non-inlineable types
usable at all: the model can be handed an image or a CSV directly, but a `.xlsx`,
a `.zip` or an `.m4a` is only ever going to be opened by code, and code needs a
path. Saving inside the context directory keeps those reads within the ADR-0012
boundary, so they do not raise an approval prompt.

Which files get *inlined* into the message (versus only referenced by path) is
not decided here — it is a per-backend question about what the model accepts.
See :func:`balam.agent.sdk_translate._attachment_block`.

The bytes travel inline as ``data:`` URLs rather than as paths in the prompt
text, which is what the OpenCode backend needs (``FilePartInput``: ``{type, mime,
url}``) and what lets the SDK backend build native content blocks without the
agent spending a tool call on a file it was just handed.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where attachments land, relative to the context's working directory. Inside
#: the workspace on purpose — :mod:`balam.approvals` gates reads outside it, so an
#: attachment parked in ``/tmp`` would prompt for approval (or be refused)
#: every time the agent opened the file the user just sent.
INBOX_DIRNAME = os.path.join(".balam", "attachments")

#: ``*`` matches the .gitignore itself, so the inbox stays invisible to ``git
#: status`` and ``/diff`` without editing the workspace's own ignore rules.
_SELF_IGNORE = "*\n"

#: Saved attachments are pruned after this many days. Telegram allows 20 MB per
#: file, so an unbounded inbox is a slow disk leak in someone's repo.
_RETENTION_DAYS = 14

#: Filenames arrive from Telegram, i.e. from another user's client — they may
#: contain path separators, ``..``, or control characters. Anything outside this
#: set is replaced before the name touches the filesystem.
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: Kept short enough to survive on any filesystem once the inbox prefix is added.
_MAX_NAME_LEN = 96

#: Telegram media that carry no MIME type of their own, and what to assume.
#: Photos are always JPEG; video notes are always MP4.
_ASSUMED_MIME = {"photo": "image/jpeg", "video_note": "video/mp4"}

_EXTENSION_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "application/pdf": ".pdf",
    "application/x-tgsticker": ".tgs",
    "text/plain": ".txt",
}


@dataclass(frozen=True)
class PromptFile:
    """One inbound attachment."""

    mime: str
    #: A ``data:<mime>;base64,...`` URL carrying the bytes inline. Empty when
    #: :attr:`error` is set, because there are no bytes to carry.
    url: str
    filename: str | None = None
    #: Absolute path once :func:`save_attachments` has written it, else None.
    path: str | None = None
    #: Why the attachment could not be fetched, if it could not be. Set instead
    #: of dropping the attachment, so the agent can say "that file was too large"
    #: rather than answering a question about a file it never saw.
    error: str | None = None

    @property
    def data(self) -> bytes:
        """The decoded bytes (empty when this attachment failed to download)."""
        if not self.url:
            return b""
        try:
            return base64.b64decode(self.url.split("base64,", 1)[-1])
        except (binascii.Error, ValueError):
            return b""


def to_data_url(data: bytes, mime: str) -> str:
    """Encode raw bytes as a ``data:`` URL for a file part's ``url``."""
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _sticker_mime(sticker: Any) -> str:
    """Stickers report no MIME type; the format is implied by two flags."""
    if getattr(sticker, "is_video", False):
        return "video/webm"
    if getattr(sticker, "is_animated", False):
        return "application/x-tgsticker"
    return "image/webp"


def _media(message: Any) -> tuple[str, str, str | None, str] | None:
    """``(file_id, mime, filename, kind)`` for the message's attachment.

    A Telegram message carries at most one downloadable attachment, with one
    catch: for a GIF, Telegram sets **both** ``animation`` and ``document`` for
    backwards compatibility. ``animation`` is therefore checked first, and
    ``document`` is only consulted if no richer field matched — otherwise every
    GIF would be downloaded twice and shown to the model twice.
    """
    photo = getattr(message, "photo", None)
    if photo:
        # Telegram sorts renditions ascending; the last is the highest resolution.
        return photo[-1].file_id, "image/jpeg", None, "photo"

    for kind in ("animation", "video", "audio", "voice", "video_note", "sticker", "document"):
        obj = getattr(message, kind, None)
        if obj is None:
            continue
        if kind == "sticker":
            mime = _sticker_mime(obj)
        else:
            mime = getattr(obj, "mime_type", None) or _ASSUMED_MIME.get(
                kind, "application/octet-stream"
            )
        return obj.file_id, mime, getattr(obj, "file_name", None), kind
    return None


def _default_name(kind: str, mime: str) -> str:
    """A filename for the kinds Telegram sends without one (photos, voice notes,
    video notes, stickers), so every attachment can be named on disk."""
    return f"{kind}{_EXTENSION_BY_MIME.get(mime, '')}"


async def collect_attachments(message: Any, bot: Any) -> list[PromptFile]:
    """Download the message's attachment, if it has one.

    Returns ``[]`` for a message with no downloadable media — including the
    non-file attachments Telegram also models as attachments (polls, contacts,
    locations, dice), which have nothing to fetch.

    A download that fails does **not** raise: the Bot API refuses ``getFile`` for
    anything over 20 MB, and losing the user's whole message (caption included)
    because they attached a 30 MB video is a worse outcome than telling the agent
    the file was unavailable.
    """
    found = _media(message)
    if found is None:
        return []
    file_id, mime, filename, kind = found
    name = filename or _default_name(kind, mime)

    try:
        handle = await bot.get_file(file_id)
        data = bytes(await handle.download_as_bytearray())
    except Exception as exc:
        logger.warning("could not download %s attachment %r: %s", kind, name, exc)
        return [PromptFile(mime=mime, url="", filename=name, error=_download_error(exc))]

    return [PromptFile(mime=mime, url=to_data_url(data, mime), filename=name)]


def _download_error(exc: Exception) -> str:
    """A reason the agent can relay, rather than a raw Telegram exception."""
    text = str(exc)
    if "too big" in text.lower():
        return "larger than the 20 MB limit the Telegram Bot API allows a bot to download"
    return f"download failed ({text})" if text else "download failed"


def safe_filename(name: str | None, mime: str = "") -> str:
    """A filename that cannot escape the inbox directory.

    Telegram filenames come from another client and are untrusted: ``basename``
    drops any directory part (including ``../``), the character class strips
    anything else exotic, and a name that reduces to nothing falls back to a
    generated one.
    """
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = _UNSAFE_IN_NAME.sub("_", base).strip("._-")
    if not base:
        base = f"attachment{_EXTENSION_BY_MIME.get(mime, '')}".strip("._-") or "attachment"
    if len(base) > _MAX_NAME_LEN:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: _MAX_NAME_LEN - len(ext) - 1] + dot + ext) if dot else base[:_MAX_NAME_LEN]
    return base


def _prune(inbox: Path) -> None:
    """Best-effort removal of inbox batches older than :data:`_RETENTION_DAYS`."""
    cutoff = time.time() - _RETENTION_DAYS * 86400
    try:
        batches = list(inbox.iterdir())
    except OSError:
        return
    for batch in batches:
        try:
            if batch.is_dir() and batch.stat().st_mtime < cutoff:
                shutil.rmtree(batch, ignore_errors=True)
        except OSError:
            continue


def save_attachments(files: list[PromptFile], directory: str | None) -> list[PromptFile]:
    """Write each attachment under ``directory`` and return copies carrying
    :attr:`PromptFile.path`.

    One batch directory per call keeps a turn's files together and makes pruning
    a directory-level operation. Files that failed to download are passed through
    untouched — there is nothing to write.

    Returns the input unchanged if ``directory`` is unset or unwritable: a
    workspace we cannot write to is not a reason to lose the turn, it just means
    the agent is limited to whatever can be inlined.
    """
    if not directory or not files:
        return files
    if all(f.error for f in files):
        return files

    root = Path(directory) / INBOX_DIRNAME
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    batch = root / f"{stamp}-{uuid.uuid4().hex[:6]}"
    try:
        batch.mkdir(parents=True, exist_ok=True)
        # Written every time so the inbox stays ignored even if the workspace was
        # re-cloned or the file removed.
        (root.parent / ".gitignore").write_text(_SELF_IGNORE)
    except OSError as exc:
        logger.warning("cannot write attachment inbox under %s: %s", directory, exc)
        return files
    _prune(root)

    saved: list[PromptFile] = []
    used: set[str] = set()
    for file in files:
        if file.error:
            saved.append(file)
            continue
        name = safe_filename(file.filename, file.mime)
        # Two attachments in one batch can sanitize to the same name.
        while name in used:
            stem, dot, ext = name.rpartition(".")
            name = f"{stem or name}_{uuid.uuid4().hex[:4]}{dot}{ext}"
        used.add(name)
        target = batch / name
        try:
            target.write_bytes(file.data)
        except OSError as exc:
            logger.warning("cannot save attachment %r: %s", name, exc)
            saved.append(file)
            continue
        saved.append(replace(file, path=str(target)))
    return saved
