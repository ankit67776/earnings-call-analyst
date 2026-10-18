import httpx2
import pytest

from app.analysis.extract import ChunkResult, Citation, ClaimType, Finding, SectionAnalysis
from app.filings.tenk import parse_10k
from app.main import app, get_pool
from app.store import save_analysis, save_filing
from tests.test_db import REF
from tests.test_tenk import FLOWING

pytestmark = pytest.mark.anyio

DOC = parse_10k(FLOWING.encode())
REVENUE = DOC.section("7").paragraphs[0]  # "Revenue grew 38% in 2020."


def analysis(claim: str) -> SectionAnalysis:
    return SectionAnalysis(
        section="7",
        model="fake-model",
        chunks=1,
        findings=[
            Finding(
                ClaimType.metric,
                claim,
                [Citation(REVENUE.id, "Revenue grew 38%", "exact", 0, 16, "Revenue grew 38%")],
            )
        ],
        rejected=[Finding(ClaimType.risk, "Made up.", [Citation("7-x", "nope", None)])],
        chunk_results=[ChunkResult(0, [REVENUE.id], None, claims=2)],
        llm_calls=1,
    )


@pytest.fixture
async def client(pool):
    async with pool.acquire() as conn:
        document_id = await save_filing(conn, REF, DOC)
        await save_analysis(conn, document_id, analysis("Old wording."))
        await save_analysis(conn, document_id, analysis("Revenue grew 38% in 2020."))
    app.dependency_overrides[get_pool] = lambda: pool
    transport = httpx2.ASGITransport(app=app)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
            c.document_id = document_id
            yield c
    finally:
        app.dependency_overrides.clear()


async def test_list_and_get_documents(client) -> None:
    [doc] = (await client.get("/api/documents")).json()
    assert (doc["ticker"], doc["analyzed_sections"], doc["findings"]) == ("ACME", ["7"], 1)

    detail = (await client.get(f"/api/documents/{client.document_id}")).json()
    assert [s["item"] for s in detail["sections"]] == ["1A", "7", "7A"]
    assert [(r["section"], r["findings"], r["rejected"]) for r in detail["runs"]] == [("7", 1, 1)]
    assert (await client.get("/api/documents/999999")).status_code == 404


async def test_findings_come_from_the_latest_run(client) -> None:
    findings = (await client.get(f"/api/documents/{client.document_id}/findings")).json()
    assert [(f["claim"], f["verified"]) for f in findings] == [
        ("Revenue grew 38% in 2020.", True),
        ("Made up.", False),
    ]
    citation = findings[0]["citations"][0]
    assert citation == {
        "paragraph_id": REVENUE.id,
        "match": "exact",
        "quote": "Revenue grew 38%",
        "start": 0,
        "end": 16,
        "source_text": "Revenue grew 38%",
    }
    other = await client.get(f"/api/documents/{client.document_id}/findings?section=1a")
    assert other.json() == []


async def test_section_paragraphs(client) -> None:
    section = (await client.get(f"/api/documents/{client.document_id}/sections/7")).json()
    assert section["title"] == "Management's Discussion and Analysis"
    assert [(p["id"], p["kind"]) for p in section["paragraphs"]] == [
        (p.id, p.kind) for p in DOC.section("7").paragraphs
    ]
    missing = await client.get(f"/api/documents/{client.document_id}/sections/99")
    assert missing.status_code == 404
