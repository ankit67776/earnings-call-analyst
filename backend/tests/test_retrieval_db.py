import json
import math

import httpx2
import pytest

from app.calls.transcript import LabeledSegment
from app.chat.answer import ask
from app.embeddings import EmbeddingClient
from app.embeddings.types import EmbedResult
from app.filings.tenk import parse_10k
from app.llm.client import LLMClient
from app.llm.types import Limits
from app.main import app, get_embeddings, get_llm, get_pool
from app.retrieval.index import embed_passages, index_call, index_document
from app.retrieval.search import search
from app.store import CallRecord, save_call, save_filing
from tests.fakes import FakeProvider, result
from tests.test_db import REF
from tests.test_tenk import FLOWING

pytestmark = pytest.mark.anyio

CALL = CallRecord("ACME", "Acme Corp", 2020, 3, "test:1", 120.0, "fake", "fake-stt", "test")
SEGMENTS = [
    LabeledSegment(1, 1, "Operator", 0.0, 5.0, "Welcome to the Acme third quarter call."),
    LabeledSegment(2, 2, "Jo CFO", 5.0, 30.0, "Widget shipments to Europe doubled this quarter."),
    LabeledSegment(3, 2, "Jo CFO", 30.0, 60.0, "We expect capital spending of $40M in 2021."),
]


class BagOfWords:
    """Embeds text as hashed word counts: close enough to similarity for a test."""

    name = "bow"
    max_batch = 100
    max_batch_tokens = 100_000
    counts_texts_as_requests = False

    def limits(self, model: str) -> Limits:
        return Limits(rpm=1000, tpm=1_000_000, rpd=100_000, tpd=10_000_000)

    async def embed(self, model, texts, input_type, dimensions) -> EmbedResult:
        vectors = []
        for text in texts:
            v = [0.0] * dimensions
            for word in text.lower().split():
                v[hash(word.strip(".,$")) % dimensions] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vectors.append([x / norm for x in v])
        return EmbedResult(vectors, None)

    async def aclose(self) -> None:
        pass


@pytest.fixture
async def indexed(pool):
    embeddings = EmbeddingClient(BagOfWords(), "bow-1")
    async with pool.acquire() as conn:
        document_id = await save_filing(conn, REF, parse_10k(FLOWING.encode()))
        call_id = await save_call(conn, CALL, SEGMENTS)
        filing_passages = await index_document(conn, document_id)
        call_passages = await index_call(conn, call_id)
        embedded = await embed_passages(conn, embeddings)
        again = await embed_passages(conn, embeddings)
    return {
        "document_id": document_id,
        "call_id": call_id,
        "counts": (filing_passages, call_passages, embedded, again),
        "embeddings": embeddings,
    }


async def test_index_builds_passages_and_embeds_each_once(pool, indexed) -> None:
    filing, call, embedded, again = indexed["counts"]
    assert filing >= 1 and call == 1
    assert embedded == filing + call and again == 0
    async with pool.acquire() as conn:
        row = await conn.fetchrow("select * from passages where call_id = $1", indexed["call_id"])
    assert row["speakers"] == ["Operator", "Jo CFO"]
    assert (row["first_segment"], row["last_segment"]) == (1, 3)
    assert row["title"] == "Q3 2020 earnings call, 0:00–1:00"


@pytest.mark.parametrize("mode", ["fts", "vector", "hybrid"])
async def test_search_finds_the_call_passage(pool, indexed, mode) -> None:
    async with pool.acquire() as conn:
        hits = await search(
            conn,
            "how many widgets shipped to Europe",
            ticker="ACME",
            embeddings=indexed["embeddings"],
            mode=mode,
            k=3,
        )
        other = await search(conn, "widgets", ticker="NOPE", mode="fts")
    assert hits[0].source == "call" and hits[0].call_id == indexed["call_id"]
    assert other == []


async def test_search_can_stick_to_one_source(pool, indexed) -> None:
    async with pool.acquire() as conn:
        hits = await search(
            conn, "revenue", ticker="ACME", embeddings=indexed["embeddings"], source="filing"
        )
    assert hits and all(h.source == "filing" for h in hits)


async def test_ask_points_call_citations_at_the_segment_time(pool, indexed) -> None:
    llm = LLMClient(
        FakeProvider(
            result(
                json.dumps(
                    {
                        "answerable": True,
                        "sentences": [
                            {
                                "text": "Jo CFO expects $40 million of capital spending in 2021.",
                                "support": [{"source": "S1", "quote": "capital spending of $40M"}],
                            }
                        ],
                    }
                )
            )
        ),
        default_model="m",
    )
    async with pool.acquire() as conn:
        out = await ask(
            conn,
            llm,
            indexed["embeddings"],
            ticker="ACME",
            question="What capex do they expect next year?",
            company="Acme Corp",
            k=1,
        )
    [citation] = out.sentences[0].citations
    assert citation.at == 30.0


async def test_chat_and_company_endpoints(pool, indexed) -> None:
    reply = result('{"answerable": false, "sentences": []}')
    llm = LLMClient(FakeProvider(reply), default_model="m")
    app.dependency_overrides[get_pool] = lambda: pool
    app.dependency_overrides[get_llm] = lambda: llm
    app.dependency_overrides[get_embeddings] = lambda: indexed["embeddings"]
    transport = httpx2.ASGITransport(app=app)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
            companies = (await c.get("/api/companies")).json()
            call = (await c.get(f"/api/calls/{indexed['call_id']}")).json()
            chat = await c.post("/api/chat", json={"ticker": "acme", "question": "Dividend?"})
            missing = await c.post("/api/chat", json={"ticker": "ZZZ", "question": "Dividend?"})
    finally:
        app.dependency_overrides.clear()
    [acme] = companies
    assert acme["ticker"] == "ACME" and len(acme["calls"]) == 1 and acme["passages"] >= 2
    assert [s["speaker"] for s in call["segments"]] == ["Operator", "Jo CFO", "Jo CFO"]
    assert chat.status_code == 200
    body = chat.json()
    assert body["answered"] is False and body["sources"]
    assert missing.status_code == 404
