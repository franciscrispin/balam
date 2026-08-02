import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from balam.attachments import (
    INBOX_DIRNAME,
    PromptFile,
    collect_attachments,
    safe_filename,
    save_attachments,
    to_data_url,
)


class _FakeFile:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self._data)


class _FakeBot:
    """Returns a fixed payload for every get_file; records requested file ids."""

    def __init__(self, data: bytes = b"payload", error: Exception | None = None) -> None:
        self._data = data
        self._error = error
        self.requested: list[str] = []

    async def get_file(self, file_id: str) -> _FakeFile:
        self.requested.append(file_id)
        if self._error is not None:
            raise self._error
        return _FakeFile(self._data)


def _message(**media) -> SimpleNamespace:
    """A message with exactly the media fields named, all others absent."""
    fields = dict.fromkeys(
        ("photo", "document", "video", "audio", "voice", "video_note", "animation", "sticker")
    )
    fields.update(media)
    return SimpleNamespace(**fields)


def test_to_data_url_round_trips_bytes() -> None:
    url = to_data_url(b"hello", "text/plain")
    assert url.startswith("data:text/plain;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"hello"


def test_prompt_file_data_decodes_its_own_url() -> None:
    assert PromptFile(mime="text/csv", url=to_data_url(b"a,b\n", "text/csv")).data == b"a,b\n"
    # A failed download carries no bytes and must not raise on access.
    assert PromptFile(mime="video/mp4", url="", error="too big").data == b""


async def test_collect_attachments_photo_uses_largest_rendition() -> None:
    bot = _FakeBot(b"\xff\xd8jpegbytes")
    message = _message(photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")])

    files = await collect_attachments(message, bot)

    assert len(files) == 1
    assert files[0].mime == "image/jpeg"
    # Photos arrive unnamed; a name is synthesized so the file can be saved.
    assert files[0].filename == "photo.jpg"
    assert files[0].data == b"\xff\xd8jpegbytes"
    # Telegram sorts photo sizes ascending — the last is the highest resolution.
    assert bot.requested == ["large"]


async def test_collect_attachments_document_keeps_mime_and_name() -> None:
    bot = _FakeBot(b"%PDF-1.7")
    message = _message(
        document=SimpleNamespace(file_id="doc", mime_type="application/pdf", file_name="report.pdf")
    )

    files = await collect_attachments(message, bot)

    assert files[0].mime == "application/pdf"
    assert files[0].filename == "report.pdf"
    assert files[0].data == b"%PDF-1.7"


@pytest.mark.parametrize(
    ("kind", "obj", "mime", "name"),
    [
        (
            "voice",
            SimpleNamespace(file_id="v", mime_type="audio/ogg"),
            "audio/ogg",
            "voice.ogg",
        ),
        (
            "audio",
            SimpleNamespace(file_id="a", mime_type="audio/mpeg", file_name="song.mp3"),
            "audio/mpeg",
            "song.mp3",
        ),
        (
            "video",
            SimpleNamespace(file_id="vd", mime_type="video/mp4", file_name="clip.mp4"),
            "video/mp4",
            "clip.mp4",
        ),
        # VideoNote reports neither a MIME type nor a filename.
        ("video_note", SimpleNamespace(file_id="vn"), "video/mp4", "video_note.mp4"),
        (
            "sticker",
            SimpleNamespace(file_id="s", is_video=False, is_animated=False),
            "image/webp",
            "sticker.webp",
        ),
        (
            "sticker",
            SimpleNamespace(file_id="s", is_video=True, is_animated=False),
            "video/webm",
            "sticker.webm",
        ),
        (
            "sticker",
            SimpleNamespace(file_id="s", is_video=False, is_animated=True),
            "application/x-tgsticker",
            "sticker.tgs",
        ),
    ],
)
async def test_collect_attachments_covers_every_media_kind(kind, obj, mime, name) -> None:
    # Before this, only photo and document were collected: a voice note or a video
    # sent to the bot was downloaded by nobody and silently vanished.
    files = await collect_attachments(_message(**{kind: obj}), _FakeBot())

    assert len(files) == 1
    assert (files[0].mime, files[0].filename) == (mime, name)


async def test_collect_attachments_downloads_a_gif_once() -> None:
    # Telegram sets `document` alongside `animation` for backwards compatibility;
    # taking both would show the model the same GIF twice.
    bot = _FakeBot()
    message = _message(
        animation=SimpleNamespace(file_id="anim", mime_type="video/mp4", file_name="cat.gif"),
        document=SimpleNamespace(file_id="anim-dup", mime_type="video/mp4", file_name="cat.gif"),
    )

    files = await collect_attachments(message, bot)

    assert len(files) == 1
    assert bot.requested == ["anim"]


async def test_collect_attachments_reports_a_failed_download() -> None:
    # The Bot API refuses getFile above 20 MB. Dropping the whole message would
    # lose the user's caption too, so the failure is carried, not raised.
    bot = _FakeBot(error=RuntimeError("File is too big"))
    message = _message(
        video=SimpleNamespace(file_id="v", mime_type="video/mp4", file_name="movie.mp4")
    )

    files = await collect_attachments(message, bot)

    assert len(files) == 1
    assert files[0].url == ""
    assert "20 MB" in files[0].error


async def test_collect_attachments_ignores_non_file_attachments() -> None:
    # filters.ATTACHMENT also admits polls, contacts, dice and locations; they
    # have no file to fetch and must not blow up the turn.
    assert await collect_attachments(_message(), _FakeBot()) == []


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("../../../etc/passwd", "passwd"),
        ("/absolute/path.csv", "path.csv"),
        ("..", "attachment"),
        ("", "attachment"),
        (None, "attachment"),
        ("nul\x00byte.txt", "nul_byte.txt"),
        ("réservé.csv", "r_serv_.csv"),
        ("a" * 200 + ".csv", "a" * 92 + ".csv"),
    ],
)
def test_safe_filename_cannot_escape_the_inbox(given, expected) -> None:
    # Filenames come from another user's Telegram client and are untrusted.
    out = safe_filename(given)
    assert out == expected
    assert "/" not in out and not out.startswith(".")


def test_save_attachments_writes_into_the_workspace(tmp_path: Path) -> None:
    files = [
        PromptFile(mime="text/csv", url=to_data_url(b"a,b\n", "text/csv"), filename="s.csv"),
        PromptFile(mime="video/mp4", url=to_data_url(b"\x00mp4", "video/mp4"), filename="c.mp4"),
    ]

    saved = save_attachments(files, str(tmp_path))

    assert all(f.path is not None for f in saved)
    for f, expected in zip(saved, (b"a,b\n", b"\x00mp4"), strict=True):
        assert Path(f.path).read_bytes() == expected
        # Inside the workspace, so reads stay within the ADR-0012 boundary.
        assert Path(f.path).is_relative_to(tmp_path / INBOX_DIRNAME)


def test_save_attachments_hides_the_inbox_from_git(tmp_path: Path) -> None:
    save_attachments(
        [PromptFile(mime="text/csv", url=to_data_url(b"x", "text/csv"), filename="s.csv")],
        str(tmp_path),
    )
    # A self-matching ignore keeps attachments out of `git status` and /diff
    # without editing the workspace's own .gitignore.
    assert (tmp_path / ".balam" / ".gitignore").read_text() == "*\n"


def test_save_attachments_deduplicates_colliding_names(tmp_path: Path) -> None:
    # Two different names can sanitize to the same thing; neither may be lost.
    files = [
        PromptFile(mime="text/csv", url=to_data_url(b"first", "text/csv"), filename="a b.csv"),
        PromptFile(mime="text/csv", url=to_data_url(b"second", "text/csv"), filename="a/b.csv"),
    ]

    saved = save_attachments(files, str(tmp_path))

    assert saved[0].path != saved[1].path
    assert Path(saved[0].path).read_bytes() == b"first"
    assert Path(saved[1].path).read_bytes() == b"second"


def test_save_attachments_passes_through_failures_and_missing_directory(tmp_path: Path) -> None:
    broken = PromptFile(mime="video/mp4", url="", filename="big.mp4", error="too big")
    assert save_attachments([broken], str(tmp_path)) == [broken]
    assert not (tmp_path / ".balam").exists()

    ok = PromptFile(mime="text/csv", url=to_data_url(b"x", "text/csv"), filename="s.csv")
    # No workspace configured: nothing to write to, but the turn still runs.
    assert save_attachments([ok], None) == [ok]


def test_save_attachments_survives_an_unwritable_workspace(tmp_path: Path) -> None:
    # A read-only workspace limits the agent to what can be inlined; it must not
    # cost the user their turn.
    blocked = tmp_path / "ro"
    blocked.mkdir(mode=0o500)
    files = [PromptFile(mime="text/csv", url=to_data_url(b"x", "text/csv"), filename="s.csv")]

    assert save_attachments(files, str(blocked)) == files
