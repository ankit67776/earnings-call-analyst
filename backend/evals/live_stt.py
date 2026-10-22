"""Word error rate of live (streaming) speech-to-text, next to the batch transcript.

Each call is replayed into the streaming provider from start to end, the way live mode
hears it, and the final results are joined into one transcript. That transcript is
scored exactly like the batch one (evals.wer): same references, same normalizer, same
number and name checks. The table puts it under the batch Whisper row from
results/stt.json.

Two live configurations are scored. The one live mode uses writes numbers and dates
the way a reader expects ("$32.7 million", "11/04/2020"); the other writes them as
spoken. The normalizer turns both "$32.7 million" and "thirty two point seven million
dollars" into the same digits, but not every date or code, so the gap between the two
rows shows how much of the live WER is formatting.

Live mode labels speakers with the provider's diarization (speaker 0, 1, ...) instead
of the dataset's reference turns. Each provider speaker is matched to the one reference
speaker it shares the most correctly transcribed words with, no two to the same person,
and the score is the share of correctly transcribed words whose matched speaker is
right. Splitting one person into two labels or merging two people costs words.

Calls stream at 1x, as live mode hears them, all at once: a full run takes as long as
the longest call (63 minutes). Faster replay doesn't help: Deepgram works through a
stream at about 1.25x real time, so audio sent faster only queues up. Streams are
recorded (.cache/live_stt), so a re-run costs no STT credit.

    uv run python -m evals.live_stt
    uv run python -m evals.live_stt --readme
"""

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.calls import earnings21
from app.calls.transcript import LabeledSegment
from app.config import settings
from app.live.replay import Replay
from app.live.stt import LiveTranscriber, LiveWord, StreamRequest, get_live_transcriber
from evals.readme import replace_block
from evals.wer import RESULTS as BATCH_RESULTS
from evals.wer import Score, aligned_speakers, score

RESULTS = Path(__file__).resolve().parent / "results" / "live_stt.json"


def variants(model: str) -> dict[str, StreamRequest]:
    request = StreamRequest(model=model)
    return {
        model: request,
        f"{model}, numbers as spoken": replace(request, smart_format=False),
    }


def segments(words: list[LiveWord]) -> list[LabeledSegment]:
    """Runs of one diarization label, as transcript segments."""
    out: list[LabeledSegment] = []
    run: list[LiveWord] = []
    for i, w in enumerate(words):
        run.append(w)
        if i + 1 == len(words) or words[i + 1].speaker != w.speaker:
            label = "" if w.speaker is None else f"S{w.speaker}"
            n = len(out) + 1
            out.append(
                LabeledSegment(n, n, label, run[0].start, w.end, " ".join(x.text for x in run))
            )
            run = []
    return out


def match_speakers(pairs: list[tuple[str | None, str]]) -> dict[str, str]:
    """Each diarization label to the reference speaker it shares the most words with,
    taking the biggest overlaps first, no reference speaker twice."""
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for (ref, hyp), _ in Counter(pairs).most_common():
        if ref is not None and hyp not in mapping and ref not in taken:
            mapping[hyp] = ref
            taken.add(ref)
    return mapping


def diarization_accuracy(pairs: list[tuple[str | None, str]]) -> float:
    mapping = match_speakers(pairs)
    return sum(mapping.get(hyp) == ref for ref, hyp in pairs) / len(pairs) if pairs else 0.0


async def _final_words(
    transcriber: LiveTranscriber, replay: Replay, request: StreamRequest
) -> list[LiveWord]:
    words: list[LiveWord] = []
    async with aclosing(transcriber.stream(replay, request)) as updates:
        async for update in updates:
            if update.kind == "final":
                words.extend(update.words)
    return words


async def stream_calls(
    transcriber: LiveTranscriber, requests: list[StreamRequest], file_ids: list[str], speed: float
) -> list[list[list[LiveWord]]]:
    """Final words of each call under each request, all streamed at once."""
    audio = {f: earnings21.audio_path(f).read_bytes() for f in file_ids}
    # Cut every file into chunks before the first stream starts, so none waits on it.
    runs = [(r, Replay(audio[f], speed=speed)) for r in requests for f in file_ids]
    # A stream that fails doesn't stop the others, so the ones that finish are recorded.
    words = await asyncio.gather(
        *(_final_words(transcriber, replay, r) for r, replay in runs), return_exceptions=True
    )
    if errors := [w for w in words if isinstance(w, BaseException)]:
        raise errors[0]
    n = len(file_ids)
    return [words[i * n : (i + 1) * n] for i in range(len(requests))]


def evaluate(
    name: str,
    request: StreamRequest,
    provider: str,
    tickers: list[str],
    transcripts: list[list[LiveWord]],
    speed: float,
) -> dict[str, Any]:
    overall, calls, audio_seconds = Score(), {}, 0.0
    speaker_right = speaker_total = 0.0
    for ticker, words in zip(tickers, transcripts, strict=True):
        file_id = earnings21.CALLS[ticker]
        tokens = earnings21.reference(file_id)
        call_score = score(tokens, " ".join(w.text for w in words))
        overall.add(call_score)
        pairs = aligned_speakers(tokens, earnings21.speaker_names(file_id), segments(words))
        accuracy = diarization_accuracy(pairs)
        speaker_right += accuracy * len(pairs)
        speaker_total += len(pairs)
        calls[ticker] = {
            "earnings21_id": file_id,
            **call_score.summary(),
            "speaker_accuracy": round(accuracy, 4),
            "speakers_found": len({w.speaker for w in words}),
            "speakers_in_reference": len({t.speaker for t in tokens}),
        }
        audio_seconds += earnings21.call_info(file_id).duration
    return {
        "name": name,
        "provider": provider,
        "request": {
            "model": request.model,
            "smart_format": request.smart_format,
            "diarize": request.diarize,
            "endpointing_ms": request.endpointing_ms,
        },
        "replay_speed": speed,
        "calls": len(calls),
        "audio_minutes": round(audio_seconds / 60, 1),
        **overall.summary(),
        # Pooled over the calls' correctly transcribed words; each call is matched alone.
        "speaker_accuracy": round(speaker_right / speaker_total, 4) if speaker_total else 0.0,
        "by_call": calls,
    }


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{100 * v:.1f}%"


def table(batch: dict[str, Any] | None, live: list[dict[str, Any]]) -> str:
    tickers = list(live[0]["by_call"])
    rows = [
        "| Transcript | WER | "
        + " | ".join(f"{t} WER" for t in tickers)
        + " | Number words wrong | Name words wrong | Speakers right |",
        "|---|---|" + "---|" * len(tickers) + "---|---|---|",
    ]

    def row(label: str, r: dict[str, Any], speakers: str) -> str:
        per_call = " | ".join(_pct(r["by_call"][t]["wer"]) for t in tickers)
        return (
            f"| {label} | {_pct(r['wer'])} | {per_call} | {_pct(r['numbers_error'])} "
            f"| {_pct(r['names_error'])} | {speakers} |"
        )

    if batch is not None:
        rows.append(row(f"Batch: {batch['model']}", batch, "reference turns"))
    for r in live:
        rows.append(row(f"Live: {r['name']}", r, _pct(r["speaker_accuracy"])))
    return "\n".join(rows)


def _batch_row() -> dict[str, Any] | None:
    try:
        rows = json.loads(BATCH_RESULTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return next((r for r in rows if r["model"] == settings.stt_model), None)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m evals.live_stt")
    parser.add_argument("--model", default=settings.live_stt_model)
    parser.add_argument("--speed", type=float, default=1.0, help="replay speed (default 1x)")
    parser.add_argument("--readme", action="store_true", help="update the README table")
    args = parser.parse_args()
    tickers = list(earnings21.CALLS)

    named = variants(args.model)
    transcriber = get_live_transcriber()
    file_ids = [earnings21.CALLS[t] for t in tickers]
    transcripts = asyncio.run(stream_calls(transcriber, list(named.values()), file_ids, args.speed))
    # Scored only once every stream is done: scoring is CPU work that would stall them.
    live = [
        evaluate(name, request, transcriber.provider.name, tickers, words, args.speed)
        for (name, request), words in zip(named.items(), transcripts, strict=True)
    ]
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    batch = _batch_row()
    print(table(batch, live))
    for r in live:
        found = ", ".join(
            f"{t} {c['speakers_found']} of {c['speakers_in_reference']}"
            for t, c in r["by_call"].items()
        )
        print(f"{r['name']}: speakers found {found}")
    if args.readme:
        r = live[0]
        note = (
            f"{r['calls']} calls, {r['audio_minutes']:.0f} minutes of audio, each streamed "
            f"start to end at {r['replay_speed']:g}x. Same references and normalizer as above."
        )
        replace_block("live-stt", f"{table(batch, live)}\n\n{note}")


if __name__ == "__main__":
    main()
