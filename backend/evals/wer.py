"""Word error rate of batch speech-to-text against the Earnings-21 reference transcripts.

Both sides go through the Whisper paper's English text normalizer (lowercase, no
punctuation or fillers, spelled-out numbers turned into digits), so "third quarter" and
"3rd Quarter" count as the same words. WER is corpus-level: all errors over all
reference words. Earnings-21 tags entities in its references, so the eval also reports
how many words inside numbers (amounts, percentages, dates...) and names (people,
companies, products) came out wrong: for an analyst those matter more than the rest.

Speakers come from the dataset's reference diarization, so the speaker check only tests
how well segments are matched to it: the share of correctly transcribed words that end
up with the speaker the reference transcript gives them.

The references are in data/raw (gitignored), so this runs locally, not in CI.
Transcriptions come from the STT cache, so a re-run costs no quota.

    uv run python -m evals.wer                        # configured STT model
    uv run python -m evals.wer --model whisper-large-v3 --model whisper-large-v3-turbo --readme
"""

import argparse
import asyncio
import json
import sys
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jiwer

from app.calls import earnings21
from app.calls.earnings21 import RefToken
from app.calls.stt import SpeechRequest, get_transcriber
from app.calls.transcript import LabeledSegment, label_speakers, transcribe_audio
from app.config import settings
from evals.readme import replace_block

with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)  # an unescaped regex in the package
    from whisper_normalizer.english import EnglishTextNormalizer

RESULTS = Path(__file__).resolve().parent / "results" / "stt.json"

ENTITY_GROUPS = {
    "numbers": {
        "CARDINAL", "DATE", "MONEY", "ORDINAL", "PERCENT", "QUANTITY", "RANGE", "TIME", "YEAR"
    },
    "names": {"PERSON", "ORG", "PRODUCT"},
}  # fmt: skip

_normalize = EnglishTextNormalizer()


@dataclass
class Score:
    ref_words: int = 0
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    # Reference words per entity group, and how many of them were transcribed exactly.
    entity_words: Counter = field(default_factory=Counter)
    entity_hits: Counter = field(default_factory=Counter)

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def wer(self) -> float:
        return self.errors / self.ref_words if self.ref_words else 0.0

    def entity_error(self, group: str) -> float | None:
        total = self.entity_words[group]
        return 1 - self.entity_hits[group] / total if total else None

    def add(self, other: "Score") -> None:
        self.ref_words += other.ref_words
        self.substitutions += other.substitutions
        self.deletions += other.deletions
        self.insertions += other.insertions
        self.entity_words.update(other.entity_words)
        self.entity_hits.update(other.entity_hits)

    def summary(self) -> dict[str, Any]:
        return {
            "wer": round(self.wer, 4),
            "ref_words": self.ref_words,
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            **{
                f"{group}_error": None if (e := self.entity_error(group)) is None else round(e, 4)
                for group in ENTITY_GROUPS
            },
            "entity_words": dict(self.entity_words),
        }


def normalize(text: str) -> list[str]:
    return _normalize(text).split()


def reference_words(tokens: list[RefToken]) -> tuple[list[str], list[str | None]]:
    """Normalized reference words, each with the entity group it came from (or None).

    Each run of tokens in one group is normalized as a piece, so a word keeps its group
    through normalization ("$32.7 million" becomes "$32700000", still a number)."""
    words: list[str] = []
    groups: list[str | None] = []
    piece: list[RefToken] = []

    def flush() -> None:
        if piece:
            out = normalize(" ".join(t.text + t.punctuation for t in piece))
            words.extend(out)
            groups.extend([_group(piece[0])] * len(out))
            piece.clear()

    for token in tokens:
        if piece and _group(token) != _group(piece[0]):
            flush()
        piece.append(token)
    flush()
    return words, groups


def score(tokens: list[RefToken], hypothesis: str) -> Score:
    ref, groups = reference_words(tokens)
    hyp = normalize(hypothesis)
    out = jiwer.process_words(" ".join(ref), " ".join(hyp))
    result = Score(
        ref_words=len(ref),
        substitutions=out.substitutions,
        deletions=out.deletions,
        insertions=out.insertions,
    )
    hit = [False] * len(ref)
    for chunk in out.alignments[0]:
        if chunk.type == "equal":
            for i in range(chunk.ref_start_idx, chunk.ref_end_idx):
                hit[i] = True
    for group, ok in zip(groups, hit, strict=True):
        if group is not None:
            result.entity_words[group] += 1
            result.entity_hits[group] += ok
    return result


def speaker_accuracy(
    tokens: list[RefToken], names: dict[int, str], segments: list[LabeledSegment]
) -> float:
    """Share of correctly transcribed words attributed to the reference's speaker."""
    pairs = aligned_speakers(tokens, names, segments)
    return sum(ref == hyp for ref, hyp in pairs) / len(pairs) if pairs else 0.0


def aligned_speakers(
    tokens: list[RefToken], names: dict[int, str], segments: list[LabeledSegment]
) -> list[tuple[str | None, str]]:
    """(reference speaker, transcript speaker) for every correctly transcribed word."""
    ref: list[str] = []
    ref_speakers: list[str | None] = []
    piece: list[RefToken] = []

    def flush() -> None:
        if piece:
            out = normalize(" ".join(t.text + t.punctuation for t in piece))
            ref.extend(out)
            ref_speakers.extend([names.get(piece[0].speaker)] * len(out))
            piece.clear()

    for token in tokens:
        if piece and token.speaker != piece[0].speaker:
            flush()
        piece.append(token)
    flush()
    hyp: list[str] = []
    hyp_speakers: list[str] = []
    for s in segments:
        out = normalize(s.text)
        hyp.extend(out)
        hyp_speakers.extend([s.speaker] * len(out))
    out = jiwer.process_words(" ".join(ref), " ".join(hyp))
    pairs = []
    for chunk in out.alignments[0]:
        if chunk.type == "equal":
            for i, j in zip(
                range(chunk.ref_start_idx, chunk.ref_end_idx),
                range(chunk.hyp_start_idx, chunk.hyp_end_idx),
                strict=True,
            ):
                pairs.append((ref_speakers[i], hyp_speakers[j]))
    return pairs


def _group(token: RefToken) -> str | None:
    return next((g for g, classes in ENTITY_GROUPS.items() if classes & set(token.entities)), None)


async def evaluate(model: str, tickers: list[str]) -> dict[str, Any]:
    transcriber = get_transcriber()
    overall, calls, audio_seconds, latency_ms = Score(), {}, 0.0, 0.0
    try:
        for ticker in tickers:
            file_id = earnings21.CALLS[ticker]
            data = earnings21.audio_path(file_id).read_bytes()
            result = await transcribe_audio(
                transcriber, data, SpeechRequest(model=model), filename=f"{file_id}.mp3"
            )
            tokens = earnings21.reference(file_id)
            call_score = score(tokens, result.text)
            overall.add(call_score)
            names = earnings21.speaker_names(file_id)
            segments = label_speakers(
                result.segments, earnings21.speaker_turns(file_id), names, result.words
            )
            speakers = speaker_accuracy(tokens, names, segments)
            calls[ticker] = {
                "earnings21_id": file_id,
                **call_score.summary(),
                "speaker_accuracy": round(speakers, 4),
            }
            audio_seconds += result.duration
            latency_ms += result.latency_ms
    finally:
        await transcriber.aclose()
    return {
        "provider": transcriber.provider.name,
        "model": model,
        "calls": len(calls),
        "audio_minutes": round(audio_seconds / 60, 1),
        **overall.summary(),
        # Wall-clock time of the API calls when they were recorded, not of this run.
        "speed_factor": round(audio_seconds / (latency_ms / 1000)) if latency_ms else None,
        "by_call": calls,
    }


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{100 * v:.1f}%"


def table(results: list[dict]) -> str:
    tickers = list(results[0]["by_call"])
    rows = [
        "| Model | WER | "
        + " | ".join(f"{t} WER" for t in tickers)
        + " | Number words wrong | Name words wrong | Speed |",
        "|---|---|" + "---|" * len(tickers) + "---|---|---|",
    ]
    for r in results:
        per_call = " | ".join(_pct(r["by_call"][t]["wer"]) for t in tickers)
        speed = f"{r['speed_factor']}x real time" if r["speed_factor"] else "–"
        rows.append(
            f"| {r['model']} | {_pct(r['wer'])} | {per_call} | {_pct(r['numbers_error'])} "
            f"| {_pct(r['names_error'])} | {speed} |"
        )
    return "\n".join(rows)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m evals.wer")
    parser.add_argument("--model", action="append", help="repeat to compare models")
    parser.add_argument("--readme", action="store_true", help="update the README table")
    args = parser.parse_args()
    models = args.model or [settings.stt_model]
    tickers = list(earnings21.CALLS)

    results = [asyncio.run(evaluate(model, tickers)) for model in models]
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(table(results))
    if args.readme:
        r = results[0]
        words = r["ref_words"]
        note = (
            f"{r['calls']} calls, {r['audio_minutes']:.0f} minutes of audio, {words:,} "
            "reference words. Both sides normalized with the Whisper English normalizer."
        )
        replace_block("stt", f"{table(results)}\n\n{note}")


if __name__ == "__main__":
    main()
