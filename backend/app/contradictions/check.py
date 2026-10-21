"""Check what management said on a call against the company's 10-K.

Stage 1, candidates: each call claim is a query for hybrid search over the company's
10-K passages, and the top k are its candidates. Stage 2, verification: the verifier
model reads the claim's quote and its candidates side by side and judges each pair as a
contradiction, consistent or unrelated, quoting the 10-K text its verdict rests on.
That quote is located in the passage like every other quote in the app: a contradiction
whose 10-K quote can't be found there is never shown.
"""

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import Literal

import asyncpg
from pydantic import BaseModel, Field

from app.analysis.grounding import MatchKind, locate
from app.embeddings import EmbeddingClient
from app.llm import (
    BudgetExceededError,
    LLMClient,
    LLMError,
    Message,
    RateLimitedError,
    TruncatedOutputError,
)
from app.retrieval.search import Hit, search

logger = logging.getLogger(__name__)

PROMPT_VERSION = 1
# Candidates per claim: three passages keep a verification to about 2K tokens of input.
DEFAULT_K = 3

Verdict = Literal["contradiction", "consistent", "unrelated"]


class Judgment(BaseModel):
    source: str = Field(description="Label of the 10-K passage, e.g. F2")
    reason: str = Field(description="What each side says, and why that's the verdict")
    verdict: Verdict
    filing_quote: str = Field(
        description="Exact text from the passage the verdict rests on; empty if unrelated"
    )


class Judgments(BaseModel):
    judgments: list[Judgment]


SYSTEM_PROMPT = """\
You check what a company's executives said on an earnings call against the company's \
annual report (Form 10-K), as a careful analyst would. You get one claim from the call \
and a few numbered 10-K passages found by search. For each passage, decide how it bears \
on the claim:

- contradiction: both are about the same thing (the same measure, scope and period, or \
the same fact about the company) and they can't both be true: figures that differ by \
more than rounding, opposite directions, or one saying something is so and the other \
that it isn't.
- consistent: the passage states the same thing, or something that agrees with it.
- unrelated: the passage doesn't speak to what the claim states, or covers a different \
period, segment or measure.

Not contradictions:
- A forecast, target or plan, and a different actual result for that period or a later, \
revised target. Plans change; that's news, not a contradiction.
- A risk the 10-K says could happen, and executives sounding confident on the call.
- Figures for different periods or scopes: a quarter and the full year, one segment \
and the whole company, adjusted (non-GAAP) and reported figures.
- Rounding, different wording, or one side giving more detail.
Flag a contradiction only when an analyst shown the two quotes side by side would agree \
they can't both be true.

The call quote is what was said; the claim sentence summarizes it to help you read it. \
The transcript is a machine transcription, so a misspelled name is still the same name.

For each passage, give a short reason first (what each side says), then the verdict, \
and filing_quote: the clause of the passage the verdict rests on, copied exactly, \
character for character, at most about 40 words. Leave filing_quote empty when the \
passage is unrelated.\
"""


@dataclass(frozen=True)
class Claim:
    """The call side of a pair."""

    type: str
    claim: str
    speaker: str
    quote: str  # as said on the call


@dataclass(frozen=True)
class PairCheck:
    rank: int  # 1-based, in the claim's candidates
    hit: Hit
    verdict: Verdict | None  # None: the verifier returned nothing for this passage
    reason: str = ""
    filing_quote: str = ""  # as the model returned it
    # Where the quote was found in the passage, and that text as filed.
    match: MatchKind | None = None
    start: int | None = None
    end: int | None = None
    source_text: str | None = None
    paragraph_id: str | None = None  # filled in by check_call()

    @property
    def flagged(self) -> bool:
        """A contradiction whose 10-K quote checks out: what the app shows."""
        return self.verdict == "contradiction" and self.match is not None


@dataclass
class ClaimCheck:
    claim: Claim
    pairs: list[PairCheck] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_ms: float = 0.0
    cached: bool = False

    @property
    def contradictions(self) -> list[PairCheck]:
        return [p for p in self.pairs if p.flagged]


async def candidates(
    conn: asyncpg.Connection,
    embeddings: EmbeddingClient,
    claim: Claim,
    *,
    ticker: str,
    k: int = DEFAULT_K,
) -> list[Hit]:
    """Stage 1: the 10-K passages most likely to speak to the claim."""
    return await search(
        conn, claim.claim, ticker=ticker, embeddings=embeddings, k=k, source="filing"
    )


def render(
    claim: Claim, hits: list[Hit], *, company: str, call: str, filings: dict[int, str]
) -> str:
    passages = "\n\n".join(
        f"[F{n}] {filings.get(h.document_id or 0, '10-K')} · {h.title}\n{h.text}"
        for n, h in enumerate(hits, 1)
    )
    return (
        f"Company: {company}\n\n"
        f"On the {call}, {claim.speaker or 'an executive'} said ({claim.type}):\n"
        f'Quote: "{claim.quote}"\n'
        f"Claim: {claim.claim}\n\n"
        f"10-K passages:\n\n{passages}"
    )


async def verify(
    llm: LLMClient,
    claim: Claim,
    hits: list[Hit],
    *,
    company: str,
    call: str,
    filings: dict[int, str],
    model: str | None = None,
) -> ClaimCheck:
    """Stage 2: judge the claim against each candidate and locate every 10-K quote.

    `call` names the call ("Q3 2020 earnings call"); `filings` describes each 10-K the
    candidates come from, by document ID ("10-K for fiscal 2020, filed 2021-02-16")."""
    if not hits:
        return ClaimCheck(claim)
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": render(claim, hits, company=company, call=call, filings=filings),
        },
    ]
    try:
        parsed = await llm.complete_json(
            messages, Judgments, model=model, max_tokens=2500, reasoning_effort="medium"
        )
    except TruncatedOutputError:
        # Now and then the model reasons at length before answering; give it room once.
        parsed = await llm.complete_json(
            messages, Judgments, model=model, max_tokens=5000, reasoning_effort="medium"
        )
    completion = parsed.completion
    by_label: dict[str, Judgment] = {}
    for j in parsed.value.judgments:
        by_label.setdefault(j.source.strip().strip("[]").upper(), j)
    result = ClaimCheck(
        claim,
        model=completion.model,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        llm_ms=completion.latency_ms,
        cached=completion.cached,
    )
    for rank, hit in enumerate(hits, 1):
        j = by_label.get(f"F{rank}")
        if j is None:
            result.pairs.append(PairCheck(rank, hit, None))
            continue
        pair = PairCheck(rank, hit, j.verdict, j.reason, j.filing_quote)
        if j.verdict != "unrelated" and (match := locate(j.filing_quote, hit.text)) is not None:
            pair = replace(
                pair,
                match=match.kind,
                start=match.start,
                end=match.end,
                source_text=hit.text[match.start : match.end],
            )
        result.pairs.append(pair)
    return result


def describe_filing(row: asyncpg.Record | dict) -> str:
    return f"{row['form']} for fiscal {row['fiscal_year']}, filed {row['filed']}"


async def check_call(
    conn: asyncpg.Connection,
    llm: LLMClient,
    embeddings: EmbeddingClient,
    call_id: int,
    *,
    k: int = DEFAULT_K,
    model: str | None = None,
    concurrency: int = 2,
) -> list[ClaimCheck]:
    """Check every stored claim from a call against the company's 10-K, saving each
    claim's verdicts as they come in. A claim whose verification fails is logged and
    left unchecked; running out of quota stops the run (what's done is cached and saved,
    so a re-run picks up where it stopped)."""
    call = await conn.fetchrow(
        "select ticker, company, fiscal_year, quarter from calls where id = $1", call_id
    )
    if call is None:
        raise LookupError(f"no call {call_id}")
    rows = await conn.fetch(
        "select id, type, claim, speaker, quote from call_claims"
        " where call_id = $1 order by position",
        call_id,
    )
    if not rows:
        raise LookupError(f"call {call_id} has no claims; run python -m app.contradictions claims")
    claims = [(r["id"], Claim(r["type"], r["claim"], r["speaker"], r["quote"])) for r in rows]

    # One embedding request for every claim, rather than one per search.
    await embeddings.embed([c.claim for _, c in claims], "query")
    hits = [await candidates(conn, embeddings, c, ticker=call["ticker"], k=k) for _, c in claims]
    documents = {h.document_id for found in hits for h in found}
    filings = {
        r["id"]: describe_filing(r)
        for r in await conn.fetch(
            "select id, form, fiscal_year, filed from documents where id = any($1::bigint[])",
            list(documents),
        )
    }
    name = f"Q{call['quarter']} {call['fiscal_year']} earnings call"
    gate = asyncio.Semaphore(concurrency)
    # One connection: database work takes turns while verifications run side by side.
    db_lock = asyncio.Lock()
    done = 0
    stopped: LLMError | None = None

    async def one(claim_id: int, claim: Claim, found: list[Hit]) -> ClaimCheck | None:
        nonlocal done, stopped
        async with gate:
            if stopped is not None:
                return None
            try:
                result = await verify(
                    llm, claim, found, company=call["company"], call=name, filings=filings,
                    model=model,
                )  # fmt: skip
            except (BudgetExceededError, RateLimitedError) as e:
                # Out of quota: the rest would fail too. Start no more, but let the
                # verdicts already in hand be saved before stopping.
                stopped = e
                return None
            except LLMError as e:
                logger.warning("claim %d failed: %s", claim_id, e)
                return None
        async with db_lock:
            result.pairs = [await _with_paragraph(conn, p) for p in result.pairs]
            await save_checks(conn, claim_id, result)
            done += 1
        logger.info("checked %d/%d claims from call %d", done, len(claims), call_id)
        return result

    results = await asyncio.gather(
        *(one(i, c, found) for (i, c), found in zip(claims, hits, strict=True))
    )
    if stopped is not None:
        raise stopped
    return [r for r in results if r is not None]


async def _with_paragraph(conn: asyncpg.Connection, pair: PairCheck) -> PairCheck:
    """Point a located 10-K quote at the filing paragraph it's in."""
    hit = pair.hit
    if pair.source_text is None or hit.document_id is None:
        return pair
    rows = await conn.fetch(
        "select id, text from paragraphs where document_id = $1 and id = any($2::text[])",
        hit.document_id,
        hit.paragraph_ids or [],
    )
    for r in rows:
        if locate(pair.source_text, r["text"], fuzzy=False):
            return replace(pair, paragraph_id=r["id"])
    return pair


async def save_checks(conn: asyncpg.Connection, claim_id: int, result: ClaimCheck) -> None:
    """Replace a claim's verdicts."""
    async with conn.transaction():
        await conn.execute("delete from claim_checks where claim_id = $1", claim_id)
        await conn.copy_records_to_table(
            "claim_checks",
            records=[
                (claim_id, p.rank, p.hit.passage_id, p.verdict, p.reason or None,
                 p.filing_quote or None, p.match, p.start, p.end, p.source_text,
                 p.paragraph_id, result.model, PROMPT_VERSION)
                for p in result.pairs
            ],
            columns=[
                "claim_id", "rank", "passage_id", "verdict", "reason", "filing_quote", "match",
                "start_offset", "end_offset", "source_text", "paragraph_id", "model",
                "prompt_version",
            ],
        )  # fmt: skip
