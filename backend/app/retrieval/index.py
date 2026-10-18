"""Build the retrieval index: passages in Postgres (full-text search comes with them) and
their embeddings, one row per embedding model."""

import logging

import asyncpg

from app.calls.transcript import LabeledSegment
from app.embeddings import EmbeddingClient
from app.filings.tenk import Paragraph, Section
from app.retrieval.passages import Passage, call_passages, embedding_text, filing_passages

logger = logging.getLogger(__name__)


async def index_document(conn: asyncpg.Connection, document_id: int) -> int:
    """(Re)build a stored filing's passages. Returns how many there are."""
    doc = await conn.fetchrow("select ticker from documents where id = $1", document_id)
    if doc is None:
        raise LookupError(f"no document {document_id}")
    sections = await conn.fetch(
        "select item, title from sections where document_id = $1 order by position",
        document_id,
    )
    rows = await conn.fetch(
        "select id, section, position, kind, text from paragraphs"
        " where document_id = $1 order by section, position",
        document_id,
    )
    by_section: dict[str, list[Paragraph]] = {}
    for r in rows:
        by_section.setdefault(r["section"], []).append(
            Paragraph(r["id"], r["section"], r["position"], r["kind"], r["text"])
        )
    passages = filing_passages(
        [Section(s["item"], s["title"], by_section.get(s["item"], [])) for s in sections]
    )
    await _replace(conn, doc["ticker"], passages, document_id=document_id)
    return len(passages)


async def index_call(conn: asyncpg.Connection, call_id: int) -> int:
    """(Re)build a stored call's passages. Returns how many there are."""
    call = await conn.fetchrow(
        "select ticker, fiscal_year, quarter from calls where id = $1", call_id
    )
    if call is None:
        raise LookupError(f"no call {call_id}")
    segments = await load_segments(conn, call_id)
    title = f"Q{call['quarter']} {call['fiscal_year']} earnings call"
    passages = call_passages(segments, title)
    await _replace(conn, call["ticker"], passages, call_id=call_id)
    return len(passages)


async def load_segments(
    conn: asyncpg.Connection, call_id: int, first: int = 1, last: int | None = None
) -> list[LabeledSegment]:
    """A call's stored segments, optionally only positions first..last."""
    rows = await conn.fetch(
        "select position, turn, speaker, start_s, end_s, text from call_segments"
        " where call_id = $1 and position >= $2 and ($3::int is null or position <= $3)"
        " order by position",
        call_id,
        first,
        last,
    )
    return [
        LabeledSegment(r["position"], r["turn"], r["speaker"], r["start_s"], r["end_s"], r["text"])
        for r in rows
    ]


async def _replace(
    conn: asyncpg.Connection,
    ticker: str,
    passages: list[Passage],
    *,
    document_id: int | None = None,
    call_id: int | None = None,
) -> None:
    async with conn.transaction():
        if document_id is not None:
            await conn.execute("delete from passages where document_id = $1", document_id)
        else:
            await conn.execute("delete from passages where call_id = $1", call_id)
        await conn.copy_records_to_table(
            "passages",
            records=[
                (
                    ticker, document_id, call_id, p.position, p.title, p.text, p.section,
                    list(p.paragraph_ids) if document_id is not None else None,
                    p.first_segment, p.last_segment, p.start, p.end,
                    list(p.speakers) if call_id is not None else None,
                )
                for p in passages
            ],
            columns=[
                "ticker", "document_id", "call_id", "position", "title", "text", "section",
                "paragraph_ids", "first_segment", "last_segment", "start_s", "end_s",
                "speakers",
            ],
        )  # fmt: skip


async def embed_passages(
    conn: asyncpg.Connection, client: EmbeddingClient, *, ticker: str | None = None
) -> int:
    """Embed every passage that has no vector from `client`'s model yet. Returns how many
    were added. Vectors are saved as each batch arrives, so an interrupted run resumes."""
    rows = await conn.fetch(
        """
        select p.id, p.title, p.text from passages p
        where ($2::text is null or p.ticker = $2)
          and not exists (
              select 1 from passage_embeddings e where e.passage_id = p.id and e.model = $1)
        order by p.id
        """,
        client.key,
        ticker,
    )
    added = 0
    # Small groups, so progress is saved often when the provider's limits are tight.
    for i in range(0, len(rows), 50):
        group = rows[i : i + 50]
        texts = [embedding_text(r["title"], r["text"]) for r in group]
        vectors = await client.embed_documents(texts)
        await conn.executemany(
            "insert into passage_embeddings (passage_id, model, embedding)"
            " values ($1, $2, $3::vector) on conflict do nothing",
            [(r["id"], client.key, to_vector(v)) for r, v in zip(group, vectors, strict=True)],
        )
        added += len(group)
        logger.info("embedded %d/%d passages with %s", added, len(rows), client.key)
    return added


def to_vector(values: list[float]) -> str:
    """pgvector's text form, so no extra codec is needed."""
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"
