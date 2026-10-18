import pytest

from app.calls import audio

# MPEG-1 Layer III, 128 kbps, 44.1 kHz, no padding: 417-byte frames of 1152 samples.
HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])
FRAME = HEADER + bytes(413)
SECONDS_PER_FRAME = 1152 / 44100


def mp3(frames: int, *, id3: bool = True, xing: bool = True) -> bytes:
    data = b""
    if id3:
        # ID3v2 header with a syncsafe size of 20 bytes, then the tag body.
        data += b"ID3\x03\x00\x00\x00\x00\x00\x14" + bytes(20)
    if xing:
        data += HEADER + bytes(32) + b"Xing" + bytes(413 - 36)
    return data + FRAME * frames + b"TAG" + bytes(125)


def test_frames_skip_tags_and_the_info_frame() -> None:
    found = audio.frames(mp3(100))
    assert len(found) == 100
    assert {f.length for f in found} == {417}
    assert audio.duration(mp3(100)) == pytest.approx(100 * SECONDS_PER_FRAME)


def test_small_files_stay_whole() -> None:
    [part] = audio.split(mp3(10), max_bytes=10_000)
    assert part.start == 0
    assert part.data == FRAME * 10


def test_split_into_equal_parts_under_the_limit_with_overlap() -> None:
    parts = audio.split(mp3(300), max_bytes=60_000, overlap=1.0)
    assert len(parts) == 3
    assert all(len(p.data) <= 60_000 for p in parts)
    # Each part starts where the previous one's own frames end...
    assert parts[1].start == pytest.approx(100 * SECONDS_PER_FRAME)
    assert parts[2].start == pytest.approx(200 * SECONDS_PER_FRAME)
    # ...but runs on about a second into the next.
    assert parts[0].duration == pytest.approx(139 * SECONDS_PER_FRAME)
    assert parts[2].duration == pytest.approx(100 * SECONDS_PER_FRAME)


def test_not_an_mp3() -> None:
    with pytest.raises(audio.AudioError):
        audio.frames(b"RIFF" + bytes(1000))


def test_overlap_counts_against_the_limit() -> None:
    # 3 parts of 100 frames would fit, but not with a second of overlap each.
    parts = audio.split(mp3(300), max_bytes=50_000, overlap=1.0)
    assert len(parts) == 4
    assert all(len(p.data) <= 50_000 for p in parts)
