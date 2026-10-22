"""Live sessions: a call replayed into streaming speech-to-text, with every event fanned
out to the browser tabs watching it.

No Redis: the API process runs the sessions and gives each tab its own queue. A tab that
falls too far behind is dropped instead of letting its queue grow; it can reconnect and
catch up from the session's history.

A session replays a recorded stream when there is one, so it costs no STT credit, and
plays it back on the timing it was recorded with. A fresh session streams to the
provider. Every event goes to a JSON-lines log with the session time it was published,
for the latency report.

Events, as sent to the browser (times in seconds; `start`/`end` on the recording's
clock, `received`/`published` on the session's):

    session        the session's state, including where the audio is now
    interim        words the provider may still revise
    final          settled words; finals never overlap
    utterance_end  a pause after the last word
    end            the session is over: done, stopped or failed
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import aclosing, suppress
from pathlib import Path
from typing import IO, Any, Literal

from app.live.replay import Replay
from app.live.stt import LiveTranscriber, StreamRequest, Update

logger = logging.getLogger(__name__)

Status = Literal["live", "done", "stopped", "failed"]

# Events a tab may have waiting before it's dropped. Interims come a few a second, so
# this is well over a minute behind.
MAX_BACKLOG = 500


class Subscriber:
    def __init__(self) -> None:
        # None wakes a tab that was dropped.
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(MAX_BACKLOG)


def update_event(update: Update) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": update.kind,
        "start": round(update.start, 3),
        "end": round(update.end, 3),
        "words": [
            {
                "text": w.text,
                "start": round(w.start, 3),
                "end": round(w.end, 3),
                "speaker": w.speaker,
            }
            for w in update.words
        ],
        "received": round(update.received, 3),
        "audio_sent": round(update.audio_sent, 3),
    }
    if update.speech_final:
        event["speech_final"] = True
    return event


class LiveSession:
    def __init__(
        self,
        call_id: int,
        replay: Replay,
        transcriber: LiveTranscriber,
        request: StreamRequest,
        *,
        fresh: bool = False,
        log_dir: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.call_id = call_id
        self.replay = replay
        self.request = request
        self.provider = transcriber.provider.name
        self.recorded = not fresh and transcriber.has_recording(replay, request)
        self.status: Status = "live"
        self.error: str | None = None
        # Everything but interims, which a tab that joins late has no use for.
        self.history: list[dict[str, Any]] = []
        self._transcriber = transcriber
        self._fresh = fresh
        self._sleep = sleep
        self._subscribers: set[Subscriber] = set()
        self._task: asyncio.Task | None = None
        self._log: IO[str] | None = None
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._log = (log_dir / f"{stamp}-call{call_id}-{self.id}.jsonl").open(
                "w", encoding="utf-8"
            )

    def start(self) -> None:
        self.replay.start()
        self._write({**self.snapshot(), "source": self.replay.describe(), "fresh": self._fresh})
        self._task = asyncio.create_task(self._run(), name=f"live-session-{self.id}")

    async def wait(self) -> None:
        """Until the session is over."""
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        # A task cancelled before it ran never got to say so.
        self._finish("stopped")

    def position(self) -> float:
        """Where in the recording the call is now."""
        played = min(self.replay.elapsed() * self.replay.speed, self.replay.duration)
        return self.replay.offset + played

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "session",
            "id": self.id,
            "call_id": self.call_id,
            "status": self.status,
            "error": self.error,
            "provider": self.provider,
            "model": self.request.model,
            "recorded": self.recorded,
            "offset": round(self.replay.offset, 3),
            "duration": round(self.replay.duration, 3),
            "speed": self.replay.speed,
            "position": round(self.position(), 3),
            "elapsed": round(self.replay.elapsed(), 3),
        }

    def subscribe(self) -> tuple[list[dict[str, Any]], Subscriber]:
        """What a new tab needs to catch up (the state, then the history), and its queue
        for what comes next. A finished session's history ends with its end event."""
        subscriber = Subscriber()
        if self.status == "live":
            self._subscribers.add(subscriber)
        return [self.snapshot(), *self.history], subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        event["published"] = round(self.replay.elapsed(), 3)
        self._write(event)
        if event["type"] != "interim":
            self.history.append(event)
        for subscriber in list(self._subscribers):
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("live session %s: dropping a tab that fell behind", self.id)
                self._subscribers.discard(subscriber)
                while not subscriber.queue.empty():
                    subscriber.queue.get_nowait()
                subscriber.queue.put_nowait(None)

    async def _run(self) -> None:
        try:
            stream = self._transcriber.stream(self.replay, self.request, use_cache=not self._fresh)
            async with aclosing(stream) as updates:
                async for update in updates:
                    # A recorded stream comes back all at once; play it on its own timing.
                    if (wait := update.received - self.replay.elapsed()) > 0:
                        await self._sleep(wait)
                    self.publish(update_event(update))
            self._finish("done")
        except asyncio.CancelledError:
            self._finish("stopped")
            raise
        except Exception as e:
            logger.exception("live session %s failed", self.id)
            self._finish("failed", str(e))

    def _finish(self, status: Status, error: str | None = None) -> None:
        if self.status != "live":
            return
        self.status, self.error = status, error
        self.publish({"type": "end", "status": status, "error": error})
        self._subscribers.clear()
        if self._log is not None:
            self._log.close()
            self._log = None

    def _write(self, event: dict[str, Any]) -> None:
        if self._log is not None:
            self._log.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._log.flush()


class LiveSessions:
    """The API process's live sessions: at most one running per call, and the latest one
    per call kept after it ends so its transcript can still be read."""

    def __init__(
        self,
        transcriber: Callable[[], LiveTranscriber],
        *,
        request: StreamRequest,
        log_dir: Path | None = None,
    ) -> None:
        self._make_transcriber = transcriber
        self._transcriber: LiveTranscriber | None = None
        self.request = request
        self._log_dir = log_dir
        self._by_call: dict[int, LiveSession] = {}
        self._starting = asyncio.Lock()

    def get(self, session_id: str) -> LiveSession | None:
        return next((s for s in self._by_call.values() if s.id == session_id), None)

    def latest(self, call_id: int) -> LiveSession | None:
        return self._by_call.get(call_id)

    async def start(self, call_id: int, audio: Path, *, fresh: bool = False) -> LiveSession:
        """Start replaying a call, or return its session if one is already running."""
        async with self._starting:
            running = self._by_call.get(call_id)
            if running is not None and running.status == "live":
                return running
            if self._transcriber is None:
                self._transcriber = self._make_transcriber()
            # Cutting an hour of MP3 into chunks takes a moment; keep it off the event loop.
            replay = await asyncio.to_thread(lambda: Replay(audio.read_bytes()))
            session = LiveSession(
                call_id, replay, self._transcriber, self.request, fresh=fresh, log_dir=self._log_dir
            )
            self._by_call[call_id] = session
            session.start()
            return session

    async def aclose(self) -> None:
        for session in self._by_call.values():
            await session.stop()
