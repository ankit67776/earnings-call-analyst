# Backend

FastAPI service: ingestion, LLM pipelines, evals.

- `app/llm/`: the one client every LLM call goes through (rate limits, retries, cache, strict JSON outputs)
- `app/filings/`: EDGAR download and 10-K parsing into Items and paragraphs with stable IDs
- `app/analysis/`: map-reduce claim extraction; every quote is located in the source (exact, normalized, elided with "...", then fuzzy) and claims whose quote can't be found are rejected
- `app/store.py`, `app/db.py`, `migrations/`: Postgres schema (plain SQL migrations) and persistence
- `app/jobs.py`: job queue on a Postgres table, claimed with `FOR UPDATE SKIP LOCKED`; leases renewed by a heartbeat, retries with backoff, and jobs that run out of LLM quota are deferred until it resets
- `app/main.py`, `app/queries.py`: the API: queue an analysis and poll its job; list filings; a filing's latest findings with citations; a section's paragraphs

```bash
uv run python -m app.db migrate                       # create/upgrade tables
uv run python -m app.jobs enqueue HSY --year 2020     # or POST /api/analyses
uv run python -m app.jobs work --drain                # the API also runs a worker (WORKER_IN_API)
uv run python -m app.jobs list

# Database tests run when TEST_DATABASE_URL is set; each test uses its own temporary schema.
TEST_DATABASE_URL=postgresql://... uv run pytest
```

```bash
# Extract claims from Risk Factors and MD&A (writes <filing>.<item>.findings.json next to it).
# A full 10-K is ~50-90K tokens; try --max-chunks 2 first. Responses are cached on disk.
uv run python -m app.analysis ../data/raw/10k/HSY_10-K_FY2020.htm --section 1A --max-chunks 2
```

```bash
uv sync
uv run uvicorn app.main:app --reload   # http://localhost:8000/health
uv run pytest
uv run pytest -m live   # real provider calls, spends a few hundred tokens
uv run ruff check .
```
