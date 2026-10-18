"""Split an MP3 into parts small enough to upload, without decoding it.

Groq's free tier takes uploads of at most 25 MB, and some calls are bigger. MP3 is a
sequence of self-contained frames, so cutting between frames gives valid files, and
summing frame durations gives each part's exact start time. No ffmpeg needed.
"""

from dataclasses import dataclass

# Layer III only: what call recordings use.
_BITRATES = {
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {
    3: (44100, 48000, 32000),  # MPEG-1
    2: (22050, 24000, 16000),  # MPEG-2
    0: (11025, 12000, 8000),  # MPEG-2.5
}


class AudioError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    offset: int
    length: int
    samples: int
    sample_rate: int


@dataclass(frozen=True)
class AudioPart:
    data: bytes
    # Seconds from the start of the original file.
    start: float
    duration: float


def frames(data: bytes) -> list[Frame]:
    """Every audio frame in the file, skipping ID3 tags and a leading Xing/Info frame."""
    pos = _skip_id3v2(data)
    found: list[Frame] = []
    while pos + 4 <= len(data):
        frame = _parse_header(data, pos)
        # A real frame is followed by another one (or the end of the audio); anything
        # else is a false sync inside junk or a tag.
        if frame is None or not _next_is_frame(data, pos + frame.length):
            if found and data[pos : pos + 3] == b"TAG":
                break
            pos += 1
            continue
        found.append(frame)
        pos += frame.length
    if not found:
        raise AudioError("no MPEG audio frames found (is this an MP3?)")
    if _is_info_frame(data, found[0]):
        found.pop(0)
    return found


def duration(data: bytes) -> float:
    return sum(f.samples / f.sample_rate for f in frames(data))


def split(data: bytes, max_bytes: int, *, overlap: float = 0.0) -> list[AudioPart]:
    """Cut into the fewest parts of at most `max_bytes`, of roughly equal size. Each part
    but the last runs on for `overlap` seconds into the next, so a word on the cut is
    heard whole at least once."""
    all_frames = frames(data)
    total = sum(f.length for f in all_frames)
    if total <= max_bytes:
        return [_part(data, all_frames, 0, len(all_frames), 0.0)]

    count = -(-total // max_bytes)
    while True:
        bounds = _cut_points(all_frames, total / count)
        parts, ok, start_time = [], True, 0.0
        for i, (first, end) in enumerate(bounds):
            stop = end if i == len(bounds) - 1 else _extend(all_frames, end, overlap)
            part = _part(data, all_frames, first, stop, start_time)
            if len(part.data) > max_bytes:
                ok = False
                break
            parts.append(part)
            start_time += sum(f.samples / f.sample_rate for f in all_frames[first:end])
        if ok:
            return parts
        count += 1


def _cut_points(all_frames: list[Frame], target: float) -> list[tuple[int, int]]:
    """(first, end) frame ranges of about `target` bytes each."""
    bounds, first, size = [], 0, 0
    for i, f in enumerate(all_frames):
        size += f.length
        if size >= target and i + 1 < len(all_frames):
            bounds.append((first, i + 1))
            first, size = i + 1, 0
    if first < len(all_frames):
        bounds.append((first, len(all_frames)))
    return bounds


def _extend(all_frames: list[Frame], end: int, seconds: float) -> int:
    covered = 0.0
    while end < len(all_frames) and covered < seconds:
        covered += all_frames[end].samples / all_frames[end].sample_rate
        end += 1
    return end


def _part(data: bytes, all_frames: list[Frame], first: int, stop: int, start: float) -> AudioPart:
    chunk = all_frames[first:stop]
    body = b"".join(data[f.offset : f.offset + f.length] for f in chunk)
    return AudioPart(body, start, sum(f.samples / f.sample_rate for f in chunk))


def _skip_id3v2(data: bytes) -> int:
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = 0
    for b in data[6:10]:
        size = (size << 7) | (b & 0x7F)
    footer = 10 if data[5] & 0x10 else 0
    return 10 + size + footer


def _parse_header(data: bytes, pos: int) -> Frame | None:
    b0, b1, b2 = data[pos], data[pos + 1], data[pos + 2]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 0x3
    layer = (b1 >> 1) & 0x3
    bitrate_index = b2 >> 4
    rate_index = (b2 >> 2) & 0x3
    if version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    mpeg1 = version == 3
    bitrate = _BITRATES[1 if mpeg1 else 2][bitrate_index] * 1000
    sample_rate = _SAMPLE_RATES[version][rate_index]
    padding = (b2 >> 1) & 0x1
    length = (144 if mpeg1 else 72) * bitrate // sample_rate + padding
    return Frame(pos, length, 1152 if mpeg1 else 576, sample_rate)


def _next_is_frame(data: bytes, pos: int) -> bool:
    if pos + 4 > len(data):
        return pos <= len(data)
    if data[pos : pos + 3] == b"TAG":
        return True
    return _parse_header(data, pos) is not None


def _is_info_frame(data: bytes, frame: Frame) -> bool:
    head = data[frame.offset : frame.offset + min(frame.length, 64)]
    return any(tag in head for tag in (b"Xing", b"Info", b"VBRI"))
