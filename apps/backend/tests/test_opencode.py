from types import SimpleNamespace
from typing import Any

from balam.attachments import PromptFile
from balam.opencode import OpenCode


class _FakePostClient:
    """Captures the body posted to prompt_async; no-op raise_for_status."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, params: Any = None, json: Any = None) -> Any:
        self.calls.append({"url": url, "params": params, "json": json})
        return SimpleNamespace(raise_for_status=lambda: None)


def _client() -> tuple[OpenCode, _FakePostClient]:
    oc = OpenCode(base_url="http://x", username="u", password=None)
    fake = _FakePostClient()
    oc._client = fake  # type: ignore[assignment]
    return oc, fake


async def test_prompt_appends_file_parts_after_text() -> None:
    oc, fake = _client()
    files = [
        PromptFile(mime="image/jpeg", url="data:image/jpeg;base64,AAAA"),
        PromptFile(
            mime="application/pdf", url="data:application/pdf;base64,BBBB", filename="r.pdf"
        ),
    ]

    await oc.prompt("ses_1", "look at these", directory="/work", files=files)

    parts = fake.calls[0]["json"]["parts"]
    assert parts[0] == {"type": "text", "text": "look at these"}
    assert parts[1] == {"type": "file", "mime": "image/jpeg", "url": "data:image/jpeg;base64,AAAA"}
    assert parts[2] == {
        "type": "file",
        "mime": "application/pdf",
        "url": "data:application/pdf;base64,BBBB",
        "filename": "r.pdf",
    }


async def test_prompt_omits_empty_text_part_for_attachment_only() -> None:
    oc, fake = _client()

    await oc.prompt(
        "ses_1",
        "",
        directory="/work",
        files=[PromptFile(mime="image/jpeg", url="data:image/jpeg;base64,AAAA")],
    )

    parts = fake.calls[0]["json"]["parts"]
    assert parts == [{"type": "file", "mime": "image/jpeg", "url": "data:image/jpeg;base64,AAAA"}]


async def test_prompt_text_only_unchanged() -> None:
    oc, fake = _client()

    await oc.prompt("ses_1", "hello", directory="/work")

    assert fake.calls[0]["json"]["parts"] == [{"type": "text", "text": "hello"}]
