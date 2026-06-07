import base64
from types import SimpleNamespace

from balam.attachments import collect_attachments, to_data_url


class _FakeFile:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self._data)


class _FakeBot:
    """Returns a fixed payload for every get_file; records requested file ids."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.requested: list[str] = []

    async def get_file(self, file_id: str) -> _FakeFile:
        self.requested.append(file_id)
        return _FakeFile(self._data)


def test_to_data_url_round_trips_bytes() -> None:
    url = to_data_url(b"hello", "text/plain")
    assert url.startswith("data:text/plain;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"hello"


async def test_collect_attachments_photo_uses_largest_rendition() -> None:
    bot = _FakeBot(b"\xff\xd8jpegbytes")
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
        document=None,
    )

    files = await collect_attachments(message, bot)

    assert len(files) == 1
    assert files[0].mime == "image/jpeg"
    assert files[0].filename is None
    assert base64.b64decode(files[0].url.split(",", 1)[1]) == b"\xff\xd8jpegbytes"
    # Telegram sorts photo sizes ascending — the last is the highest resolution.
    assert bot.requested == ["large"]


async def test_collect_attachments_document_keeps_mime_and_name() -> None:
    bot = _FakeBot(b"%PDF-1.7")
    message = SimpleNamespace(
        photo=[],
        document=SimpleNamespace(
            file_id="doc", mime_type="application/pdf", file_name="report.pdf"
        ),
    )

    files = await collect_attachments(message, bot)

    assert len(files) == 1
    assert files[0].mime == "application/pdf"
    assert files[0].filename == "report.pdf"
    assert base64.b64decode(files[0].url.split(",", 1)[1]) == b"%PDF-1.7"


async def test_collect_attachments_document_defaults_unknown_mime() -> None:
    bot = _FakeBot(b"data")
    message = SimpleNamespace(
        photo=[],
        document=SimpleNamespace(file_id="doc", mime_type=None, file_name="x"),
    )

    files = await collect_attachments(message, bot)

    assert files[0].mime == "application/octet-stream"


async def test_collect_attachments_text_only_returns_empty() -> None:
    bot = _FakeBot(b"")
    message = SimpleNamespace(photo=[], document=None)

    assert await collect_attachments(message, bot) == []
