"""Batch speech-to-text behind one provider-agnostic client.

Pipelines call `Transcriber.transcribe`, which adds an on-disk cache (re-running a
pipeline or an eval costs no audio quota) and retries with backoff on top of a
provider. Groq's hosted Whisper is the default; another provider only needs to
implement `SpeechProvider`.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import httpx2

from app.config import settings

logger = logging.getLogger(__name__)

# Bump to invalidate cached transcriptions, e.g. after changing request parameters.
CACHE_VERSION = 1


class STTError(Exception):
    pass


class TransientSTTError(STTError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass(frozen=True)
class Transcription:
    provider: str
    model: str
    duration: float
    text: str
    segments: list[Segment]
    words: list[Word] = field(default_factory=list)
    latency_ms: float = 0.0
    cached: bool = False

    def shifted(self, seconds: float) -> "Transcription":
        """The same transcription with every timestamp moved by `seconds`."""
        return replace(
            self,
            segments=[
                replace(s, start=s.start + seconds, end=s.end + seconds) for s in self.segments
            ],
            words=[replace(w, start=w.start + seconds, end=w.end + seconds) for w in self.words],
        )


@dataclass(frozen=True)
class SpeechRequest:
    model: str
    language: str = "en"
    # Optional context that steers spelling (names, tickers); Whisper reads ~224 tokens.
    prompt: str | None = None


class SpeechProvider(Protocol):
    name: str
    # Largest upload the provider accepts, in bytes.
    max_upload: int

    async def transcribe(
        self, audio: bytes, filename: str, request: SpeechRequest
    ) -> Transcription: ...

    async def aclose(self) -> None: ...


class GroqSpeech:
    """Whisper on Groq's OpenAI-compatible transcription endpoint."""

    name = "groq"
    base_url = "https://api.groq.com/openai/v1"
    max_upload = 25 * 1024 * 1024  # free tier

    def __init__(
        self, api_key: str, *, http: httpx2.AsyncClient | None = None, timeout: float = 600.0
    ) -> None:
        if not api_key:
            raise STTError("GROQ_API_KEY is not set")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http or httpx2.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def transcribe(
        self, audio: bytes, filename: str, request: SpeechRequest
    ) -> Transcription:
        form: dict[str, Any] = {
            "model": request.model,
            "response_format": "verbose_json",
            "language": request.language,
            "temperature": "0",
            "timestamp_granularities[]": ["word", "segment"],
        }
        if request.prompt:
            form["prompt"] = request.prompt
        try:
            response = await self._http.post(
                "/audio/transcriptions",
                headers=self._headers,
                files={"file": (filename, audio, "audio/mpeg")},
                data=form,
            )
        except (httpx2.TimeoutException, httpx2.TransportError) as e:
            raise TransientSTTError(f"groq transcription failed: {e!r}") from e

        status = response.status_code
        if status == 429 or status >= 500 or status in (408, 409):
            raise TransientSTTError(
                f"groq {status}: {_error_message(response)}", retry_after=_retry_after(response)
            )
        if status >= 400:
            raise STTError(f"groq {status}: {_error_message(response)}")

        data = response.json()
        return Transcription(
            provider=self.name,
            model=request.model,
            duration=float(data.get("duration") or 0.0),
            text=(data.get("text") or "").strip(),
            segments=[
                Segment(
                    start=float(s["start"]),
                    end=float(s["end"]),
                    text=s["text"].strip(),
                    avg_logprob=s.get("avg_logprob"),
                    no_speech_prob=s.get("no_speech_prob"),
                )
                for s in data.get("segments") or []
                if s["text"].strip()
            ],
            words=[
                Word(w["word"].strip(), float(w["start"]), float(w["end"]))
                for w in data.get("words") or []
            ],
        )

    async def aclose(self) -> None:
        await self._http.aclose()


class Transcriber:
    """The one entry point pipelines use for batch speech-to-text."""

    def __init__(
        self,
        provider: SpeechProvider,
        *,
        cache_dir: Path | None = None,
        max_attempts: int = 4,
        max_retry_wait: float = 3600.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self._cache_dir = cache_dir
        self._max_attempts = max_attempts
        self._max_retry_wait = max_retry_wait
        self._sleep = sleep

    async def transcribe(
        self, audio: bytes, request: SpeechRequest, *, filename: str = "audio.mp3"
    ) -> Transcription:
        if len(audio) > self.provider.max_upload:
            raise STTError(
                f"{len(audio)} bytes is over {self.provider.name}'s "
                f"{self.provider.max_upload}-byte upload limit; split the audio first"
            )
        key = _cache_key(self.provider.name, audio, request)
        if (hit := self._cache_get(key)) is not None:
            logger.info("stt cache hit model=%s duration=%.0fs", hit.model, hit.duration)
            return hit

        attempt = 0
        while True:
            attempt += 1
            start = time.perf_counter()
            try:
                result = await self.provider.transcribe(audio, filename, request)
                break
            except TransientSTTError as e:
                delay = self._retry_delay(e, attempt)
                if delay is None:
                    raise
                logger.warning(
                    "stt retry model=%s attempt=%d delay=%.0fs: %s",
                    request.model,
                    attempt,
                    delay,
                    e,
                )
                await self._sleep(delay)
        result = replace(result, latency_ms=(time.perf_counter() - start) * 1000)
        logger.info(
            "stt call model=%s duration=%.0fs latency_ms=%.0f segments=%d",
            result.model,
            result.duration,
            result.latency_ms,
            len(result.segments),
        )
        self._cache_put(key, request, result)
        return result

    def _retry_delay(self, error: TransientSTTError, attempt: int) -> float | None:
        if attempt >= self._max_attempts:
            return None
        if error.retry_after is not None:
            # Audio-seconds-per-hour limits can ask for a long wait; a batch job can afford it.
            return error.retry_after + 1 if error.retry_after <= self._max_retry_wait else None
        return min(2.0 ** (attempt - 1), 30.0) + random.uniform(0, 0.5)

    def _cache_get(self, key: str) -> Transcription | None:
        if self._cache_dir is None:
            return None
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))["transcription"]
            segments = [Segment(**s) for s in data.pop("segments")]
            words = [Word(**w) for w in data.pop("words")]
            return Transcription(**data, segments=segments, words=words, cached=True)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _cache_put(self, key: str, request: SpeechRequest, result: Transcription) -> None:
        if self._cache_dir is None:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = asdict(result)
        del stored["cached"]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"request": asdict(request), "transcription": stored}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _path(self, key: str) -> Path:
        assert self._cache_dir is not None
        return self._cache_dir / key[:2] / f"{key}.json"

    async def aclose(self) -> None:
        await self.provider.aclose()


def _cache_key(provider: str, audio: bytes, request: SpeechRequest) -> str:
    payload = {
        "v": CACHE_VERSION,
        "provider": provider,
        "audio": hashlib.sha256(audio).hexdigest(),
        **asdict(request),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _retry_after(response: httpx2.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _error_message(response: httpx2.Response) -> str:
    try:
        error = response.json().get("error")
    except ValueError:
        return response.text[:300]
    if isinstance(error, dict) and error.get("message"):
        return error["message"]
    return response.text[:300]


def get_transcriber() -> Transcriber:
    """Transcriber configured from settings (.env)."""
    if settings.stt_provider != "groq":
        raise STTError(f"unsupported STT_PROVIDER {settings.stt_provider!r}")
    return Transcriber(GroqSpeech(settings.groq_api_key), cache_dir=Path(settings.stt_cache_dir))
