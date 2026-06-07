"""Inbound Telegram attachments → OpenCode file parts (tier-1 plan §4).

The bot accepts images / PDFs / text files and forwards them to the agent as native
OpenCode *file parts* (``FilePartInput``: ``{type, mime, url}``) in the prompt body's
``parts`` array. The bytes travel inline as ``data:`` URLs, so there are no temp
files to manage and nothing for the agent's Read tool (and its permission gate) to
touch — OpenCode hands the file straight to the model as a media/content block.

Verified against the running OpenCode (v1.15.13): the prompt ``parts`` array accepts
``FilePartInput`` and its ``url`` may be a ``data:`` URL. This is how OpenCode's own
web app sends image attachments. The path-in-text alternative (used by the
open-shrimp reference, which downloads with the same ``get_file`` →
``download_as_bytearray`` calls but writes to a temp dir and names the path in the
prompt) works on v1.15.13 but breaks on a future OpenCode that restricts the Read
tool to in-workspace paths — file parts sidestep that entirely.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PromptFile:
    """One attachment as an OpenCode file part (``FilePartInput``)."""

    mime: str
    #: A ``data:<mime>;base64,...`` URL carrying the bytes inline.
    url: str
    filename: str | None = None


def to_data_url(data: bytes, mime: str) -> str:
    """Encode raw bytes as a ``data:`` URL for a file part's ``url``."""
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def collect_attachments(message: Any, bot: Any) -> list[PromptFile]:
    """Download a message's photo/document attachments as file parts.

    Photos use Telegram's largest rendition (``message.photo[-1]``); documents carry
    their own MIME type and filename. Returns ``[]`` for a text-only message.
    """
    files: list[PromptFile] = []

    if message.photo:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        data = bytes(await file.download_as_bytearray())
        files.append(PromptFile(mime="image/jpeg", url=to_data_url(data, "image/jpeg")))

    document = getattr(message, "document", None)
    if document is not None:
        file = await bot.get_file(document.file_id)
        data = bytes(await file.download_as_bytearray())
        mime = document.mime_type or "application/octet-stream"
        files.append(
            PromptFile(mime=mime, url=to_data_url(data, mime), filename=document.file_name)
        )

    return files
