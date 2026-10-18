"""Answer questions about a company from its filings and calls, with a verified citation
for every sentence, or refuse.

Retrieve: hybrid search over the company's passages. Answer: the LLM sees the passages
as numbered sources and returns strict JSON, sentence by sentence, each with the exact
quotes that support it. Verify: every quote is located in the source it names (the same
grounding check as claim extraction). Quotes that aren't found are dropped, a sentence
left with no verified quote is dropped, and an answer left with no sentences becomes a
refusal. So the chat never shows a sentence it can't point to in a source.
"""

import time
from dataclasses import dataclass, field, replace

import asyncpg
from pydantic import BaseModel, Field

from app.analysis.grounding import MatchKind, locate
from app.embeddings import EmbeddingClient
from app.llm import LLMClient, Message, TruncatedOutputError
from app.retrieval.index import load_segments
from app.retrieval.passages import render_call_text
from app.retrieval.search import Hit, search

PROMPT_VERSION = 1
# Passages shown to the model: ~2K tokens, which leaves room in the free tier's 8K
# tokens a minute for the prompt, reasoning and the answer.
DEFAULT_K = 6

REFUSAL = "The filings and calls I can search don't answer that."


class Support(BaseModel):
    source: str = Field(description="Label of the source the quote is from, e.g. S2")
    quote: str = Field(description="Exact text copied from that source")


class Sentence(BaseModel):
    text: str = Field(description="One sentence of the answer")
    support: list[Support] = Field(description="Quotes that state what the sentence says")


class Response(BaseModel):
    answerable: bool = Field(description="False if the sources don't contain the answer")
    sentences: list[Sentence]


SYSTEM_PROMPT = """\
You answer an analyst's questions about {company} using only the numbered sources \
below: passages from the company's annual report (Form 10-K) and transcripts of its \
earnings calls.

Rules:
- Use only what the sources state. No outside knowledge, no guessing, and no numbers \
the sources don't give (don't compute growth rates or totals yourself).
- Every sentence of your answer needs support: one or more quotes, each copied exactly, \
character for character, from one source, with that source's label. Quote the clause \
that states the fact, at most about 40 words. Don't fix typos or join text from \
different places.
- If the sources don't answer the question, set answerable to false and return no \
sentences. Don't answer a different question than the one asked, and don't accept a \
premise of the question that the sources don't support.
- Lead with the direct answer. Use one to four sentences. Write in the third person \
(name the company, or "it"), not in the filing's "we" and "our".
- Say who said it when the source is a call ("Michele Buck said..."). Call \
transcripts are machine transcriptions and may contain errors; quote them as written.\
"""


@dataclass(frozen=True)
class Citation:
    label: str  # S1, S2...
    passage_id: int
    quote: str  # as the model returned it
    match: MatchKind
    # Span in the passage text, and that text exactly as it appears there.
    start: int
    end: int
    source_text: str
    # Where it points: a time in the call, or a paragraph of the filing (filled in by ask()).
    at: float | None = None
    paragraph_id: str | None = None


@dataclass(frozen=True)
class AnswerSentence:
    text: str
    citations: list[Citation]


@dataclass
class ChatAnswer:
    question: str
    answered: bool
    sentences: list[AnswerSentence]
    sources: list[Hit]
    # The model said it could answer, but no sentence survived verification.
    unsupported: bool = False
    # What verification removed: sentences with no quote found, and quotes not found.
    dropped_sentences: list[str] = field(default_factory=list)
    dropped_quotes: int = 0
    quotes: int = 0
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_ms: float = 0.0
    retrieval_ms: float = 0.0
    cached: bool = False

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.sentences) if self.answered else REFUSAL


def render_sources(hits: list[Hit]) -> str:
    blocks = []
    for n, h in enumerate(hits, 1):
        kind = "Earnings call" if h.source == "call" else "10-K"
        blocks.append(f"[S{n}] {kind}: {h.title}\n{h.text}")
    return "\n\n".join(blocks)


async def answer(
    llm: LLMClient, question: str, hits: list[Hit], *, company: str, model: str | None = None
) -> ChatAnswer:
    """Answer from passages already retrieved, verifying every quote."""
    if not hits:
        return ChatAnswer(question, answered=False, sentences=[], sources=[])
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(company=company)},
        {
            "role": "user",
            "content": f"Sources:\n\n{render_sources(hits)}\n\nQuestion: {question}",
        },
    ]
    try:
        parsed = await llm.complete_json(
            messages, Response, model=model, max_tokens=2000, reasoning_effort="medium"
        )
    except TruncatedOutputError:
        # Now and then the model reasons at length before answering; give it room once.
        parsed = await llm.complete_json(
            messages, Response, model=model, max_tokens=4000, reasoning_effort="medium"
        )
    response, completion = parsed.value, parsed.completion
    result = ChatAnswer(
        question,
        answered=False,
        sentences=[],
        sources=hits,
        model=completion.model,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        llm_ms=completion.latency_ms,
        cached=completion.cached,
    )
    if not response.answerable:
        return result
    labels = {f"S{n}": h for n, h in enumerate(hits, 1)}
    for sentence in response.sentences:
        citations = []
        for s in sentence.support:
            result.quotes += 1
            citation = verify(s, labels)
            if citation is None:
                result.dropped_quotes += 1
            elif not any(c.passage_id == citation.passage_id and c.start == citation.start
                         for c in citations):  # fmt: skip
                citations.append(citation)
        if citations:
            result.sentences.append(AnswerSentence(sentence.text, citations))
        else:
            result.dropped_sentences.append(sentence.text)
    result.answered = bool(result.sentences)
    result.unsupported = not result.answered
    return result


def verify(support: Support, labels: dict[str, Hit]) -> Citation | None:
    """Locate a quote in the source it names; failing that (exact or normalized only) in
    another source, in case the model gave the wrong label."""
    label = support.source.strip().strip("[]").upper()
    cited = labels.get(label)
    candidates = [(label, cited, True)] if cited else []
    candidates += [(k, h, False) for k, h in labels.items() if h is not cited]
    for key, hit, fuzzy in candidates:
        match = locate(support.quote, hit.text, fuzzy=fuzzy)
        if match is not None:
            return Citation(
                label=key,
                passage_id=hit.passage_id,
                quote=support.quote,
                match=match.kind,
                start=match.start,
                end=match.end,
                source_text=hit.text[match.start : match.end],
            )
    return None


async def ask(
    conn: asyncpg.Connection,
    llm: LLMClient,
    embeddings: EmbeddingClient,
    *,
    ticker: str,
    question: str,
    company: str,
    k: int = DEFAULT_K,
    model: str | None = None,
) -> ChatAnswer:
    """Retrieve, answer and verify; then point each citation at its call timestamp or
    filing paragraph."""
    started = time.perf_counter()
    hits = await search(conn, question, ticker=ticker, embeddings=embeddings, k=k)
    retrieval_ms = (time.perf_counter() - started) * 1000
    result = await answer(llm, question, hits, company=company, model=model)
    result.retrieval_ms = retrieval_ms
    by_id = {h.passage_id: h for h in hits}
    for i, sentence in enumerate(result.sentences):
        located = [await _locate(conn, by_id[c.passage_id], c) for c in sentence.citations]
        result.sentences[i] = AnswerSentence(sentence.text, located)
    return result


async def _locate(conn: asyncpg.Connection, hit: Hit, citation: Citation) -> Citation:
    if hit.source == "call":
        segments = await load_segments(conn, hit.call_id, hit.first_segment, hit.last_segment)
        text, offsets = render_call_text(segments)
        if text != hit.text or not segments:
            return replace(citation, at=hit.start)
        # The segment the quote starts in.
        i = max((n for n, o in enumerate(offsets) if o <= citation.start), default=0)
        return replace(citation, at=segments[i].start)
    rows = await conn.fetch(
        "select id, text from paragraphs where document_id = $1 and id = any($2::text[])",
        hit.document_id,
        hit.paragraph_ids or [],
    )
    for r in rows:
        if locate(citation.source_text, r["text"], fuzzy=False):
            return replace(citation, paragraph_id=r["id"])
    return citation


def to_json(result: ChatAnswer) -> dict:
    """The API's shape for an answer: sentences with citations, and the passages read."""
    by_id = {h.passage_id: h for h in result.sources}

    def citation(c: Citation) -> dict:
        hit = by_id[c.passage_id]
        return {
            "label": c.label,
            "passage_id": c.passage_id,
            "source": hit.source,
            "title": hit.title,
            "quote": c.source_text,
            # Where the quote sits in the passage's text.
            "start": c.start,
            "end": c.end,
            "match": c.match,
            "document_id": hit.document_id,
            "section": hit.section,
            "paragraph_id": c.paragraph_id,
            "call_id": hit.call_id,
            "at": c.at,
        }

    return {
        "question": result.question,
        "answered": result.answered,
        "text": result.text,
        "sentences": [
            {"text": s.text, "citations": [citation(c) for c in s.citations]}
            for s in result.sentences
        ],
        "sources": [
            {
                "label": f"S{n}",
                "passage_id": h.passage_id,
                "source": h.source,
                "title": h.title,
                "text": h.text,
                "document_id": h.document_id,
                "section": h.section,
                "call_id": h.call_id,
                "start": h.start,
                "end": h.end,
                "speakers": h.speakers,
            }
            for n, h in enumerate(result.sources, 1)
        ],
        "verification": {
            "quotes": result.quotes,
            "dropped_quotes": result.dropped_quotes,
            "dropped_sentences": result.dropped_sentences,
            "unsupported": result.unsupported,
        },
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "llm_ms": round(result.llm_ms),
        "retrieval_ms": round(result.retrieval_ms),
        "cached": result.cached,
    }
