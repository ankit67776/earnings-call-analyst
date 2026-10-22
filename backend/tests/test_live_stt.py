import asyncio
import json
from contextlib import aclosing, asynccontextmanager

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from app.calls.stt import STTError, TransientSTTError
from app.live.replay import Replay
from app.live.stt import (
    DeepgramStreaming,
    LiveTranscriber,
    LiveWord,
    StreamRequest,
    Update,
    parse,
)
from tests.fakes import FakeTime

pytestmark = pytest.mark.anyio

REQUEST = StreamRequest(model="nova-3")
# MPEG-1 Layer III, 128 kbps, 44.1 kHz: 417-byte frames of 1152 samples.
FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)
SECONDS_PER_FRAME = 1152 / 44100


def results(start: float, duration: float, words: list[tuple], *, final: bool = True) -> dict:
    return {
        "type": "Results",
        "start": start,
        "duration": duration,
        "is_final": final,
        "speech_final": False,
        "channel": {
            "alternatives": [
                {
                    "transcript": " ".join(w[0] for w in words),
                    "words": [
                        {
                            "word": text.lower().strip(",."),
                            "punctuated_word": text,
                            "start": s,
                            "end": e,
                            "speaker": speaker,
                            "confidence": 0.9,
                        }
                        for text, s, e, speaker in words
                    ],
                }
            ]
        },
    }


def test_parse_results_utterance_ends_and_skips_the_rest() -> None:
    final = parse(results(1.0, 2.0, [("Good", 1.1, 1.4, 0), ("afternoon.", 1.4, 2.0, 1)]))
    assert final == Update(
        kind="final",
        start=1.0,
        end=3.0,
        words=[LiveWord("Good", 1.1, 1.4, 0, 0.9), LiveWord("afternoon.", 1.4, 2.0, 1, 0.9)],
    )
    assert final.text == "Good afternoon."
    assert parse(results(1.0, 1.0, [("Good", 1.1, 1.4, 0)], final=False)).kind == "interim"
    # Silence gives results with no words, which carry nothing.
    assert parse(results(5.0, 3.0, [])) is None
    end = parse({"type": "UtteranceEnd", "channel": [0, 1], "last_word_end": 2.0})
    assert (end.kind, end.start, end.speech_final, end.words) == ("utterance_end", 2.0, True, [])
    assert parse({"type": "Metadata", "request_id": "r", "duration": 3.0}) is None
    assert parse({"type": "SpeechStarted", "timestamp": 1.0}) is None


class FakeSocket:
    """Answers with scripted messages while audio comes in, and closes once the client
    asks it to (or closes itself)."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.sent: list[bytes | str] = []
        self.closed = False
        self._done = asyncio.Event()

    async def send(self, data: bytes | str) -> None:
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data)["type"] == "CloseStream":
            self._done.set()

    async def close(self) -> None:
        self.closed = True
        self._done.set()

    async def __aiter__(self):
        for m in self.messages:
            yield json.dumps(m)
        await self._done.wait()


def deepgram(socket: FakeSocket, seen: dict) -> DeepgramStreaming:
    @asynccontextmanager
    async def connect(url: str, *, additional_headers: dict):
        seen["url"], seen["headers"] = url, additional_headers
        yield socket

    return DeepgramStreaming("test-key", connect=connect)


async def chunks(*items: bytes):
    for item in items:
        yield item


async def test_deepgram_streams_audio_then_closes_and_yields_updates() -> None:
    socket = FakeSocket(
        [
            results(0.0, 1.0, [("Good", 0.1, 0.4, 0)], final=False),
            results(0.0, 2.0, [("Good", 0.1, 0.4, 0), ("afternoon.", 0.4, 1.2, 0)]),
            {"type": "Metadata", "request_id": "r1", "duration": 2.0},
        ]
    )
    seen: dict = {}
    async with aclosing(deepgram(socket, seen).stream(chunks(b"a", b"b"), REQUEST)) as s:
        updates = [u async for u in s]

    assert [u.kind for u in updates] == ["interim", "final"]
    assert updates[1].text == "Good afternoon."
    assert seen["headers"] == {"Authorization": "Token test-key"}
    url = seen["url"]
    assert url.startswith("wss://api.deepgram.com/v1/listen?model=nova-3&")
    for param in ("diarize_model=latest", "interim_results=true", "utterance_end_ms=1000"):
        assert param in url
    # MP3 is a container: Deepgram reads the format from it.
    assert "encoding" not in url and "sample_rate" not in url
    assert socket.sent == [b"a", b"b", '{"type": "CloseStream"}']


async def test_deepgram_closes_the_socket_and_raises_when_the_audio_fails() -> None:
    async def broken():
        yield b"a"
        raise OSError("disk gone")

    socket = FakeSocket([])
    with pytest.raises(OSError, match="disk gone"):
        async with aclosing(deepgram(socket, {}).stream(broken(), REQUEST)) as s:
            [u async for u in s]
    assert socket.closed


@pytest.mark.parametrize(("status", "error"), [(401, STTError), (429, TransientSTTError)])
async def test_deepgram_handshake_refusal(status: int, error: type) -> None:
    @asynccontextmanager
    async def connect(url: str, *, additional_headers: dict):
        raise InvalidStatus(Response(status, "", Headers({"dg-error": "Invalid credentials."})))
        yield

    stream = DeepgramStreaming("bad", connect=connect).stream(chunks(b"a"), REQUEST)
    with pytest.raises(error, match=f"refused the stream \\({status}\\): Invalid credentials"):
        [u async for u in stream]


def replay(frames: int, time: FakeTime, **kwargs) -> Replay:
    return Replay(FRAME * frames, clock=time.clock, sleep=time.sleep, **kwargs)


async def test_replay_sends_each_chunk_once_its_audio_has_been_spoken() -> None:
    time = FakeTime()
    r = replay(10, time, speed=2.0, chunk_seconds=0.1)
    sent = [(len(c), time.now, r.sent) async for c in r.chunks()]

    # 0.1s chunks are four 26ms frames; the last chunk takes the two left over.
    assert [n for n, _, _ in sent] == [4 * 417, 4 * 417, 2 * 417]
    ends = [4 * SECONDS_PER_FRAME, 8 * SECONDS_PER_FRAME, 10 * SECONDS_PER_FRAME]
    assert [t for _, t, _ in sent] == pytest.approx([e / 2 for e in ends])
    assert [s for _, _, s in sent] == pytest.approx(ends)
    assert r.elapsed() == pytest.approx(ends[-1] / 2)


def test_replay_plays_a_stretch_starting_on_a_frame() -> None:
    r = replay(100, FakeTime(), start=1.0, duration=0.5)
    # The first frame at or after 1s is the 39th; frames that start within 0.5s follow.
    assert r.offset == pytest.approx(39 * SECONDS_PER_FRAME)
    assert r.duration == pytest.approx(19 * SECONDS_PER_FRAME)
    assert r.describe()["offset"] == pytest.approx(r.offset)


class ScriptedStream:
    """Takes all the audio, then answers with scripted updates."""

    name = "fake"

    def __init__(self, *updates: Update) -> None:
        self.updates = updates
        self.calls = 0

    async def stream(self, audio, request):
        self.calls += 1
        async for _ in audio:
            pass
        for u in self.updates:
            yield u


WORDS = [LiveWord("Hello.", 0.1, 0.3, 0)]


async def test_live_transcriber_stamps_shifts_records_and_replays(tmp_path) -> None:
    provider = ScriptedStream(Update("final", 0.0, 0.5, WORDS))
    transcriber = LiveTranscriber(provider, cache_dir=tmp_path)
    time = FakeTime()
    r = replay(100, time, start=1.0, duration=0.5)

    [update] = [u async for u in transcriber.stream(r, REQUEST)]
    # Times move onto the recording's clock; arrival is stamped from the source.
    assert update.start == pytest.approx(r.offset)
    assert update.words[0].start == pytest.approx(r.offset + 0.1)
    assert update.received == pytest.approx(r.duration)
    assert update.audio_sent == pytest.approx(r.offset + r.duration)

    again = replay(100, time, start=1.0, duration=0.5)
    assert [u async for u in transcriber.stream(again, REQUEST)] == [update]
    assert provider.calls == 1
    # A different speed is a different run.
    faster = replay(100, time, start=1.0, duration=0.5, speed=4.0)
    [u async for u in transcriber.stream(faster, REQUEST)]
    assert provider.calls == 2
    [u async for u in transcriber.stream(again, REQUEST, use_cache=False)]
    assert provider.calls == 3


async def test_live_transcriber_does_not_record_a_run_stopped_early(tmp_path) -> None:
    provider = ScriptedStream(Update("final", 0.0, 0.5, WORDS), Update("final", 0.5, 1.0, WORDS))
    transcriber = LiveTranscriber(provider, cache_dir=tmp_path)
    async with aclosing(transcriber.stream(replay(10, FakeTime()), REQUEST)) as updates:
        async for _ in updates:
            break
    assert not list(tmp_path.rglob("*.json"))


async def test_live_transcriber_raises_provider_errors(tmp_path) -> None:
    class Failing(ScriptedStream):
        async def stream(self, audio, request):
            yield Update("final", 0.0, 0.5, WORDS)
            raise TransientSTTError("deepgram stream dropped: 1011 NET-0001")

    transcriber = LiveTranscriber(Failing(), cache_dir=tmp_path)
    got = []
    with pytest.raises(TransientSTTError, match="NET-0001"):
        async for u in transcriber.stream(replay(10, FakeTime()), REQUEST):
            got.append(u)
    assert len(got) == 1
    assert not list(tmp_path.rglob("*.json"))
