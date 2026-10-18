import httpx2
import pytest

from app.calls.earnings21 import SpeakerTurn
from app.calls.stt import (
    GroqSpeech,
    Segment,
    SpeechRequest,
    STTError,
    Transcriber,
    Transcription,
    TransientSTTError,
    Word,
)
from app.calls.transcript import label_speakers, stitch
from tests.fakes import FakeTime

pytestmark = pytest.mark.anyio

REQUEST = SpeechRequest(model="whisper-large-v3")
VERBOSE = {
    "duration": 4.0,
    "text": " Good afternoon. Welcome.",
    "segments": [
        {"start": 0.0, "end": 1.5, "text": " Good afternoon.", "avg_logprob": -0.2},
        {"start": 2.0, "end": 4.0, "text": " Welcome.", "no_speech_prob": 0.01},
    ],
    "words": [
        {"word": "Good", "start": 0.1, "end": 0.5},
        {"word": "afternoon", "start": 0.5, "end": 1.4},
        {"word": "Welcome", "start": 2.1, "end": 3.0},
    ],
}


def groq(handler) -> GroqSpeech:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), base_url=GroqSpeech.base_url)
    return GroqSpeech("test-key", http=http)


async def test_groq_sends_verbose_json_request_and_parses_segments_and_words() -> None:
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx2.Response(200, json=VERBOSE)

    result = await groq(handler).transcribe(b"ID3audio", "call.mp3", REQUEST)

    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    body = seen["body"]
    for part in (b"whisper-large-v3", b"verbose_json", b"timestamp_granularities[]", b"word"):
        assert part in body
    assert result.text == "Good afternoon. Welcome."
    assert result.segments[0] == Segment(0.0, 1.5, "Good afternoon.", -0.2, None)
    assert result.words[2] == Word("Welcome", 2.1, 3.0)


async def test_groq_rate_limit_is_transient_with_retry_after() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            429, json={"error": {"message": "audio limit"}}, headers={"retry-after": "42"}
        )

    with pytest.raises(TransientSTTError) as e:
        await groq(handler).transcribe(b"x", "a.mp3", REQUEST)
    assert e.value.retry_after == 42


class ScriptedSpeech:
    name = "fake"
    max_upload = 100

    def __init__(self, *script: Transcription | Exception) -> None:
        self.script = list(script)
        self.calls = 0

    async def transcribe(self, audio: bytes, filename: str, request: SpeechRequest):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def transcription(*segments: Segment, duration: float = 10.0) -> Transcription:
    return Transcription("fake", "m", duration, " ".join(s.text for s in segments), list(segments))


async def test_transcriber_retries_then_caches(tmp_path) -> None:
    time = FakeTime()
    provider = ScriptedSpeech(
        TransientSTTError("busy", retry_after=30), transcription(Segment(0, 1, "Hello."))
    )
    transcriber = Transcriber(provider, cache_dir=tmp_path, sleep=time.sleep)

    first = await transcriber.transcribe(b"audio", REQUEST)
    again = await transcriber.transcribe(b"audio", REQUEST)

    assert time.sleeps == [31]
    assert provider.calls == 2
    assert (first.cached, again.cached) == (False, True)
    assert again.segments == first.segments


async def test_transcriber_refuses_audio_over_the_upload_limit() -> None:
    with pytest.raises(STTError, match="split the audio"):
        await Transcriber(ScriptedSpeech()).transcribe(bytes(101), REQUEST)


def test_stitch_keeps_a_segment_crossing_the_cut_once() -> None:
    # Part 2 starts at 10s; part 1 runs on to 13s, so it hears the sentence at 9-12 whole.
    part1 = transcription(Segment(0, 5, "One."), Segment(9, 12, "Across the cut."), duration=13)
    part2 = transcription(
        Segment(10, 12, "the cut."), Segment(12.5, 15, "Two."), Segment(16, 20, "Three.")
    )
    stitched = stitch([(0.0, part1), (10.0, part2)])
    assert [s.text for s in stitched.segments] == ["One.", "Across the cut.", "Two.", "Three."]
    assert stitched.duration == 20.0


NAMES = {0: "Operator", 1: "Jane Analyst", 2: "Sam CFO"}


def test_each_segment_gets_the_speaker_it_overlaps_most() -> None:
    turns = [SpeakerTurn(0, 4, 0), SpeakerTurn(4, 10, 1), SpeakerTurn(10.5, 20, 2)]
    segments = [Segment(0, 3, "Next question."), Segment(3.5, 9, "How is demand?"),
                Segment(10, 12, "Strong."), Segment(12, 15, "Very strong.")]  # fmt: skip
    labeled = label_speakers(segments, turns, NAMES)
    assert [(s.speaker, s.turn) for s in labeled] == [
        ("Operator", 1), ("Jane Analyst", 2), ("Sam CFO", 3), ("Sam CFO", 3)
    ]  # fmt: skip


def test_word_timestamps_split_a_segment_where_the_speaker_changes() -> None:
    turns = [SpeakerTurn(0, 3.0, 1), SpeakerTurn(3.2, 8, 2)]
    segments = [Segment(0, 6, "What about margins? They improved a lot.")]
    words = [
        Word(w, s, s + 0.4)
        for w, s in [("What", 0.2), ("about", 0.8), ("margins", 1.5), ("They", 3.4),
                     ("improved", 4.0), ("a", 4.6), ("lot", 5.0)]
    ]  # fmt: skip
    labeled = label_speakers(segments, turns, NAMES, words)
    assert [(s.speaker, s.text, s.turn) for s in labeled] == [
        ("Jane Analyst", "What about margins?", 1),
        ("Sam CFO", "They improved a lot.", 2),
    ]
    assert labeled[1].start == 3.4 and labeled[1].end == 6


def test_a_lone_word_does_not_become_its_own_turn() -> None:
    turns = [SpeakerTurn(0, 1.0, 1), SpeakerTurn(1.0, 1.3, 2), SpeakerTurn(1.3, 5, 1)]
    segments = [Segment(0, 3, "Revenue grew strongly this year.")]
    words = [Word(w, i * 0.5, i * 0.5 + 0.3) for i, w in enumerate(segments[0].text.split())]
    labeled = label_speakers(segments, turns, NAMES, words)
    assert [(s.speaker, s.text) for s in labeled] == [("Jane Analyst", segments[0].text)]
