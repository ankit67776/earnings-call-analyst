"""Turn call audio into a transcript of timed, speaker-labeled segments.

Long recordings are split into parts that fit the provider's upload limit, each part
but the last running on a few seconds into the next. The parts' segments are stitched
back on one timeline: a segment that crosses a cut is kept whole from the earlier part,
and the later part's copy of it is dropped.

Whisper doesn't say who is speaking. When the call comes with speaker turns (Earnings-21
ships reference diarization as RTTM files), each segment goes to the speaker it overlaps
most, split first where word timestamps show the speaker changing mid-segment;
consecutive segments by one speaker form a turn.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from app.calls import audio
from app.calls.earnings21 import SpeakerTurn
from app.calls.stt import Segment, SpeechRequest, Transcriber, Transcription, Word

# How far each part runs on into the next, so a sentence on the cut is heard whole once.
PART_OVERLAP = 15.0


@dataclass(frozen=True)
class LabeledSegment:
    position: int  # 1-based
    turn: int  # 1-based speaker turn
    speaker: str
    start: float
    end: float
    text: str


async def transcribe_audio(
    transcriber: Transcriber, data: bytes, request: SpeechRequest, *, filename: str
) -> Transcription:
    parts = audio.split(data, transcriber.provider.max_upload, overlap=PART_OVERLAP)
    results = []
    for i, part in enumerate(parts):
        name = filename if len(parts) == 1 else f"{i + 1:02d}-{filename}"
        result = await transcriber.transcribe(part.data, request, filename=name)
        results.append((part.start, result.shifted(part.start)))
    return stitch(results)


def stitch(parts: list[tuple[float, Transcription]]) -> Transcription:
    """Join transcriptions of overlapping parts; `parts` pairs each with its start time."""
    first = parts[0][1]
    segments: list[Segment] = []
    words: list[Word] = []
    for i, (_, result) in enumerate(parts):
        boundary = parts[i + 1][0] if i + 1 < len(parts) else float("inf")
        kept_end = segments[-1].end if segments else float("-inf")
        for s in result.segments:
            # Skip what the previous part already covered, then stop at the next cut. A
            # segment that starts before the cut is kept even if it runs past it.
            if (s.start + s.end) / 2 < kept_end or s.start >= boundary:
                continue
            segments.append(s)
        new_end = segments[-1].end if segments else kept_end
        words += [w for w in result.words if kept_end <= w.start < new_end]
    return Transcription(
        provider=first.provider,
        model=first.model,
        duration=parts[-1][0] + parts[-1][1].duration,
        text=" ".join(s.text for s in segments),
        segments=segments,
        words=words,
        latency_ms=sum(r.latency_ms for _, r in parts),
        cached=all(r.cached for _, r in parts),
    )


def label_speakers(
    segments: list[Segment],
    turns: list[SpeakerTurn],
    names: dict[int, str],
    words: list[Word] | None = None,
) -> list[LabeledSegment]:
    """Give each segment the speaker it overlaps most, and number the runs of one speaker
    as turns.

    Whisper's segments don't always break where the speaker changes (an analyst's
    question running into the answer). With word timestamps, each word of a segment is
    placed on the timeline and the segment is split where the speaker changes.
    """
    starts = [t.start for t in turns]
    timed = _time_tokens(segments, words) if words else None
    pieces: list[tuple[int | None, Segment]] = []
    for i, s in enumerate(segments):
        runs = _speaker_runs(timed[i], turns, starts) if timed else []
        if len(runs) <= 1:
            pieces.append((_speaker_at(s.start, s.end, turns, starts), s))
            continue
        for n, (speaker, tokens) in enumerate(runs):
            start = s.start if n == 0 else tokens[0].start
            end = s.end if n == len(runs) - 1 else tokens[-1].end
            text = " ".join(t.text for t in tokens)
            pieces.append((speaker, replace(s, start=start, end=end, text=text)))

    labeled: list[LabeledSegment] = []
    for n, (speaker_id, s) in enumerate(pieces, start=1):
        speaker = names.get(speaker_id, f"Speaker {speaker_id}") if speaker_id is not None else ""
        turn = 1 if not labeled else labeled[-1].turn + (labeled[-1].speaker != speaker)
        labeled.append(LabeledSegment(n, turn, speaker, s.start, s.end, s.text))
    return labeled


def unlabeled(segments: list[Segment]) -> list[LabeledSegment]:
    """Segments with no speaker information, as one turn per segment."""
    return [
        LabeledSegment(n, n, "", s.start, s.end, s.text) for n, s in enumerate(segments, start=1)
    ]


def _time_tokens(segments: list[Segment], words: list[Word]) -> list[list[Word]]:
    """Each segment's whitespace tokens (with their punctuation), timed by the word
    timestamps they line up with. Tokens with no matching word share the time of their
    nearest timed neighbour in the segment, or the segment's own span."""
    tokens = [(i, tok) for i, s in enumerate(segments) for tok in s.text.split()]
    matcher = SequenceMatcher(
        None, [_bare(t) for _, t in tokens], [_bare(w.text) for w in words], autojunk=False
    )
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            w = words[block.b + k]
            times[block.a + k] = (w.start, w.end)

    timed: list[list[Word]] = [[] for _ in segments]
    for (i, tok), span in zip(tokens, times, strict=True):
        timed[i].append(Word(tok, *span) if span else Word(tok, -1.0, -1.0))
    for i, s in enumerate(segments):
        row = timed[i]
        known = [j for j, w in enumerate(row) if w.start >= 0]
        for j, w in enumerate(row):
            if w.start >= 0:
                continue
            if known:
                nearest = row[min(known, key=lambda k: abs(k - j))]
                row[j] = replace(w, start=nearest.start, end=nearest.end)
            else:
                row[j] = replace(w, start=s.start, end=s.end)
    return timed


def _speaker_runs(
    tokens: list[Word], turns: list[SpeakerTurn], starts: list[float]
) -> list[tuple[int | None, list[Word]]]:
    runs: list[tuple[int | None, list[Word]]] = []
    for t in tokens:
        speaker = _speaker_at(t.start, t.end, turns, starts)
        if runs and runs[-1][0] == speaker:
            runs[-1][1].append(t)
        else:
            runs.append((speaker, [t]))
    # A lone word is more likely a timestamp slip than a one-word turn.
    merged: list[tuple[int | None, list[Word]]] = []
    for speaker, run in runs:
        if merged and (len(run) == 1 or merged[-1][0] == speaker):
            merged[-1][1].extend(run)
        else:
            merged.append((speaker, run))
    if len(merged) > 1 and len(merged[0][1]) == 1:
        merged[1] = (merged[1][0], merged[0][1] + merged[1][1])
        merged.pop(0)
    return merged


def _bare(token: str) -> str:
    return "".join(c for c in token.lower() if c.isalnum())


def _speaker_at(
    start: float, end: float, turns: list[SpeakerTurn], starts: list[float]
) -> int | None:
    if not turns:
        return None
    # Turns are sorted by start and short; only those starting shortly before can overlap.
    nearby = turns[max(0, bisect_left(starts, start - 120)) : bisect_right(starts, end) + 1]
    overlap: dict[int, float] = {}
    for t in nearby:
        shared = min(end, t.end) - max(start, t.start)
        if shared > 0:
            overlap[t.speaker] = overlap.get(t.speaker, 0.0) + shared
    if overlap:
        return max(overlap, key=lambda k: overlap[k])
    middle = (start + end) / 2
    nearest = min(nearby or turns, key=lambda t: min(abs(t.start - middle), abs(t.end - middle)))
    return nearest.speaker
