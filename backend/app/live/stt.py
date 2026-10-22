"""Streaming speech-to-text behind one provider-agnostic client.

A provider turns a stream of audio chunks into transcript updates while the audio is
still coming in: interim guesses that may change, then a final version of each stretch
of audio. Deepgram's streaming API is the default; another provider only needs to
implement `StreamingProvider`.

`LiveTranscriber` stamps every update with when it arrived and how much audio had been
sent by then, which is what latency is measured from, and records each run to disk.
Re-running the same audio with the same settings replays the recording, so development
and the WER eval cost no STT credit. Latency runs turn the cache off.
"""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterable, AsyncIterator, Callable
from contextlib import aclosing, suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from app.calls.stt import STTError, TransientSTTError
from app.config import settings

logger = logging.getLogger(__name__)

# Bump to invalidate recorded streams, e.g. after changing how requests are built.
CACHE_VERSION = 1


@dataclass(frozen=True)
class LiveWord:
    # As displayed, with punctuation and casing ("Thanks,").
    text: str
    start: float
    end: float
    # Diarization label, numbered from 0 per stream; None when diarization is off.
    speaker: int | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class Update:
    """One message from the provider about a stretch of audio, in seconds from the start
    of the recording.

    An interim update is a guess the provider may still revise; a final one is settled,
    and finals never overlap, so joining them in order gives the transcript. An
    utterance end marks a pause after the last word (its start and end are when that
    word ended) and carries no words.
    """

    kind: Literal["interim", "final", "utterance_end"]
    start: float
    end: float
    words: list[LiveWord] = field(default_factory=list)
    # The provider heard a pause long enough to end the utterance here.
    speech_final: bool = False
    # Filled in by LiveTranscriber: wall-clock seconds from the first audio sent until
    # this update arrived, and how far into the recording the audio sent by then reached.
    received: float = 0.0
    audio_sent: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def shifted(self, seconds: float) -> "Update":
        return replace(
            self,
            start=self.start + seconds,
            end=self.end + seconds,
            words=[replace(w, start=w.start + seconds, end=w.end + seconds) for w in self.words],
        )


@dataclass(frozen=True)
class StreamRequest:
    model: str
    language: str = "en"
    diarize: bool = True
    # Numbers, amounts and dates as written ("$32.7 million", "11/04/2020") rather than
    # as spoken ("thirty two point seven million dollars").
    smart_format: bool = True
    # Silence that makes the provider finalize what it has heard so far.
    endpointing_ms: int = 300
    # Silence that counts as the end of an utterance (the speaker paused or finished).
    utterance_end_ms: int = 1000


class AudioSource(Protocol):
    """Audio arriving in real time: a replayed file now, a live feed later."""

    # Where in the recording the first chunk starts, in seconds.
    offset: float
    # Seconds of audio sent so far, counted from `offset`.
    sent: float

    def chunks(self) -> AsyncIterator[bytes]: ...

    def elapsed(self) -> float:
        """Wall-clock seconds since the first chunk was sent."""
        ...

    def describe(self) -> dict[str, Any]:
        """Everything about the source that can change the transcript or its timing."""
        ...


class StreamingProvider(Protocol):
    name: str

    def stream(self, audio: AsyncIterable[bytes], request: StreamRequest) -> AsyncIterator[Update]:
        """Send the audio as it comes and yield updates as they arrive, with times in
        seconds from the start of the stream. Ends once the provider has flushed
        everything it heard."""
        ...


class DeepgramStreaming:
    """Deepgram's live transcription WebSocket.

    MP3 is a container format, so no encoding or sample rate is sent: Deepgram reads
    them from the frames. Interim results are on because utterance ends need them.
    """

    name = "deepgram"
    url = "wss://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, *, connect: Callable[..., Any] = ws_connect) -> None:
        if not api_key:
            raise STTError("DEEPGRAM_API_KEY is not set")
        self._headers = {"Authorization": f"Token {api_key}"}
        self._connect = connect

    def params(self, request: StreamRequest) -> dict[str, str]:
        params = {
            "model": request.model,
            "language": request.language,
            "punctuate": "true",
            "smart_format": str(request.smart_format).lower(),
            "interim_results": "true",
            "endpointing": str(request.endpointing_ms),
            "utterance_end_ms": str(request.utterance_end_ms),
        }
        if request.diarize:
            params["diarize_model"] = "latest"
        return params

    async def stream(
        self, audio: AsyncIterable[bytes], request: StreamRequest
    ) -> AsyncIterator[Update]:
        url = f"{self.url}?{urlencode(self.params(request))}"
        # An error reading the audio, kept apart from the connection's own errors.
        audio_error: list[Exception] = []
        try:
            async with self._connect(url, additional_headers=self._headers) as ws:
                sender = asyncio.create_task(_send(ws, audio, audio_error))
                try:
                    # Ends when the socket closes normally: everything was flushed, or
                    # the audio failed and the sender closed it.
                    async for message in ws:
                        if isinstance(message, str) and (update := parse(json.loads(message))):
                            yield update
                finally:
                    if not sender.done():
                        sender.cancel()
                        with suppress(asyncio.CancelledError):
                            await sender
        except InvalidStatus as e:
            status = e.response.status_code
            body = (e.response.body or b"")[:300].decode("utf-8", "replace")
            reason = e.response.headers.get("dg-error") or body
            error = TransientSTTError if status == 429 or status >= 500 else STTError
            raise error(f"deepgram refused the stream ({status}): {reason}") from e
        except ConnectionClosedError as e:
            frame = e.rcvd or e.sent
            detail = f"{frame.code} {frame.reason}" if frame else "no close frame"
            raise TransientSTTError(f"deepgram stream dropped: {detail}") from e
        except (OSError, TimeoutError) as e:
            raise TransientSTTError(f"deepgram connection failed: {e!r}") from e
        if audio_error:
            raise audio_error[0]


async def _send(ws: Any, audio: AsyncIterable[bytes], error: list[Exception]) -> None:
    try:
        async for chunk in audio:
            await ws.send(chunk)
        # Ask Deepgram to finalize what it has and close the socket once it has.
        await ws.send(json.dumps({"type": "CloseStream"}))
    except Exception as e:
        error.append(e)
        await ws.close()


def parse(message: dict[str, Any]) -> Update | None:
    """An update from a Deepgram message, or None for messages that carry none
    (metadata, speech started, empty results over silence)."""
    kind = message.get("type")
    if kind == "Results":
        alternative = message["channel"]["alternatives"][0]
        words = [
            LiveWord(
                text=w.get("punctuated_word") or w["word"],
                start=float(w["start"]),
                end=float(w["end"]),
                speaker=w.get("speaker"),
                confidence=w.get("confidence"),
            )
            for w in alternative.get("words") or []
        ]
        if not words:
            return None
        start = float(message["start"])
        return Update(
            kind="final" if message.get("is_final") else "interim",
            start=start,
            end=start + float(message["duration"]),
            words=words,
            speech_final=bool(message.get("speech_final")),
        )
    if kind == "UtteranceEnd":
        at = float(message["last_word_end"])
        return Update(kind="utterance_end", start=at, end=at, speech_final=True)
    if kind == "Error":
        # Deepgram reports problems it can recover from this way; fatal ones close the socket.
        logger.warning("deepgram error: %s", message.get("description") or message)
    elif kind == "Metadata":
        logger.info(
            "deepgram stream done request_id=%s duration=%.0fs",
            message.get("request_id"),
            float(message.get("duration") or 0.0),
        )
    return None


class LiveTranscriber:
    """The one entry point pipelines use for streaming speech-to-text."""

    def __init__(self, provider: StreamingProvider, *, cache_dir: Path | None = None) -> None:
        self.provider = provider
        self._cache_dir = cache_dir

    async def stream(
        self, source: AudioSource, request: StreamRequest, *, use_cache: bool = True
    ) -> AsyncIterator[Update]:
        """Updates with times on the recording's clock, as they arrive. A run that ends
        early (an error, or the caller stopping) is not recorded."""
        key = _cache_key(self.provider.name, request, source.describe())
        if use_cache and (hit := self._cache_get(key)) is not None:
            logger.info("live stt cache hit model=%s updates=%d", request.model, len(hit))
            for update in hit:
                yield update
            return

        # Read the provider in its own task, so each update is stamped when it arrives
        # even if the caller takes its time with the previous one.
        queue: asyncio.Queue[Update | None] = asyncio.Queue()
        recorded: list[Update] = []

        async def pump() -> None:
            async with aclosing(self.provider.stream(source.chunks(), request)) as updates:
                async for update in updates:
                    update = replace(
                        update.shifted(source.offset),
                        received=source.elapsed(),
                        audio_sent=source.offset + source.sent,
                    )
                    recorded.append(update)
                    queue.put_nowait(update)

        task = asyncio.create_task(pump())
        task.add_done_callback(lambda _: queue.put_nowait(None))
        try:
            while (update := await queue.get()) is not None:
                yield update
            await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._cache_put(key, request, source.describe(), recorded)

    def _cache_get(self, key: str) -> list[Update] | None:
        if self._cache_dir is None:
            return None
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))
            return [
                Update(**{**u, "words": [LiveWord(**w) for w in u["words"]]})
                for u in data["updates"]
            ]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _cache_put(
        self, key: str, request: StreamRequest, source: dict[str, Any], updates: list[Update]
    ) -> None:
        if self._cache_dir is None:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        record = {
            "provider": self.provider.name,
            "request": asdict(request),
            "source": source,
            "updates": [asdict(u) for u in updates],
        }
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _path(self, key: str) -> Path:
        assert self._cache_dir is not None
        return self._cache_dir / key[:2] / f"{key}.json"


def _cache_key(provider: str, request: StreamRequest, source: dict[str, Any]) -> str:
    payload = {"v": CACHE_VERSION, "provider": provider, **asdict(request), "source": source}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def get_live_transcriber() -> LiveTranscriber:
    """LiveTranscriber configured from settings (.env)."""
    if settings.live_stt_provider != "deepgram":
        raise STTError(f"unsupported LIVE_STT_PROVIDER {settings.live_stt_provider!r}")
    return LiveTranscriber(
        DeepgramStreaming(settings.deepgram_api_key), cache_dir=Path(settings.live_stt_cache_dir)
    )
