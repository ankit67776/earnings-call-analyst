import asyncio
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from app import jobs
from app.filings.edgar import FilingNotFoundError
from app.filings.tenk import parse_10k
from app.llm.client import LLMClient
from app.llm.types import BudgetExceededError
from app.main import app, get_pool
from tests.fakes import FakeTime
from tests.test_db import REF
from tests.test_extract import ByContentProvider, claims
from tests.test_tenk import FLOWING

pytestmark = pytest.mark.anyio


async def status(pool, job_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("select * from jobs where id = $1", job_id)


async def enqueue(pool, kind: str = "test", payload: dict | None = None, **kw) -> int:
    async with pool.acquire() as conn:
        return await jobs.enqueue(conn, kind, payload or {}, **kw)


async def claim(pool, worker: str = "w1"):
    async with pool.acquire() as conn:
        return await jobs.claim(conn, worker)


def context(pool, tmp_path, llm=None, edgar=None) -> jobs.Context:
    return jobs.Context(pool=pool, llm=llm, edgar=edgar, raw_dir=tmp_path)


async def test_claim_and_complete(pool, tmp_path, monkeypatch) -> None:
    async def double(ctx, payload):
        return {"value": payload["n"] * 2}

    monkeypatch.setitem(jobs.HANDLERS, "test", double)
    job_id = await enqueue(pool, payload={"n": 21})
    job = await claim(pool)
    assert (job.id, job.kind, job.payload, job.attempts) == (job_id, "test", {"n": 21}, 1)
    assert await claim(pool, "w2") is None  # nothing else ready

    await jobs.run_job(context(pool, tmp_path), job)
    row = await status(pool, job_id)
    assert (row["status"], row["result"], row["locked_by"]) == ("done", {"value": 42}, None)


async def test_claim_skips_jobs_locked_by_another_worker(pool) -> None:
    job_id = await enqueue(pool)
    async with pool.acquire() as a, pool.acquire() as b:
        tx = a.transaction()
        await tx.start()
        try:
            assert (await jobs.claim(a, "w1")).id == job_id
            # While w1's transaction holds the row, w2 neither waits nor gets it.
            assert await asyncio.wait_for(jobs.claim(b, "w2"), timeout=10) is None
        finally:
            await tx.rollback()
        assert (await jobs.claim(b, "w2")).id == job_id


async def test_a_stale_lease_is_reclaimed(pool) -> None:
    job_id = await enqueue(pool)
    await claim(pool, "dead-worker")
    async with pool.acquire() as conn:
        await conn.execute(
            "update jobs set locked_at = now() - interval '10 minutes' where id = $1", job_id
        )
    job = await claim(pool, "w2")
    assert (job.id, job.attempts) == (job_id, 2)


async def test_failures_retry_with_backoff_then_fail(pool, tmp_path, monkeypatch) -> None:
    async def broken(ctx, payload):
        raise ValueError("boom")

    monkeypatch.setitem(jobs.HANDLERS, "test", broken)
    job_id = await enqueue(pool, max_attempts=2)

    await jobs.run_job(context(pool, tmp_path), await claim(pool))
    row = await status(pool, job_id)
    assert (row["status"], row["error"]) == ("queued", "ValueError: boom")
    assert row["run_after"] > datetime.now(UTC) + timedelta(seconds=30)

    async with pool.acquire() as conn:
        await conn.execute("update jobs set run_after = now() where id = $1", job_id)
    await jobs.run_job(context(pool, tmp_path), await claim(pool))
    assert (await status(pool, job_id))["status"] == "failed"


async def test_permanent_errors_fail_immediately(pool, tmp_path, monkeypatch) -> None:
    async def missing(ctx, payload):
        raise FilingNotFoundError("unknown ticker 'NOPE'")

    monkeypatch.setitem(jobs.HANDLERS, "test", missing)
    job_id = await enqueue(pool)
    await jobs.run_job(context(pool, tmp_path), await claim(pool))
    row = await status(pool, job_id)
    assert (row["status"], row["attempts"]) == ("failed", 1)


async def test_retry_later_defers_without_using_an_attempt(pool, tmp_path, monkeypatch) -> None:
    later = datetime.now(UTC) + timedelta(hours=3)

    async def out_of_quota(ctx, payload):
        raise jobs.RetryLater(later, "daily budget used up", payload | {"done": {"1A": 7}})

    monkeypatch.setitem(jobs.HANDLERS, "test", out_of_quota)
    job_id = await enqueue(pool, payload={"ticker": "SITM"})
    await jobs.run_job(context(pool, tmp_path), await claim(pool))
    row = await status(pool, job_id)
    assert (row["status"], row["attempts"]) == ("queued", 0)
    assert row["payload"] == {"ticker": "SITM", "done": {"1A": 7}}
    assert abs(row["run_after"] - later) < timedelta(seconds=1)
    assert await claim(pool) is None  # not ready until then


class FakeEdgar:
    def __init__(self, html: str) -> None:
        self.html = html

    async def find_annual_report(self, ticker, fiscal_year):
        return REF

    async def download(self, ref, dest_dir):
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / ref.filename
        path.write_text(self.html, encoding="utf-8")
        return path


DOC = parse_10k(FLOWING.encode())
RISK_REPLY = claims(
    ("risk", DOC.section("1A").paragraphs[1].id, "Our revenue depends on a few customers",
     "Revenue is concentrated in a few customers."),
)  # fmt: skip
MDNA_REPLY = claims(
    ("metric", DOC.section("7").paragraphs[0].id, "Revenue grew 38% in 2020.",
     "Revenue grew 38% in 2020."),
)  # fmt: skip


def llm(replies) -> tuple[LLMClient, ByContentProvider]:
    provider = ByContentProvider(replies)
    time = FakeTime()
    client = LLMClient(provider, default_model="fake-model", sleep=time.sleep, clock=time.clock)
    return client, provider


async def test_analyze_filing_end_to_end_resuming_after_quota_runs_out(pool, tmp_path) -> None:
    payload = {"ticker": "ACME", "fiscal_year": 2020, "sections": ["1A", "7"]}
    job_id = await enqueue(pool, "analyze_filing", payload)

    # First attempt: Item 1A succeeds, then the daily budget runs out during Item 7.
    client, first = llm(
        {
            "Section: Item 1A.": RISK_REPLY,
            "Section: Item 7.": BudgetExceededError("daily budget used up"),
        }
    )
    ctx = context(pool, tmp_path, client, FakeEdgar(FLOWING))
    await jobs.run_job(ctx, await claim(pool))
    row = await status(pool, job_id)
    assert (row["status"], row["attempts"]) == ("queued", 0)
    assert row["run_after"].date() > datetime.now(UTC).date()
    assert set(row["payload"]["done"]) == {"1A"}

    # Quota is back: only Item 7 runs.
    async with pool.acquire() as conn:
        await conn.execute("update jobs set run_after = now() where id = $1", job_id)
    client, second = llm({"Section: Item 7.": MDNA_REPLY})
    ctx = context(pool, tmp_path, client, FakeEdgar(FLOWING))
    await jobs.run_job(ctx, await claim(pool))

    row = await status(pool, job_id)
    assert row["status"] == "done"
    assert set(row["result"]["runs"]) == {"1A", "7"}
    assert all("Section: Item 7." in p for p in second.prompts)
    async with pool.acquire() as conn:
        found = await conn.fetch(
            """
            select r.section, f.claim, c.source_text from analysis_runs r
            join findings f on f.run_id = r.id join citations c on c.finding_id = f.id
            where r.document_id = $1 and f.verified order by r.section
            """,
            row["result"]["document_id"],
        )
    assert [tuple(r) for r in found] == [
        (
            "1A",
            "Revenue is concentrated in a few customers.",
            "Our revenue depends on a few customers",
        ),
        ("7", "Revenue grew 38% in 2020.", "Revenue grew 38% in 2020."),
    ]


async def test_api_queues_analyses_and_reports_job_status(pool) -> None:
    app.dependency_overrides[get_pool] = lambda: pool
    transport = httpx2.ASGITransport(app=app)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/api/analyses", json={"ticker": "sitm", "fiscal_year": 2020, "sections": ["1a"]}
            )
            assert r.status_code == 202
            job = (await client.get(f"/api/jobs/{r.json()['job_id']}")).json()
            assert (job["status"], job["kind"]) == ("queued", "analyze_filing")
            assert job["payload"] == {"ticker": "SITM", "fiscal_year": 2020, "sections": ["1A"]}

            assert (await client.get("/api/jobs/999999")).status_code == 404
            bad = {"ticker": "SITM", "fiscal_year": 2020, "sections": ["99"]}
            assert (await client.post("/api/analyses", json=bad)).status_code == 422
    finally:
        app.dependency_overrides.clear()
