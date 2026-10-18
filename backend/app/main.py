import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app import db, jobs
from app.config import settings
from app.filings.tenk import ITEMS

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


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: int, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select id, kind, status, attempts, max_attempts, payload, result, error,"
            " run_after, created_at, updated_at from jobs where id = $1",
            job_id,
        )
    if row is None:
        raise HTTPException(404, "job not found")
    return dict(row)
