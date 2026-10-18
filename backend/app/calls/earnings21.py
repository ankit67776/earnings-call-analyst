"""Rev's Earnings-21 dataset: call metadata, reference transcripts and speaker turns.

Layout (see data/README.md for the sparse checkout):

    earnings21/earnings21-file-metadata.csv     one row per call
    earnings21/speaker-metadata.csv             speaker names per call
    earnings21/transcripts/nlp_references/*.nlp reference words with speaker IDs
    earnings21/transcripts/wer_tags/*.json      entity classes tagged in those words
    earnings21/rttms/*.rttm                     timed speaker turns
    earnings21/media/*.mp3                      audio
"""

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

# The calls this project uses, keyed by the ticker of the 10-K filer.
CALLS = {"SITM": "4385072", "HSY": "4385939", "COP": "4382825"}
# All Earnings-21 calls are from 2020, and these companies' fiscal years are calendar years.
FISCAL_YEAR = 2020


def default_root() -> Path:
    return Path(settings.raw_data_dir) / "speech-datasets" / "earnings21"


@dataclass(frozen=True)
class CallInfo:
    file_id: str
    company: str
    quarter: int
    sector: str
    duration: float
    sample_rate: int


@dataclass(frozen=True)
class RefToken:
    text: str
    speaker: int
    # Punctuation that follows the token, e.g. "." or ",".
    punctuation: str
    # Classes of the entities the token is part of, e.g. ("MONEY",) for "million" in
    # "$32.7 million". Overlapping entities ("third quarter 2020" is a DATE holding a
    # YEAR) give several.
    entities: tuple[str, ...]


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: int


def call_info(file_id: str, root: Path | None = None) -> CallInfo:
    root = root or default_root()
    with (root / "earnings21-file-metadata.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["file_id"] == file_id:
                return CallInfo(
                    file_id=file_id,
                    company=row["company_name"],
                    quarter=int(row["financial_quarter"]),
                    sector=row["sector"],
                    duration=float(row["audio_length"]),
                    sample_rate=int(row["sample_rate"]),
                )
    raise KeyError(f"no Earnings-21 call {file_id}")


def audio_path(file_id: str, root: Path | None = None) -> Path:
    return (root or default_root()) / "media" / f"{file_id}.mp3"


def reference(file_id: str, root: Path | None = None) -> list[RefToken]:
    """The reference transcript, one token per word as Rev wrote it ("$32.7", "52%")."""
    transcripts = (root or default_root()) / "transcripts"
    classes = {
        entity_id: entity["entity_type"]
        for entity_id, entity in json.loads(
            (transcripts / "wer_tags" / f"{file_id}.wer_tag.json").read_text(encoding="utf-8")
        ).items()
    }
    tokens = []
    with (transcripts / "nlp_references" / f"{file_id}.nlp").open(encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="|", quoting=csv.QUOTE_NONE):
            ids = [t.strip(" '\"") for t in row["wer_tags"].strip("[]").split(",")]
            entities = tuple(dict.fromkeys(classes[i] for i in ids if i in classes))
            tokens.append(RefToken(row["token"], int(row["speaker"]), row["punctuation"], entities))
    return tokens


def speaker_turns(file_id: str, root: Path | None = None) -> list[SpeakerTurn]:
    """Who speaks when, from the dataset's RTTM file (reference diarization)."""
    path = (root or default_root()) / "rttms" / f"{file_id}.rttm"
    turns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[0] != "SPEAKER":
            continue
        start, length = float(fields[3]), float(fields[4])
        turns.append(SpeakerTurn(start, start + length, int(fields[7])))
    return sorted(turns, key=lambda t: t.start)


def speaker_names(file_id: str, root: Path | None = None) -> dict[int, str]:
    root = root or default_root()
    with (root / "speaker-metadata.csv").open(encoding="utf-8") as f:
        return {
            int(row["speaker_id"]): row["speaker_name"]
            for row in csv.DictReader(f)
            if row["file_id"] == file_id
        }
