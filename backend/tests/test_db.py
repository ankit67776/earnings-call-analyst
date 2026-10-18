import pytest

from app import db
from app.analysis.extract import ChunkResult, Citation, ClaimType, Finding, SectionAnalysis
from app.filings.edgar import FilingRef
from app.filings.tenk import parse_10k
from app.store import save_analysis, save_filing
from tests.test_tenk import FLOWING

pytestmark = pytest.mark.anyio

REF = FilingRef(
    ticker="ACME",
    company="Acme Corp",
    form="10-K",
    accession="0000000001-21-000001",
    cik=1,
    period="2020-12-31",
    filed="2021-02-16",
    primary_document="acme-10k.htm",
)


async def test_migrations_apply_once(pool) -> None:
    async with pool.acquire() as conn:
        assert await db.migrate(conn) == []
        tables = await conn.fetchval(
            "select count(*) from pg_tables where schemaname = current_schema()"
        )
    assert tables >= 9


async def test_save_filing_stores_sections_and_paragraphs(pool) -> None:
    doc = parse_10k(FLOWING.encode())
    async with pool.acquire() as conn:
        document_id = await save_filing(conn, REF, doc)
        again = await save_filing(conn, REF, doc)  # re-saving replaces, doesn't duplicate
        sections = await conn.fetch(
            "select item, title from sections where document_id = $1 order by position",
            document_id,
        )
        paragraphs = await conn.fetch(
            "select id, kind, text from paragraphs"
            " where document_id = $1 and section = '7' order by position",
            document_id,
        )
    assert again == document_id
    assert [s["item"] for s in sections] == ["1A", "7", "7A"]
    assert [(p["id"], p["kind"], p["text"]) for p in paragraphs] == [
        (p.id, p.kind, p.text) for p in doc.section("7").paragraphs
    ]


async def test_save_analysis_round_trips_findings_and_citations(pool) -> None:
    doc = parse_10k(FLOWING.encode())
    paragraph = doc.section("7").paragraphs[0]  # "Revenue grew 38% in 2020."
    verified = Finding(
        ClaimType.metric,
        "Revenue grew 38% in 2020.",
        [Citation(paragraph.id, "revenue grew 38%", "normalized", 0, 16, "Revenue grew 38%")],
    )
    rejected = Finding(ClaimType.risk, "Made up.", [Citation("7-nope", "not there", None)])
    analysis = SectionAnalysis(
        section="7",
        model="fake-model",
        chunks=1,
        findings=[verified],
        rejected=[rejected],
        chunk_results=[ChunkResult(0, [paragraph.id], None, claims=2, prompt_tokens=10)],
        prompt_tokens=10,
        llm_calls=1,
    )
    async with pool.acquire() as conn:
        document_id = await save_filing(conn, REF, doc)
        run_id = await save_analysis(conn, document_id, analysis)
        rows = await conn.fetch(
            """
            select f.type, f.claim, f.verified, c.paragraph_id, c.match, c.source_text
            from findings f join citations c on c.finding_id = f.id
            where f.run_id = $1 order by f.position, c.position
            """,
            run_id,
        )
        chunk = await conn.fetchrow("select * from run_chunks where run_id = $1", run_id)
    assert [tuple(r) for r in rows] == [
        (
            "metric",
            "Revenue grew 38% in 2020.",
            True,
            paragraph.id,
            "normalized",
            "Revenue grew 38%",
        ),
        ("risk", "Made up.", False, "7-nope", None, None),
    ]
    assert (chunk["paragraph_ids"], chunk["claims"], chunk["error"]) == ([paragraph.id], 2, None)
