import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app import db, jobs, queries
from app.config import settings
from app.filings.tenk import ITEMS
from app.llm import LLMClient, LLMError, get_llm_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await db.create_pool() if settings.database_url else None
    worker = None
    if app.state.pool is not None and settings.worker_in_api:
        if settings.groq_api_key and settings.sec_user_agent:
            worker = asyncio.create_task(_supervise_worker(app.state.pool))
        else:
            logger.warning("worker not started: GROQ_API_KEY or SEC_USER_AGENT missing")
    try:
        yield
    finally:
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        if app.state.pool is not None:
            await app.state.pool.close()


async def _supervise_worker(pool: asyncpg.Pool) -> None:
    """Run the job worker in this process, restarting it if it crashes (e.g. DB blip)."""
    ctx = jobs.default_context(pool)
    try:
        while True:
            try:
                await jobs.work(ctx)
            except Exception:
                logger.exception("worker crashed; restarting in 30s")
                await asyncio.sleep(30)
    finally:
        await ctx.llm.aclose()
        await ctx.edgar.aclose()


app = FastAPI(title="Earnings Call Analyst", lifespan=lifespan)


def get_pool(request: Request) -> asyncpg.Pool:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(503, "DATABASE_URL is not configured")
    return pool


Pool = Annotated[asyncpg.Pool, Depends(get_pool)]


def get_llm() -> LLMClient:
    try:
        return get_llm_client()
    except LLMError as e:
        raise HTTPException(503, str(e)) from e


LLM = Annotated[LLMClient, Depends(get_llm)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class AnalysisRequest(BaseModel):
    ticker: str = Field(pattern=r"^[A-Za-z][A-Za-z.\-]{0,9}$")
    fiscal_year: int = Field(ge=1994, le=2100)
    sections: list[str] | None = None

    @field_validator("sections")
    @classmethod
    def known_items(cls, sections: list[str] | None) -> list[str] | None:
        if sections is None:
            return None
        upper = [s.upper() for s in sections]
        unknown = [s for s in upper if s not in ITEMS]
        if unknown:
            raise ValueError(f"unknown 10-K items: {unknown}")
        return upper


@app.post("/api/analyses", status_code=202)
async def create_analysis(body: AnalysisRequest, pool: Pool) -> dict[str, int]:
    """Queue a 10-K for analysis; poll /api/jobs/{job_id} for progress."""
    payload: dict[str, Any] = {"ticker": body.ticker.upper(), "fiscal_year": body.fiscal_year}
    if body.sections:
        payload["sections"] = body.sections
    async with pool.acquire() as conn:
        job_id = await jobs.enqueue(conn, "analyze_filing", payload)
    return {"job_id": job_id}


_JOB_COLUMNS = (
    "id, kind, status, attempts, max_attempts, payload, result, error, progress,"
    " run_after, created_at, updated_at"
)


@app.get("/api/jobs")
async def list_jobs(
    pool: Pool, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[dict[str, Any]]:
    """The most recent jobs, newest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"select {_JOB_COLUMNS} from jobs order by id desc limit $1", limit)
    return [dict(r) for r in rows]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"select {_JOB_COLUMNS} from jobs where id = $1", job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    return dict(row)


@app.get("/api/usage")
def get_usage(llm: LLM) -> dict[str, Any]:
    """Today's token use of the analysis model against the provider's daily budget."""
    return llm.usage(settings.analysis_model)


@app.get("/api/documents")
async def list_documents(pool: Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        return await queries.list_documents(conn)


@app.get("/api/documents/{document_id}")
async def get_document(document_id: int, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        doc = await queries.get_document(conn, document_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    return doc


@app.get("/api/documents/{document_id}/findings")
async def get_findings(
    document_id: int, pool: Pool, section: str | None = None
) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        return await queries.get_findings(conn, document_id, section.upper() if section else None)


@app.get("/api/documents/{document_id}/sections/{item}")
async def get_section(document_id: int, item: str, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        section = await queries.get_section(conn, document_id, item.upper())
    if section is None:
        raise HTTPException(404, "section not found")
    return section
