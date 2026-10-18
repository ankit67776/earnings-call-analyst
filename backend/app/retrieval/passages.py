"""Cut filings and call transcripts into passages: the units retrieval returns and the
chat cites.

A passage is a few hundred words: big enough to hold a whole thought (one risk factor,
one answer on the call), small enough that a handful fit in a prompt. Filing passages
are runs of whole paragraphs that prefer to start at a heading; call passages are runs
of segments that prefer to start at a change of speaker, so a question and the start of
its answer don't get mixed with the next question.
"""

import re
from dataclasses import dataclass

from app.calls.transcript import LabeledSegment
from app.filings.tenk import Paragraph, Section

# Business, Risk Factors, MD&A and market risk: where a company explains itself. The
# financial statements (Item 8) are mostly tables that MD&A already discusses.
INDEX_SECTIONS = ("1", "1A", "7", "7A")
MAX_WORDS = 250
# A call passage keeps going into the next speaker's turn while it's shorter than this
# (an operator handing over, "Thank you", a one-line question).
MIN_CALL_WORDS = 60

_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(\"“])")


@dataclass(frozen=True)
class Passage:
    position: int  # 1-based, in source order
    title: str  # where it is, shown with citations
    text: str
    # Filings: the section and the paragraphs the passage spans.
    section: str | None = None
    paragraph_ids: tuple[str, ...] = ()
    # Calls: the segments, time span and speakers it covers.
    first_segment: int | None = None
    last_segment: int | None = None
    start: float | None = None
    end: float | None = None
    speakers: tuple[str, ...] = ()

    @property
    def embedding_text(self) -> str:
        return embedding_text(self.title, self.text)


def embedding_text(title: str, text: str) -> str:
    """What gets embedded: the passage with its location, which carries context (the
    section, the risk-factor heading, the call) the text alone may not."""
    return f"{title}\n\n{text}"


def filing_passages(
    sections: list[Section], items: tuple[str, ...] = INDEX_SECTIONS, max_words: int = MAX_WORDS
) -> list[Passage]:
    passages: list[Passage] = []
    for section in sections:
        if section.id in items:
            passages += _section_passages(section, len(passages) + 1, max_words)
    return passages


def _section_passages(section: Section, first_position: int, max_words: int) -> list[Passage]:
    title = f"Item {section.id}. {section.title}"
    passages: list[Passage] = []
    current: list[tuple[Paragraph, str]] = []
    heading: str | None = None  # latest heading seen
    context: str | None = None  # heading in force when `current` started

    def close() -> None:
        nonlocal current, context
        # Trailing headings move on to the passage with the text they introduce.
        carry: list[tuple[Paragraph, str]] = []
        while current and current[-1][0].kind == "heading":
            carry.insert(0, current.pop())
        if current:
            passages.append(
                Passage(
                    position=first_position + len(passages),
                    title=f"{title} › {context}" if context else title,
                    text="\n\n".join(text for _, text in current),
                    section=section.id,
                    paragraph_ids=tuple(dict.fromkeys(p.id for p, _ in current)),
                )
            )
        current, context = carry, None

    for p in section.paragraphs:
        for piece in _split(p.text, max_words, lines=p.kind == "table"):
            size = sum(_words(t) for _, t in current)
            if current and (
                size + _words(piece) > max_words
                or (p.kind == "heading" and current[-1][0].kind != "heading")
            ):
                close()
            if not current:
                context = None if p.kind == "heading" else heading
            current.append((p, piece))
        if p.kind == "heading":
            heading = p.text
    close()
    return passages


def call_passages(
    segments: list[LabeledSegment],
    title: str,
    max_words: int = MAX_WORDS,
    min_words: int = MIN_CALL_WORDS,
) -> list[Passage]:
    """`title` names the call, e.g. "Q3 2020 earnings call"; the time span is added."""
    passages: list[Passage] = []
    current: list[LabeledSegment] = []

    def close() -> None:
        nonlocal current
        if not current:
            return
        text, _ = render_call_text(current)
        first, last = current[0], current[-1]
        passages.append(
            Passage(
                position=len(passages) + 1,
                title=f"{title}, {clock(first.start)}–{clock(last.end)}",
                text=text,
                first_segment=first.position,
                last_segment=last.position,
                start=first.start,
                end=last.end,
                speakers=tuple(dict.fromkeys(s.speaker for s in current if s.speaker)),
            )
        )
        current = []

    for s in segments:
        size = sum(_words(c.text) for c in current)
        new_turn = bool(current) and s.turn != current[-1].turn
        if current and (size + _words(s.text) > max_words or (new_turn and size >= min_words)):
            close()
        current.append(s)
    close()
    return passages


def render_call_text(segments: list[LabeledSegment]) -> tuple[str, list[int]]:
    """A run of segments as text, one paragraph per speaker turn ("Name: ..."), and the
    offset in that text where each segment starts."""
    text = ""
    offsets: list[int] = []
    for i, s in enumerate(segments):
        if i == 0 or s.turn != segments[i - 1].turn:
            if text:
                text += "\n\n"
            if s.speaker:
                text += f"{s.speaker}: "
        else:
            text += " "
        offsets.append(len(text))
        text += s.text
    return text, offsets


def clock(seconds: float) -> str:
    """42.5 -> "0:42", 3725 -> "1:02:05"."""
    total = int(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _words(text: str) -> int:
    return len(text.split())


def _split(text: str, max_words: int, *, lines: bool) -> list[str]:
    """A paragraph longer than `max_words`, cut at sentence ends (or rows, for tables)."""
    if _words(text) <= max_words:
        return [text]
    units = text.split("\n") if lines else _SENTENCE_END.split(text)
    pieces: list[str] = []
    current: list[str] = []
    for unit in units:
        if current and _words(" ".join(current + [unit])) > max_words:
            pieces.append(("\n" if lines else " ").join(current))
            current = []
        current.append(unit)
    if current:
        pieces.append(("\n" if lines else " ").join(current))
    return pieces
