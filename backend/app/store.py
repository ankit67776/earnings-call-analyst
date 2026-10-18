"""Saving filings and analysis results to Postgres."""

from datetime import date

import asyncpg

from app.analysis.extract import PROMPT_VERSION, SectionAnalysis
from app.filings.edgar import FilingRef
from app.filings.tenk import TenK


async def save_filing(conn: asyncpg.Connection, ref: FilingRef, doc: TenK) -> int:
    """Insert or refresh a filing and its parsed text. Returns the document ID.

    Re-saving replaces the sections and paragraphs (e.g. after a parser change).
    Paragraph IDs are content-based, so existing citations keep pointing at the same text.
    """
    async with conn.transaction():
        document_id = await conn.fetchval(
            """
            insert into documents
                (ticker, company, form, fiscal_year, accession, period, filed, url)
            values ($1, $2, $3, $4, $5, $6, $7, $8)
            on conflict (accession) do update set
                ticker = excluded.ticker, company = excluded.company, url = excluded.url
            returning id
            """,
            ref.ticker,
            ref.company,
            ref.form,
            ref.fiscal_year,
            ref.accession,
            date.fromisoformat(ref.period),
            date.fromisoformat(ref.filed),
            ref.url,
        )
        await conn.execute("delete from sections where document_id = $1", document_id)
        await conn.copy_records_to_table(
            "sections",
            records=[(document_id, s.id, s.title, n) for n, s in enumerate(doc.sections, start=1)],
            columns=["document_id", "item", "title", "position"],
        )
        await conn.copy_records_to_table(
            "paragraphs",
            records=[
                (document_id, p.id, p.section, p.index, p.kind, p.text)
                for s in doc.sections
                for p in s.paragraphs
            ],
            columns=["document_id", "id", "section", "position", "kind", "text"],
        )
    return document_id


async def save_analysis(
    conn: asyncpg.Connection, document_id: int, analysis: SectionAnalysis
) -> int:
    """Store one section's run: its chunks, findings (verified and rejected) and citations."""
    async with conn.transaction():
        run_id = await conn.fetchval(
            """
            insert into analysis_runs
                (document_id, section, model, prompt_version, chunks, llm_calls,
                 cached_calls, prompt_tokens, completion_tokens, seconds)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            returning id
            """,
            document_id,
            analysis.section,
            analysis.model,
            PROMPT_VERSION,
            analysis.chunks,
            analysis.llm_calls,
            analysis.cached_calls,
            analysis.prompt_tokens,
            analysis.completion_tokens,
            analysis.seconds,
        )
        await conn.copy_records_to_table(
            "run_chunks",
            records=[
                (
                    run_id,
                    c.position,
                    c.paragraph_ids,
                    c.context_heading,
                    c.claims,
                    c.prompt_tokens,
                    c.completion_tokens,
                    c.latency_ms,
                    c.cached,
                    c.error,
                )
                for c in analysis.chunk_results
            ],
            columns=[
                "run_id", "position", "paragraph_ids", "context_heading", "claims",
                "prompt_tokens", "completion_tokens", "latency_ms", "cached", "error",
            ],
        )  # fmt: skip
        findings = [(f, True) for f in analysis.findings] + [(f, False) for f in analysis.rejected]
        ids = await conn.fetch(
            """
            insert into findings (run_id, position, type, claim, verified)
            select $1, n, t.type, t.claim, t.verified
            from unnest($2::text[], $3::text[], $4::boolean[])
                with ordinality as t(type, claim, verified, n)
            order by n
            returning id, position
            """,
            run_id,
            [str(f.type) for f, _ in findings],
            [f.claim for f, _ in findings],
            [verified for _, verified in findings],
        )
        finding_ids = {r["position"]: r["id"] for r in ids}
        await conn.copy_records_to_table(
            "citations",
            records=[
                (
                    finding_ids[n],
                    i,
                    c.paragraph_id,
                    c.quote,
                    c.match,
                    c.start,
                    c.end,
                    c.source_text,
                )
                for n, (f, _) in enumerate(findings, start=1)
                for i, c in enumerate(f.citations, start=1)
            ],
            columns=[
                "finding_id", "position", "paragraph_id", "quote", "match",
                "start_offset", "end_offset", "source_text",
            ],
        )  # fmt: skip
    return run_id
