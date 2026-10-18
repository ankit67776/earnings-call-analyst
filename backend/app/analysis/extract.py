"""Map-reduce claim extraction over a 10-K section.

Map: each chunk goes to the LLM, which returns claims as strict JSON, each with the
paragraph ID and an exact quote. Ground: every quote is located in the source; claims
whose quote can't be found are rejected. Reduce: duplicate claims from different chunks
are merged into one finding with several citations (no LLM call).
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field

from app.analysis.chunking import DEFAULT_MAX_CHARS, Chunk, chunk_section
from app.analysis.grounding import MatchKind, locate
from app.filings.tenk import Paragraph, Section
from app.llm import BudgetExceededError, Completion, LLMClient, LLMError, RateLimitedError

logger = logging.getLogger(__name__)

PROMPT_VERSION = 1


class ClaimType(StrEnum):
    guidance = "guidance"
    metric = "metric"
    risk = "risk"
    event = "event"


class ExtractedClaim(BaseModel):
    type: ClaimType
    paragraph_id: str = Field(description="ID in brackets before the paragraph quoted")
    quote: str = Field(description="Exact contiguous text copied from that paragraph")
    claim: str = Field(description="One self-contained sentence stating the claim")


class Extraction(BaseModel):
    claims: list[ExtractedClaim]


SYSTEM_PROMPT = """\
You extract the key claims a company makes in its annual report (Form 10-K).

Claim types:
- guidance: forward-looking targets, plans or expectations (capital spending plans, \
production or growth targets, expected costs, outlook).
- metric: a reported financial or operating result with a number (revenue, margins, \
cash flow, production, customer concentration, headcount).
- risk: a specific risk or uncertainty the company says could hurt its business. One \
claim per distinct risk.
- event: a completed or announced corporate action (acquisition, divestiture, dividend \
or buyback change, restructuring, impairment, financing).

Rules:
- Every claim needs a quote copied exactly, character for character, from a single \
paragraph: the sentence or clause that states the claim, at most about 40 words. \
Don't paraphrase, shorten with "...", fix typos or join text from different paragraphs.
- paragraph_id is the ID in brackets before the paragraph the quote comes from.
- claim restates it in one plain sentence that makes sense on its own: name the \
subject, the period and the number or direction.
- Skip headings, generic boilerplate (forward-looking statement disclaimers, "we may \
be subject to litigation" without specifics), definitions and cross-references.
- Return an empty list when the text has no such claims.\
"""


@dataclass(frozen=True)
class Citation:
    paragraph_id: str
    # The model's quote, and how it matched the source (None: not found).
    quote: str
    match: MatchKind | None
    # Span of the matched text in the paragraph, and that text exactly as filed.
    start: int | None = None
    end: int | None = None
    source_text: str | None = None


@dataclass
class Finding:
    type: ClaimType
    claim: str
    citations: list[Citation]


@dataclass(frozen=True)
class ChunkResult:
    """What one LLM call saw and cost."""

    position: int
    paragraph_ids: list[str]
    context_heading: str | None
    claims: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cached: bool = False
    error: str | None = None


@dataclass
class SectionAnalysis:
    section: str
    model: str
    chunks: int
    findings: list[Finding]
    # Claims dropped because their quote couldn't be found in the source.
    rejected: list[Finding]
    chunk_results: list[ChunkResult] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    cached_calls: int = 0
    seconds: float = 0.0

    @property
    def failed_chunks(self) -> list[tuple[int, str]]:
        return [(c.position, c.error) for c in self.chunk_results if c.error]

    def match_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("exact", "normalized", "elided", "fuzzy"), 0)
        counts["unverified"] = len(self.rejected)
        for finding in self.findings:
            for c in finding.citations:
                counts[c.match or "unverified"] += 1
        return counts


def render_chunk(chunk: Chunk, company: str | None = None) -> str:
    lines = []
    if company:
        lines.append(f"Company: {company}")
    lines.append(f"Section: Item {chunk.section}. {chunk.section_title}")
    if chunk.context_heading:
        lines.append(f'(Continues under the heading "{chunk.context_heading}")')
    for p in chunk.paragraphs:
        lines.append(f"\n[{p.id}] {p.text}")
    return "\n".join(lines)


async def extract_chunk(
    client: LLMClient, chunk: Chunk, *, company: str | None = None, model: str | None = None
) -> tuple[list[ExtractedClaim], Completion]:
    parsed = await client.complete_json(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": render_chunk(chunk, company)},
        ],
        Extraction,
        model=model,
        max_tokens=3000,
    )
    return parsed.value.claims, parsed.completion


def ground(claim: ExtractedClaim, chunk: Chunk) -> Citation:
    """Locate the claim's quote: in the cited paragraph first, then (exact or normalized
    only) in the chunk's other paragraphs, in case the model cited the wrong ID."""
    by_id = {p.id: p for p in chunk.paragraphs}
    cited = by_id.get(claim.paragraph_id)
    candidates: list[tuple[Paragraph, bool]] = []
    if cited is not None:
        candidates.append((cited, True))
    candidates += [(p, False) for p in chunk.paragraphs if p is not cited]
    for paragraph, fuzzy in candidates:
        match = locate(claim.quote, paragraph.text, fuzzy=fuzzy)
        if match is not None:
            return Citation(
                paragraph_id=paragraph.id,
                quote=claim.quote,
                match=match.kind,
                start=match.start,
                end=match.end,
                source_text=paragraph.text[match.start : match.end],
            )
    return Citation(claim.paragraph_id, claim.quote, None)


def reduce_findings(findings: list[Finding]) -> list[Finding]:
    """Merge findings that state the same claim, keeping every distinct citation."""
    merged: list[Finding] = []
    for finding in findings:
        same = next((m for m in merged if _same_claim(m, finding)), None)
        if same is None:
            merged.append(Finding(finding.type, finding.claim, list(finding.citations)))
            continue
        for c in finding.citations:
            if not any(_overlaps(c, existing) for existing in same.citations):
                same.citations.append(c)
    return merged


async def analyze_section(
    client: LLMClient,
    section: Section,
    *,
    company: str | None = None,
    model: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_chunks: int | None = None,
    concurrency: int = 4,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> SectionAnalysis:
    """`on_progress(done, total)` is awaited before the first chunk and after each one."""
    started = time.perf_counter()
    chunks = chunk_section(section, max_chars=max_chars)[:max_chunks]
    gate = asyncio.Semaphore(concurrency)
    finished = 0
    if on_progress is not None:
        await on_progress(0, len(chunks))

    async def run(i: int, chunk: Chunk):
        nonlocal finished
        async with gate:
            try:
                result = await extract_chunk(client, chunk, company=company, model=model)
            except (BudgetExceededError, RateLimitedError):
                # Out of quota: the rest of the chunks would fail too, so stop the run.
                raise
            except LLMError as e:
                logger.warning("chunk %d of Item %s failed: %s", i, section.id, e)
                result = e
        # Outside the gate, so reporting doesn't hold up the next LLM call.
        finished += 1
        if on_progress is not None:
            await on_progress(finished, len(chunks))
        return result

    results = await asyncio.gather(*(run(i, c) for i, c in enumerate(chunks)))

    analysis = SectionAnalysis(
        section=section.id,
        model=model or client.default_model,
        chunks=len(chunks),
        findings=[],
        rejected=[],
    )
    grounded: list[Finding] = []
    for i, (chunk, result) in enumerate(zip(chunks, results, strict=True)):
        seen = ([p.id for p in chunk.paragraphs], chunk.context_heading)
        if isinstance(result, Exception):
            analysis.chunk_results.append(ChunkResult(i, *seen, error=str(result)))
            continue
        claims, completion = result
        analysis.chunk_results.append(
            ChunkResult(
                i,
                *seen,
                claims=len(claims),
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                latency_ms=completion.latency_ms,
                cached=completion.cached,
            )
        )
        analysis.llm_calls += 1
        analysis.cached_calls += completion.cached
        analysis.prompt_tokens += completion.prompt_tokens
        analysis.completion_tokens += completion.completion_tokens
        for claim in claims:
            finding = Finding(claim.type, claim.claim, [ground(claim, chunk)])
            (grounded if finding.citations[0].match else analysis.rejected).append(finding)

    analysis.findings = reduce_findings(grounded)
    analysis.seconds = time.perf_counter() - started
    return analysis


_WORD = re.compile(r"[a-z0-9][a-z0-9.,%$]*", re.I)
_NUMBER = re.compile(r"\d")
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "could", "for", "from", "has",
    "have", "in", "is", "its", "may", "of", "on", "or", "that", "the", "their", "to",
    "was", "were", "which", "will", "with",
}  # fmt: skip


def _terms(text: str) -> set[str]:
    words = (w.rstrip(".,").lower() for w in _WORD.findall(text))
    return {w for w in words if w and w not in _STOPWORDS}


def _same_claim(a: Finding, b: Finding) -> bool:
    if a.type != b.type:
        return False
    ta, tb = _terms(a.claim), _terms(b.claim)
    if not (ta and tb):
        return False
    # Claims with different numbers are different claims, however similar the wording.
    if {t for t in ta if _NUMBER.search(t)} != {t for t in tb if _NUMBER.search(t)}:
        return False
    similarity = len(ta & tb) / len(ta | tb)
    # One sentence can state several claims, so a shared quote alone isn't enough.
    shared_quote = any(_overlaps(x, y) for x in a.citations for y in b.citations)
    return similarity >= 0.75 or (shared_quote and similarity >= 0.4)


def _overlaps(a: Citation, b: Citation) -> bool:
    if a.paragraph_id != b.paragraph_id or a.start is None or b.start is None:
        return False
    return a.start < b.end and b.start < a.end
