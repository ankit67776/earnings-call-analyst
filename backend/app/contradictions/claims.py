"""Claims management makes on an earnings call, each with its quote, speaker and time.

The same map, ground and reduce as claim extraction from a 10-K, run over the call's
passages: each chunk is a few consecutive passages with their speaker turns, and the
model takes claims only from the company's executives. Every quote is located in its
passage, which says who said it and when.

The model doesn't always tell an analyst's assertive question from an answer, so a
first, small LLM call works out who speaks for the company (from the call's opening and
how each speaker is introduced), and claims by anyone else are dropped.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field

import asyncpg
from pydantic import BaseModel, Field

from app.analysis.chunking import Chunk, chunk_section
from app.analysis.extract import ClaimType, ExtractionPrompt, SectionAnalysis, analyze_chunks
from app.analysis.grounding import MatchKind
from app.calls.transcript import LabeledSegment
from app.filings.tenk import Paragraph, Section
from app.llm import Completion, LLMClient
from app.retrieval.index import load_segments
from app.retrieval.passages import render_call_text

logger = logging.getLogger(__name__)

PROMPT_VERSION = 1
# About 1,300 words of transcript per LLM call: a stretch of prepared remarks, or a few
# questions with their answers.
MAX_CHARS = 8_000

SYSTEM_PROMPT = """\
You extract the claims a company's executives make on its earnings call: statements of \
fact, results and plans that an analyst could check against the company's annual \
report (Form 10-K).

Claim types:
- guidance: forward-looking targets, plans or expectations (outlook, spending plans, \
production or growth targets, expected costs, the timing of a deal).
- metric: a reported financial or operating result with a number (revenue, margins, \
cash, debt, production, market share, customer counts, headcount).
- risk: a specific risk, problem or uncertainty they say affects or could affect the \
business.
- event: a completed or announced corporate action (acquisition, divestiture, dividend \
or buyback change, restructuring, impairment, financing, a plant opening or closing).

Rules:
- Only what the company's executives say. Skip the operator, analysts' questions and \
the safe-harbor statement. An executive agreeing with an analyst ("yes, that's right") \
is not a claim unless they state the content themselves.
- Every claim needs a quote copied exactly, character for character, from a single \
passage: the sentence or clause that states the claim, at most about 40 words, without \
the speaker's name. The transcript is a machine transcription: quote it as written, \
errors and all. Don't paraphrase, shorten with "...", or join text from different places.
- paragraph_id is the ID in brackets before the passage the quote comes from.
- claim restates it in one plain sentence that makes sense on its own: name the \
company as the Company line spells it (the transcript may misspell it), the period and \
the number or direction.
- Skip greetings, thanks, praise for employees, vague optimism with nothing to check \
("we're excited about the opportunities ahead") and repeats of a claim already made.
- Return an empty list when the text has no such claims.\
"""


def render_call_chunk(chunk: Chunk, company: str | None = None) -> str:
    lines = []
    if company:
        lines.append(f"Company: {company}")
    lines.append(f"Transcript of the {chunk.section_title}, consecutive passages:")
    for p in chunk.paragraphs:
        lines.append(f"\n[{p.id}] {p.text}")
    return "\n".join(lines)


CALL_PROMPT = ExtractionPrompt(SYSTEM_PROMPT, render_call_chunk, max_tokens=4000)


class CompanySpeakers(BaseModel):
    speakers: list[int] = Field(description="Numbers of the company's speakers")


SPEAKERS_PROMPT = """\
You work out who speaks for the company on an earnings call. You get the opening of \
the transcript, then each speaker, numbered, with how much they say, the words just \
before they first speak and how they begin. Return the numbers of the company's \
executives and investor-relations staff. Leave out the operator and the analysts who \
ask questions.

Clues: the investor-relations host usually names the executives on the call; the \
operator introduces each analyst ("our next question comes from ... with ..."); \
executives answer the questions and usually speak the most. The transcript is a machine \
transcription, so a name in the text may be spelled differently from the speaker's \
label.\
"""

# How much of the call's opening to show: the welcome and the investor-relations
# introduction, which names the executives on the call.
OPENING_WORDS = 600
LEAD_IN_WORDS = 40
FIRST_WORDS = 25


def speaker_labels(segments: list[LabeledSegment]) -> list[str]:
    """Each speaker once, in order of first appearance: speaker n is labels[n - 1]."""
    return list(dict.fromkeys(s.speaker for s in segments if s.speaker))


def render_speakers(company: str, segments: list[LabeledSegment]) -> str:
    words = Counter[str]()
    first: dict[str, int] = {}
    for i, s in enumerate(segments):
        words[s.speaker] += len(s.text.split())
        first.setdefault(s.speaker, i)
    opening: list[LabeledSegment] = []
    for s in segments:
        if sum(len(t.text.split()) for t in opening) >= OPENING_WORDS:
            break
        opening.append(s)
    lines = [f"Company: {company}", "", "Opening of the call:", render_call_text(opening)[0]]
    lines += ["", "Speakers:"]
    for n, label in enumerate(speaker_labels(segments), 1):
        i = first[label]
        before = " ".join(t.text for t in segments[max(0, i - 6) : i]).split()[-LEAD_IN_WORDS:]
        turn = " ".join(t.text for t in segments[i : i + 6] if t.turn == segments[i].turn)
        lead_in = f'"... {" ".join(before)}"' if before else "nothing, they open the call"
        lines.append(
            f"[{n}] {label}, {words[label]:,} words in all.\n"
            f"  Just before they first speak: {lead_in}\n"
            f'  They begin: "{" ".join(turn.split()[:FIRST_WORDS])} ..."'
        )
    return "\n".join(lines)


async def company_speakers(
    llm: LLMClient, company: str, segments: list[LabeledSegment], *, model: str | None = None
) -> tuple[set[str], Completion]:
    """The speaker labels of the company's executives and investor-relations staff."""
    parsed = await llm.complete_json(
        [
            {"role": "system", "content": SPEAKERS_PROMPT},
            {"role": "user", "content": render_speakers(company, segments)},
        ],
        CompanySpeakers,
        model=model,
        max_tokens=4000,
        # At low effort the model missed executives whose names the transcript misspells.
        reasoning_effort="medium",
    )
    labels = speaker_labels(segments)
    chosen = {labels[n - 1] for n in parsed.value.speakers if 1 <= n <= len(labels)}
    return chosen, parsed.completion


@dataclass(frozen=True)
class CallClaim:
    type: ClaimType
    claim: str
    speaker: str
    quote: str  # the transcript's text, as found
    match: MatchKind
    at: float  # seconds into the call where the quote starts


@dataclass
class CallClaims:
    call_id: int
    claims: list[CallClaim]
    # What the extraction saw and cost, chunk by chunk.
    analysis: SectionAnalysis
    # Who speaks for the company, and the claims dropped for coming from someone else.
    executives: set[str] = field(default_factory=set)
    dropped: list[CallClaim] = field(default_factory=list)
    speaker_tokens: int = 0


async def extract_call_claims(
    conn: asyncpg.Connection,
    llm: LLMClient,
    call_id: int,
    *,
    model: str | None = None,
    max_chars: int = MAX_CHARS,
) -> CallClaims:
    call = await conn.fetchrow(
        "select company, fiscal_year, quarter from calls where id = $1", call_id
    )
    if call is None:
        raise LookupError(f"no call {call_id}")
    rows = await conn.fetch(
        "select position, text, first_segment, last_segment from passages"
        " where call_id = $1 order by position",
        call_id,
    )
    if not rows:
        raise LookupError(f"call {call_id} has no passages; run python -m app.retrieval index")
    title = f"Q{call['quarter']} {call['fiscal_year']} earnings call"
    paragraphs = [
        Paragraph(f"P{r['position']}", "call", r["position"], "text", r["text"]) for r in rows
    ]
    chunks = chunk_section(Section("call", title, paragraphs), max_chars=max_chars)
    analysis = await analyze_chunks(
        llm, "call", chunks, company=call["company"], model=model, prompt=CALL_PROMPT
    )

    segments = await load_segments(conn, call_id)
    executives, completion = await company_speakers(llm, call["company"], segments, model=model)
    if not executives:
        logger.warning("call %d: no company speakers identified; keeping every claim", call_id)
    by_position = {r["position"]: r for r in rows}
    claims = []
    for finding in analysis.findings:
        # Citations come in transcript order; the first is where the claim is first made.
        c = finding.citations[0]
        passage = by_position[int(c.paragraph_id.removeprefix("P"))]
        first, last = passage["first_segment"], passage["last_segment"]
        run = [s for s in segments if first <= s.position <= last]
        text, offsets = render_call_text(run)
        if text != passage["text"] or not run:
            raise RuntimeError(f"call {call_id} passages are stale; re-run the retrieval index")
        # The segment the quote starts in.
        i = max((n for n, o in enumerate(offsets) if o <= c.start), default=0)
        claims.append(
            CallClaim(
                type=finding.type,
                claim=finding.claim,
                speaker=run[i].speaker,
                quote=c.source_text,
                match=c.match,
                at=run[i].start,
            )
        )
    claims.sort(key=lambda c: c.at)
    kept = [c for c in claims if not executives or c.speaker in executives]
    return CallClaims(
        call_id,
        kept,
        analysis,
        executives=executives,
        dropped=[c for c in claims if c not in kept],
        speaker_tokens=completion.total_tokens,
    )


async def save_call_claims(conn: asyncpg.Connection, result: CallClaims) -> None:
    """Replace a call's claims (and with them, their checks against the 10-K)."""
    async with conn.transaction():
        await conn.execute("delete from call_claims where call_id = $1", result.call_id)
        await conn.copy_records_to_table(
            "call_claims",
            records=[
                (result.call_id, n, str(c.type), c.claim, c.speaker, c.quote, c.match, c.at,
                 result.analysis.model, PROMPT_VERSION)
                for n, c in enumerate(result.claims, 1)
            ],
            columns=[
                "call_id", "position", "type", "claim", "speaker", "quote", "match", "at_s",
                "model", "prompt_version",
            ],
        )  # fmt: skip
