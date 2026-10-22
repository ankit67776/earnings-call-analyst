import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from app import db, jobs, queries
from app.calls import earnings21
from app.calls.stt import STTError
from app.chat.answer import ask, to_json
from app.config import settings
from app.embeddings import EmbeddingClient, EmbeddingError, get_embedding_client
from app.filings.tenk import ITEMS
from app.live.session import LiveSessions
from app.live.stt import StreamRequest, get_live_transcriber
from app.llm import BudgetExceededError, LLMClient, LLMError, RateLimitedError, get_llm_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await db.create_pool() if settings.database_url else None
    app.state.live = LiveSessions(
        get_live_transcriber,
        request=StreamRequest(model=settings.live_stt_model),
        log_dir=Path(settings.live_log_dir),
    )
    worker = None
    if app.state.pool is not None and settings.worker_in_api:
        if settings.groq_api_key and settings.sec_user_agent:
            worker = asyncio.create_task(_supervise_worker(app.state.pool))
        else:
            logger.warning("worker not started: GROQ_API_KEY or SEC_USER_AGENT missing")
    try:
        yield
    finally:
        await app.state.live.aclose()
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


def get_embeddings() -> EmbeddingClient:
    try:
        return get_embedding_client()
    except EmbeddingError as e:
        raise HTTPException(503, str(e)) from e


Embeddings = Annotated[EmbeddingClient, Depends(get_embeddings)]


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


@app.get("/api/companies")
async def list_companies(pool: Pool) -> list[dict[str, Any]]:
    """Companies with stored filings or calls: what the chat can answer about."""
    async with pool.acquire() as conn:
        return await queries.list_companies(conn)


@app.get("/api/calls/{call_id}")
async def get_call(call_id: int, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        call = await queries.get_call(conn, call_id)
    if call is None:
        raise HTTPException(404, "call not found")
    return call


# Finds a call's audio file, or None when it has none.
AudioLookup = Callable[[int], Awaitable[Path | None]]


def get_audio_lookup(pool: Pool) -> AudioLookup:
    async def lookup(call_id: int) -> Path | None:
        async with pool.acquire() as conn:
            source = await conn.fetchval("select source from calls where id = $1", call_id)
        if not source or not source.startswith("earnings21:"):
            return None
        path = earnings21.audio_path(source.removeprefix("earnings21:"))
        return path if path.exists() else None

    return lookup


Audio = Annotated[AudioLookup, Depends(get_audio_lookup)]


@app.get("/api/calls/{call_id}/audio")
async def get_call_audio(call_id: int, audio: Audio) -> FileResponse:
    path = await audio(call_id)
    if path is None:
        raise HTTPException(404, "no audio for this call")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/calls/{call_id}/contradictions")
async def get_call_contradictions(call_id: int, pool: Pool) -> dict[str, Any]:
    """The call's claims, each with the verifier's verdicts against the 10-K."""
    async with pool.acquire() as conn:
        checks = await queries.get_call_checks(conn, call_id)
    if checks is None:
        raise HTTPException(404, "call not found")
    return checks


class ChatRequest(BaseModel):
    ticker: str = Field(pattern=r"^[A-Za-z][A-Za-z.\-]{0,9}$")
    question: str = Field(min_length=3, max_length=500)


@app.post("/api/chat")
async def chat(body: ChatRequest, pool: Pool, llm: LLM, embeddings: Embeddings) -> dict[str, Any]:
    """Answer from the company's filings and calls with a citation per sentence, or refuse."""
    ticker = body.ticker.upper()
    async with pool.acquire() as conn:
        company = await conn.fetchval(
            "select company from documents where ticker = $1"
            " union all select company from calls where ticker = $1 limit 1",
            ticker,
        )
        if company is None:
            raise HTTPException(404, f"nothing stored for {ticker}")
        try:
            result = await ask(
                conn,
                llm,
                embeddings,
                ticker=ticker,
                question=body.question.strip(),
                company=company,
                model=settings.chat_model,
            )
        except (BudgetExceededError, RateLimitedError) as e:
            raise HTTPException(429, str(e)) from e
        except (LLMError, EmbeddingError) as e:
            raise HTTPException(502, str(e)) from e
    return to_json(result)


def get_live(request: Request) -> LiveSessions:
    return request.app.state.live


Live = Annotated[LiveSessions, Depends(get_live)]


class LiveStart(BaseModel):
    call_id: int
    # Stream to the provider even when a recorded run exists (spends STT credit).
    fresh: bool = False


@app.post("/api/live/sessions", status_code=201)
async def start_live_session(body: LiveStart, live: Live, audio: Audio) -> dict[str, Any]:
    """Replay a call as if it were happening now, or join its running session."""
    path = await audio(body.call_id)
    if path is None:
        raise HTTPException(404, "no audio for this call")
    try:
        session = await live.start(body.call_id, path, fresh=body.fresh)
    except STTError as e:
        raise HTTPException(503, str(e)) from e
    return session.snapshot()


@app.get("/api/live/sessions")
def list_live_sessions(live: Live, call_id: int) -> list[dict[str, Any]]:
    """The call's latest session, running or finished, if it has one."""
    session = live.latest(call_id)
    return [session.snapshot()] if session is not None else []


@app.delete("/api/live/sessions/{session_id}")
async def stop_live_session(session_id: str, live: Live) -> dict[str, Any]:
    session = live.get(session_id)
    if session is None:
        raise HTTPException(404, "no such live session")
    await session.stop()
    return session.snapshot()


@app.websocket("/api/live/sessions/{session_id}/events")
async def live_events(websocket: WebSocket, session_id: str) -> None:
    """The session's state and history so far, then each event as it happens. Closes
    after the end event, or with 1013 if the tab falls too far behind."""
    session = websocket.app.state.live.get(session_id)
    await websocket.accept()
    if session is None:
        await websocket.close(code=4404, reason="no such live session")
        return
    backlog, subscriber = session.subscribe()
    try:
        for event in backlog:
            await websocket.send_json(event)
        event = backlog[-1]
        while event["type"] != "end":
            queued = await subscriber.queue.get()
            if queued is None:
                await websocket.close(code=1013, reason="fell behind; reconnect to catch up")
                return
            event = queued
            await websocket.send_json(event)
        await websocket.close()
    except WebSocketDisconnect:
        pass
    finally:
        session.unsubscribe(subscriber)
