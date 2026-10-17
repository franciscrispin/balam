"""Voice notes → text, transcribed locally with faster-whisper.

A voice note (or a round video note) is turned into text *before* the turn
starts, and that text becomes the prompt. Everything downstream — the router,
the streamer, permissions, both agent backends — then sees an ordinary typed
message, so none of it needs to know voice exists. This is the approach
OpenShrimp takes; Balam swaps its Moonshine binary for faster-whisper, which is
multilingual and installs as a plain Python dependency.

The model runs in-process on the CPU (int8), with no network call after the
weights are fetched. It is loaded on the first voice note, not at boot: the
weights are downloaded from Hugging Face on first use, and a bot that never
receives a voice note should not pay the RAM for a resident model. Once loaded
it stays loaded, so only the first note is slow.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from telegram.constants import ChatAction

from balam.attachments import PromptFile
from balam.telegram_utils import thread_kwargs

logger = logging.getLogger(__name__)

#: Telegram's message length limit, so a long note's echo is cut, not refused.
_MAX_ECHO_CHARS = 4096

#: Telegram kinds that carry speech to transcribe. Plain ``audio`` (a music or
#: recording *file*) is deliberately absent: it is something to work on, not a
#: way of talking, so it stays an attachment the agent can open.
VOICE_KINDS = ("voice", "video_note")


def voice_kind(message: Any) -> str | None:
    """The message's voice-like attachment kind, or None if it has none."""
    for kind in VOICE_KINDS:
        if getattr(message, kind, None) is not None:
            return kind
    return None


class Transcriber:
    """A lazily loaded faster-whisper model shared by every topic.

    Transcriptions run one at a time: the model is CPU-bound and already uses
    several threads, so two in parallel would each just run at half speed.
    """

    def __init__(self, model: str, *, language: str | None = None) -> None:
        self.model_name = model
        self.language = language
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def transcribe(self, audio: bytes) -> str:
        """The text spoken in ``audio`` (any format PyAV can decode)."""
        async with self._lock:
            return await asyncio.to_thread(self._transcribe_sync, audio)

    def _load(self) -> Any:
        if self._model is None:
            # Imported here so a test run, or a bot that never hears a voice
            # note, does not pay for importing CTranslate2.
            from faster_whisper import WhisperModel

            logger.info("loading whisper model %r (first voice note)", self.model_name)
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        return self._model

    def _transcribe_sync(self, audio: bytes) -> str:
        model = self._load()
        # The VAD filter drops silence before decoding, which is both faster and
        # stops Whisper from inventing words in the quiet at the end of a note.
        segments, info = model.transcribe(
            io.BytesIO(audio), language=self.language, vad_filter=True
        )
        # ``segments`` is a lazy generator: the decoding happens while iterating,
        # so it must be consumed here, inside the worker thread.
        text = " ".join(segment.text.strip() for segment in segments).strip()
        logger.info(
            "transcribed %.1fs of %s audio (%d chars)",
            info.duration,
            info.language,
            len(text),
        )
        return text


async def transcribe_voice_note(
    message: Any,
    files: list[PromptFile],
    transcriber: Transcriber | None,
    bot: Any,
    thread_id: int | None,
) -> str | None:
    """Transcribe ``message`` if it is a voice note, echoing what was heard.

    ``files`` is what :func:`balam.attachments.collect_attachments` fetched for
    the message. Returns the transcript (``""`` when the note held no speech),
    or ``None`` when the audio should travel on as a plain attachment instead:
    the message is not a voice note, transcription is off, the download failed,
    or the model did. A failed transcription never loses the message — the agent
    still gets the recording, and can say it cannot listen to it.

    The transcript is echoed back as a reply to the note, because a
    mis-hearing is otherwise invisible until the agent answers the wrong
    question.
    """
    if transcriber is None or voice_kind(message) is None:
        return None
    if not files or files[0].error:
        return None

    try:
        await bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.TYPING, **thread_kwargs(thread_id)
        )
    except Exception:  # cosmetic; never worth losing the note over
        logger.debug("typing indicator failed before transcription", exc_info=True)

    try:
        text = await transcriber.transcribe(files[0].data)
    except Exception:
        logger.exception("voice transcription failed")
        await message.reply_text(
            "⚠️ Couldn't transcribe this voice note, so the agent gets the audio file instead."
        )
        return None

    echo = f"🎙️ {text}" if text else "🎙️ No speech heard in this voice note."
    await message.reply_text(echo[:_MAX_ECHO_CHARS])
    return text
