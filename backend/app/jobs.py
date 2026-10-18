"""A Postgres-backed job queue and worker.

Workers claim jobs with `FOR UPDATE SKIP LOCKED`, so any number can run without a
separate queue service. A running job's lease is renewed by a heartbeat; if a worker
dies, the job becomes claimable again once the lease expires. Failures retry with
backoff. A job that runs out of LLM quota is deferred until the quota resets, without
using up an attempt.

    uv run python -m app.jobs enqueue SITM --year 2020
    uv run python -m app.jobs work --drain
"""

import argparse
import asyncio
import logging
import os
import socket
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import asyncpg

from app.analysis.extract import analyze_section
from app.config import settings
from app.filings.edgar import EdgarClient, FilingNotFoundError
from app.filings.tenk import FOCUS_SECTIONS, parse_10k
from app.llm import BudgetExceededError, LLMClient, RateLimitedError, get_llm_client
from app.store import save_analysis, save_filing

logger = logging.getLogger(__name__)

LEASE = timedelta(minutes=5)
HEARTBEAT_SECONDS = 60.0


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


class RetryLater(Exception):
    """Run the job again at `run_after` without counting this attempt. `payload`, if
    given, replaces the job's payload (e.g. to remember work already done)."""

    def __init__(
        self, run_after: datetime, reason: str, payload: dict[str, Any] | None = None
    ) -> None:
        super().__init__(reason)
        self.run_after = run_after
        self.payload = payload


class PermanentError(Exception):
    """Retrying can't help (bad input, missing filing): fail the job now."""


@dataclass
class Context:
    pool: asyncpg.Pool
    llm: LLMClient
    edgar: EdgarClient
    raw_dir: Path
    # The job being run, set by run_job so its handler can report progress.
    job_id: int | None = None


Handler = Callable[[Context, dict[str, Any]], Awaitable[dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return register


# --- queue operations ---


async def enqueue(
    conn: asyncpg.Connection, kind: str, payload: dict[str, Any], *, max_attempts: int = 3
) -> int:
    return await conn.fetchval(
        "insert into jobs (kind, payload, max_attempts) values ($1, $2, $3) returning id",
        kind,
        payload,
        max_attempts,
    )


async def claim(conn: asyncpg.Connection, worker: str, lease: timedelta = LEASE) -> Job | None:
    """Take the next ready job, or a running one whose worker stopped renewing its lease."""
    row = await conn.fetchrow(
        """
        update jobs set status = 'running', attempts = attempts + 1,
                        locked_by = $1, locked_at = now(), updated_at = now()
        where id = (
            select id from jobs
            where (status = 'queued' and run_after <= now())
               or (status = 'running' and locked_at < now() - $2::interval)
            order by run_after, id
            limit 1
            for update skip locked
        )
        returning id, kind, payload, attempts, max_attempts
        """,
        worker,
        lease,
    )
    return Job(**dict(row)) if row else None


async def complete(conn: asyncpg.Connection, job_id: int, result: dict[str, Any]) -> None:
    await conn.execute(
        """
        update jobs set status = 'done', result = $2, error = null,
                        locked_by = null, locked_at = null, updated_at = now()
        where id = $1
        """,
        job_id,
        result,
    )


async def fail(conn: asyncpg.Connection, job_id: int, error: str, *, permanent: bool) -> None:
    """Requeue with exponential backoff (1, 2, 4... minutes), or fail once out of attempts."""
    await conn.execute(
        """
        update jobs set
            status = case when $3 or attempts >= max_attempts then 'failed' else 'queued' end,
            run_after = now() + make_interval(mins => power(2, attempts - 1)::int),
            error = $2, locked_by = null, locked_at = null, updated_at = now()
        where id = $1
        """,
        job_id,
        error,
        permanent,
    )


async def report(ctx: Context, progress: dict[str, Any]) -> None:
    """Record how far the running job has got, for the UI. Best effort: a failed write
    never fails the job."""
    if ctx.job_id is None:
        return
    try:
        async with ctx.pool.acquire() as conn:
            await conn.execute("update jobs set progress = $2 where id = $1", ctx.job_id, progress)
    except (asyncpg.PostgresError, OSError):
        logger.warning("couldn't record progress for job %d", ctx.job_id, exc_info=True)


async def defer(
    conn: asyncpg.Connection,
    job_id: int,
    run_after: datetime,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        update jobs set status = 'queued', attempts = greatest(attempts - 1, 0),
                        run_after = $2, error = $3, payload = coalesce($4, payload),
                        locked_by = null, locked_at = null, updated_at = now()
        where id = $1
        """,
        job_id,
        run_after,
        reason,
        payload,
    )


# --- worker ---


async def run_job(ctx: Context, job: Job) -> None:
    beat = asyncio.create_task(_heartbeat(ctx.pool, job.id))
    try:
        fn = HANDLERS.get(job.kind)
        if fn is None:
            raise PermanentError(f"no handler for job kind {job.kind!r}")
        result = await fn(replace(ctx, job_id=job.id), job.payload)
    except RetryLater as e:
        logger.info("job %d deferred until %s: %s", job.id, e.run_after, e)
        async with ctx.pool.acquire() as conn:
            await defer(conn, job.id, e.run_after, str(e), e.payload)
    except Exception as e:
        logger.exception("job %d (%s) failed", job.id, job.kind)
        async with ctx.pool.acquire() as conn:
            await fail(conn, job.id, f"{type(e).__name__}: {e}", permanent=_is_permanent(e))
    else:
        logger.info("job %d (%s) done", job.id, job.kind)
        async with ctx.pool.acquire() as conn:
            await complete(conn, job.id, result)
    finally:
        beat.cancel()


async def work(
    ctx: Context,
    *,
    worker: str | None = None,
    drain: bool = False,
    poll_seconds: float = 5.0,
    max_idle_seconds: float = 600.0,
) -> int:
    """Process jobs until cancelled, or until none are ready when `drain` is set.

    When idle, the poll interval doubles up to `max_idle_seconds`, so a serverless
    database like Neon can scale to zero between bursts of work.
    """
    worker = worker or f"{socket.gethostname()}:{os.getpid()}"
    processed, idle = 0, poll_seconds
    while True:
        async with ctx.pool.acquire() as conn:
            job = await claim(conn, worker)
        if job is None:
            if drain:
                return processed
            await asyncio.sleep(idle)
            idle = min(idle * 2, max_idle_seconds)
            continue
        idle = poll_seconds
        logger.info("job %d (%s) attempt %d: %s", job.id, job.kind, job.attempts, job.payload)
        await run_job(ctx, job)
        processed += 1


async def _heartbeat(pool: asyncpg.Pool, job_id: int) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        async with pool.acquire() as conn:
            await conn.execute(
                "update jobs set locked_at = now() where id = $1 and status = 'running'", job_id
            )


def _is_permanent(error: Exception) -> bool:
    return isinstance(error, PermanentError | FilingNotFoundError)


# --- handlers ---


@handler("analyze_filing")
async def analyze_filing(ctx: Context, payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch a 10-K, store its parsed text, and extract claims from the given sections.

    Payload: {"ticker": "SITM", "fiscal_year": 2020, "sections": ["1A", "7"]}.
    Sections already analyzed by an earlier, deferred attempt are kept in "done".
    """
    ticker, year = payload["ticker"], int(payload["fiscal_year"])
    sections = payload.get("sections") or list(FOCUS_SECTIONS)
    done: dict[str, int] = dict(payload.get("done", {}))

    await report(ctx, {"step": "fetching", "sections": sections, "done": list(done)})
    ref = await ctx.edgar.find_annual_report(ticker, year)
    path = await ctx.edgar.download(ref, ctx.raw_dir / "10k")
    doc = parse_10k(path.read_bytes())
    async with ctx.pool.acquire() as conn:
        document_id = await save_filing(conn, ref, doc)

    missing = [item for item in sections if doc.section(item) is None]
    for item in sections:
        section = doc.section(item)
        if section is None or item in done:
            continue
        started_at = datetime.now(UTC).isoformat()

        async def progress(
            chunks_done: int, chunks: int, item: str = item, started_at: str = started_at
        ) -> None:
            await report(
                ctx,
                {
                    "step": "extracting",
                    "sections": sections,
                    "done": list(done),
                    "section": item,
                    "chunks_done": chunks_done,
                    "chunks": chunks,
                    "started_at": started_at,
                },
            )

        try:
            analysis = await analyze_section(
                ctx.llm, section, company=ref.company, on_progress=progress
            )
        except (BudgetExceededError, RateLimitedError) as e:
            run_after = _quota_reset(e)
            raise RetryLater(run_after, str(e), payload | {"done": done}) from e
        async with ctx.pool.acquire() as conn:
            done[item] = await save_analysis(conn, document_id, analysis)

    return {"document_id": document_id, "runs": done, "missing_sections": missing}


def _quota_reset(error: Exception) -> datetime:
    now = datetime.now(UTC)
    if isinstance(error, RateLimitedError) and error.retry_after is not None:
        return now + timedelta(seconds=error.retry_after)
    if isinstance(error, RateLimitedError):
        return now + timedelta(minutes=5)
    # The daily token budget is tracked per UTC day.
    return datetime.combine(now.date() + timedelta(days=1), time(0, 5), tzinfo=UTC)


# --- CLI ---


def default_context(pool: asyncpg.Pool) -> Context:
    return Context(
        pool=pool,
        llm=get_llm_client(),
        edgar=EdgarClient(settings.sec_user_agent),
        raw_dir=Path(settings.raw_data_dir),
    )


async def _cli(args: argparse.Namespace) -> None:
    from app.db import create_pool

    pool = await create_pool()
    try:
        if args.command == "enqueue":
            payload = {"ticker": args.ticker.upper(), "fiscal_year": args.year}
            if args.section:
                payload["sections"] = args.section
            async with pool.acquire() as conn:
                job_id = await enqueue(conn, "analyze_filing", payload)
            print(f"queued job {job_id}: analyze_filing {payload}")
        elif args.command == "work":
            ctx = default_context(pool)
            try:
                count = await work(ctx, drain=args.drain)
                print(f"processed {count} jobs")
            finally:
                await ctx.llm.aclose()
                await ctx.edgar.aclose()
        else:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "select id, kind, status, attempts, run_after, error, payload"
                    " from jobs order by id desc limit 20"
                )
            for r in rows:
                error = f"  error: {r['error'][:100]}" if r["error"] else ""
                print(
                    f"{r['id']:>4} {r['kind']:<15} {r['status']:<8} attempts={r['attempts']} "
                    f"run_after={r['run_after']:%Y-%m-%d %H:%M}Z {r['payload']}{error}"
                )
    finally:
        await pool.close()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m app.jobs")
    commands = parser.add_subparsers(dest="command", required=True)
    enqueue_cmd = commands.add_parser("enqueue", help="queue a 10-K for analysis")
    enqueue_cmd.add_argument("ticker")
    enqueue_cmd.add_argument("--year", type=int, required=True, help="fiscal year")
    enqueue_cmd.add_argument("--section", action="append", help="Item (default: 1A and 7)")
    work_cmd = commands.add_parser("work", help="run a worker")
    work_cmd.add_argument("--drain", action="store_true", help="exit when no job is ready")
    commands.add_parser("list", help="show recent jobs")
    asyncio.run(_cli(parser.parse_args()))


if __name__ == "__main__":
    main()
