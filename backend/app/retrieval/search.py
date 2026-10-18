"""Hybrid retrieval: Postgres full-text search and pgvector similarity, fused by rank.

Keyword search finds exact terms (a product name, "Willow", "EPS"), embeddings find
paraphrases ("how did the pandemic hit sales" vs "COVID-19 impact on net sales"). Each
returns its top candidates and reciprocal rank fusion combines the two lists: a passage's
score is the sum of weight / (60 + rank) over the lists it appears in.

The keyword list is weighted at a quarter. On the retrieval eval's paraphrased questions,
keyword search is far weaker than embeddings and equal weights cost about ten points of
recall@1; at a quarter, hybrid is level with vector search alone while still lifting
passages that share the query's rare terms. The weight was picked on one annotator's
questions and checked on the other's.
"""

from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from app.embeddings import EmbeddingClient
from app.retrieval.index import to_vector

Mode = Literal["fts", "vector", "hybrid"]
Source = Literal["filing", "call"]

RRF_K = 60
CANDIDATES = 50
FTS_WEIGHT = 0.25


@dataclass(frozen=True)
class Hit:
    passage_id: int
    ticker: str
    source: Source
    document_id: int | None
    call_id: int | None
    title: str
    text: str
    section: str | None
    paragraph_ids: list[str] | None
    first_segment: int | None
    last_segment: int | None
    start: float | None
    end: float | None
    speakers: list[str] | None
    score: float
    # 1-based rank in each list it came from; None if it wasn't a candidate there.
    fts_rank: int | None = None
    vector_rank: int | None = None


_COLUMNS = """
    p.id, p.ticker, p.document_id, p.call_id, p.title, p.text, p.section, p.paragraph_ids,
    p.first_segment, p.last_segment, p.start_s, p.end_s, p.speakers
"""

# Any of the query's words, not all: questions are long and passages rarely hold every word.
_FTS = f"""
    with q as (
        select replace(plainto_tsquery('english', $1)::text, ' & ', ' | ')::tsquery as query
    )
    select {_COLUMNS}, ts_rank(p.tsv, q.query, 1) as score
    from passages p, q
    where p.ticker = $2 and p.tsv @@ q.query
      and ($3::text is null or (p.call_id is not null) = ($3 = 'call'))
    order by score desc, p.id
    limit $4
"""

_VECTOR = f"""
    select {_COLUMNS}, 1 - (e.embedding <=> $1::vector) as score
    from passage_embeddings e join passages p on p.id = e.passage_id
    where e.model = $2 and p.ticker = $3
      and ($4::text is null or (p.call_id is not null) = ($4 = 'call'))
    order by e.embedding <=> $1::vector, p.id
    limit $5
"""


async def search(
    conn: asyncpg.Connection,
    query: str,
    *,
    ticker: str,
    embeddings: EmbeddingClient | None = None,
    mode: Mode = "hybrid",
    k: int = 8,
    source: Source | None = None,
    candidates: int = CANDIDATES,
    fts_weight: float = FTS_WEIGHT,
) -> list[Hit]:
    """The `k` best passages for `query` from one company's filings and calls."""
    if mode != "fts" and embeddings is None:
        raise ValueError(f"{mode} search needs an embedding client")
    fts: list[asyncpg.Record] = []
    vector: list[asyncpg.Record] = []
    if mode in ("fts", "hybrid"):
        fts = await conn.fetch(_FTS, query, ticker, source, candidates)
    if mode in ("vector", "hybrid"):
        assert embeddings is not None
        q = to_vector(await embeddings.embed_query(query))
        vector = await conn.fetch(_VECTOR, q, embeddings.key, ticker, source, candidates)

    if mode == "fts":
        return [_hit(r, r["score"], fts_rank=n) for n, r in enumerate(fts[:k], 1)]
    if mode == "vector":
        return [_hit(r, r["score"], vector_rank=n) for n, r in enumerate(vector[:k], 1)]
    return fuse(fts, vector, fts_weight=fts_weight)[:k]


def fuse(fts: list[Any], vector: list[Any], *, fts_weight: float = FTS_WEIGHT) -> list[Hit]:
    """Weighted reciprocal rank fusion of two ranked lists of passage rows."""
    rows: dict[int, Any] = {}
    ranks: dict[int, dict[str, int]] = {}
    for name, results in (("fts_rank", fts), ("vector_rank", vector)):
        for n, r in enumerate(results, 1):
            rows.setdefault(r["id"], r)
            ranks.setdefault(r["id"], {})[name] = n
    weights = {"fts_rank": fts_weight, "vector_rank": 1.0}
    scored = sorted(
        ((sum(weights[k] / (RRF_K + n) for k, n in ranks[i].items()), i) for i in rows),
        key=lambda pair: (-pair[0], pair[1]),
    )
    return [_hit(rows[i], score, **ranks[i]) for score, i in scored]


def _hit(row: Any, score: float, **ranks: int) -> Hit:
    return Hit(
        passage_id=row["id"],
        ticker=row["ticker"],
        source="call" if row["call_id"] is not None else "filing",
        document_id=row["document_id"],
        call_id=row["call_id"],
        title=row["title"],
        text=row["text"],
        section=row["section"],
        paragraph_ids=row["paragraph_ids"],
        first_segment=row["first_segment"],
        last_segment=row["last_segment"],
        start=row["start_s"],
        end=row["end_s"],
        speakers=row["speakers"],
        score=float(score),
        **ranks,
    )
