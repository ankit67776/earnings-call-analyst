import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main
from app.calls.stt import TransientSTTError
from app.config import settings
from app.live import session as live_session
from app.live.replay import Replay
from app.live.session import LiveSession, LiveSessions
from app.live.stt import LiveTranscriber, LiveWord, StreamRequest, Update
from tests.fakes import FakeTime
from tests.test_live_stt import FRAME, ScriptedStream

REQUEST = StreamRequest(model="nova-3")


def word(text: str, start: float, speaker: int = 0) -> LiveWord:
    return LiveWord(text, start, start + 0.1, speaker)


UPDATES = (
    Update("interim", 0.0, 0.1, [word("Good", 0.0)]),
    Update("final", 0.0, 0.2, [word("Good", 0.0), word("afternoon.", 0.1)]),
    Update("utterance_end", 0.2, 0.2, speech_final=True),
)


def session(
    tmp_path, time: FakeTime, *updates: Update, fresh: bool = False
) -> tuple[LiveSession, ScriptedStream]:
    provider = ScriptedStream(*(updates or UPDATES))
    transcriber = LiveTranscriber(provider, cache_dir=tmp_path / "recorded")
    replay = Replay(FRAME * 20, clock=time.clock, sleep=time.sleep)
    s = LiveSession(
        7, replay, transcriber, REQUEST, fresh=fresh, log_dir=tmp_path / "logs", sleep=time.sleep
    )
    return s, provider


@pytest.mark.anyio
async def test_tabs_get_every_event_and_late_tabs_catch_up_from_history(tmp_path) -> None:
    s, _ = session(tmp_path, FakeTime())
    backlog, tab = s.subscribe()
    assert [e["type"] for e in backlog] == ["session"]
    s.start()
    await s.wait()

    events = []
    while not tab.queue.empty():
        events.append(tab.queue.get_nowait())
    assert [e["type"] for e in events] == ["interim", "final", "utterance_end", "end"]
    assert events[1]["words"][1] == {"text": "afternoon.", "start": 0.1, "end": 0.2, "speaker": 0}
    assert events[-1] == {
        "type": "end",
        "status": "done",
        "error": None,
        "published": pytest.approx(s.replay.duration, abs=1e-3),
    }

    # A tab that joins after the end gets the history, interims left out.
    late, _ = s.subscribe()
    assert [e["type"] for e in late] == ["session", "final", "utterance_end", "end"]
    assert late[0]["status"] == "done"

    [log] = (tmp_path / "logs").glob("*.jsonl")
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["type"] for e in lines] == ["session", "interim", "final", "utterance_end", "end"]
    assert lines[0]["source"]["audio_sha256"] == s.replay.describe()["audio_sha256"]


@pytest.mark.anyio
async def test_a_recorded_stream_plays_back_on_its_recorded_timing(tmp_path) -> None:
    time = FakeTime()
    first, provider = session(tmp_path, time)
    assert not first.recorded
    first.start()
    await first.wait()
    recorded_at = [e["received"] for e in first.history if e["type"] == "final"]

    again, again_provider = session(tmp_path, time)
    assert again.recorded
    start = time.now
    again.start()
    await again.wait()
    assert (provider.calls, again_provider.calls) == (1, 0)
    final = next(e for e in again.history if e["type"] == "final")
    # Published when it arrived the first time, not all at once.
    assert final["published"] == pytest.approx(recorded_at[0])
    assert time.now - start == pytest.approx(recorded_at[0], abs=1e-3)


@pytest.mark.anyio
async def test_a_tab_that_falls_behind_is_dropped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(live_session, "MAX_BACKLOG", 2)
    s, _ = session(tmp_path, FakeTime())
    _, slow = s.subscribe()
    s.start()
    await s.wait()
    # Its queue is emptied and woken with None; it gets nothing more.
    assert slow.queue.get_nowait() is None
    assert slow.queue.empty()


@pytest.mark.anyio
async def test_stopped_and_failed_sessions_end_with_their_status(tmp_path) -> None:
    class Failing(ScriptedStream):
        async def stream(self, audio, request):
            yield UPDATES[1]
            raise TransientSTTError("deepgram stream dropped: 1011 NET-0001")

    transcriber = LiveTranscriber(Failing(), cache_dir=None)
    time = FakeTime()
    failed = LiveSession(1, Replay(FRAME * 20, clock=time.clock, sleep=time.sleep), transcriber,
                         REQUEST, sleep=time.sleep)  # fmt: skip
    failed.start()
    await failed.wait()
    assert (failed.status, failed.history[-1]["status"]) == ("failed", "failed")
    assert "NET-0001" in failed.error

    # Real time, so the replay is still sending when it's stopped.
    stopped = LiveSession(2, Replay(FRAME * 2000), LiveTranscriber(ScriptedStream()), REQUEST)
    stopped.start()
    await stopped.stop()
    assert stopped.history[-1]["status"] == "stopped"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "")
    audio = tmp_path / "call.mp3"
    audio.write_bytes(FRAME * 20)

    async def lookup(call_id: int):
        return audio if call_id == 1 else None

    main.app.dependency_overrides[main.get_audio_lookup] = lambda: lookup
    try:
        with TestClient(main.app) as c:
            main.app.state.live = LiveSessions(
                lambda: LiveTranscriber(ScriptedStream(*UPDATES)), request=REQUEST
            )
            yield c
    finally:
        main.app.dependency_overrides.clear()


def test_start_a_session_and_follow_it_over_a_websocket(client) -> None:
    started = client.post("/api/live/sessions", json={"call_id": 1})
    assert started.status_code == 201
    snapshot = started.json()
    assert (snapshot["status"], snapshot["provider"]) == ("live", "fake")
    assert not snapshot["recorded"]

    with client.websocket_connect(f"/api/live/sessions/{snapshot['id']}/events") as ws:
        events = [ws.receive_json()]
        while events[-1]["type"] != "end":
            events.append(ws.receive_json())
    types = [e["type"] for e in events]
    assert types[0] == "session" and types[-1] == "end"
    assert "final" in types and "utterance_end" in types

    [listed] = client.get("/api/live/sessions", params={"call_id": 1}).json()
    assert (listed["id"], listed["status"]) == (snapshot["id"], "done")
    assert client.get("/api/live/sessions", params={"call_id": 2}).json() == []


def test_live_errors(client) -> None:
    assert client.post("/api/live/sessions", json={"call_id": 2}).status_code == 404
    assert client.delete("/api/live/sessions/nope").status_code == 404
    with client.websocket_connect("/api/live/sessions/nope/events") as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 4404


def test_call_audio_supports_seeking(client) -> None:
    whole = client.get("/api/calls/1/audio")
    assert (whole.status_code, whole.headers["content-type"]) == (200, "audio/mpeg")
    part = client.get("/api/calls/1/audio", headers={"Range": "bytes=0-99"})
    assert (part.status_code, len(part.content)) == (206, 100)
    assert client.get("/api/calls/2/audio").status_code == 404
