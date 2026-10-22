"""Play a recorded call as if it were live.

The MP3 is cut between frames (see app.calls.audio), so every chunk is valid audio and
its place in the recording is known exactly. Each chunk is sent once the last of its
audio would have been spoken, so at 1x a word reaches the provider no sooner than it
would from a live feed. Faster speeds only help short stretches: Deepgram works through
a stream at about 1.25x real time, so audio sent faster queues up, and on a long run the
queue stalls the connection's keepalive pings until it drops.
"""

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.calls import audio


class Replay:
    def __init__(
        self,
        data: bytes,
        *,
        speed: float = 1.0,
        start: float = 0.0,
        duration: float | None = None,
        chunk_seconds: float = 0.1,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Plays `duration` seconds (default: the rest) from the first frame at or after
        `start`, `speed` times faster than real time, in chunks of about `chunk_seconds`."""
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.speed = speed
        self.chunk_seconds = chunk_seconds
        self._clock = clock
        self._sleep = sleep

        frame_start = 0.0
        selected: list[audio.Frame] = []
        self.offset = 0.0
        for f in audio.frames(data):
            if frame_start >= start - 1e-6 and (
                duration is None or frame_start < start + duration - 1e-6
            ):
                if not selected:
                    self.offset = frame_start
                selected.append(f)
            frame_start += f.samples / f.sample_rate
        if not selected:
            raise audio.AudioError(f"no audio after {start:.1f}s")

        # (bytes, end) per chunk, `end` in seconds from `offset`.
        self._chunks: list[tuple[bytes, float]] = []
        body, length, end = bytearray(), 0.0, 0.0
        for f in selected:
            body += data[f.offset : f.offset + f.length]
            length += f.samples / f.sample_rate
            end += f.samples / f.sample_rate
            if length >= chunk_seconds - 1e-9:
                self._chunks.append((bytes(body), end))
                body, length = bytearray(), 0.0
        if body:
            self._chunks.append((bytes(body), end))
        self.duration = end
        self._sha256 = hashlib.sha256(b"".join(c for c, _ in self._chunks)).hexdigest()

        self.sent = 0.0
        self._started: float | None = None

    async def chunks(self) -> AsyncIterator[bytes]:
        self._started = self._clock()
        for body, end in self._chunks:
            wait = self._started + end / self.speed - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self.sent = end
            yield body

    def elapsed(self) -> float:
        return 0.0 if self._started is None else self._clock() - self._started

    def describe(self) -> dict[str, Any]:
        return {
            "audio_sha256": self._sha256,
            "offset": round(self.offset, 6),
            "duration": round(self.duration, 6),
            "speed": self.speed,
            "chunk_seconds": self.chunk_seconds,
        }
