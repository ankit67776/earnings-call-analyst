# Backend

FastAPI service: ingestion, LLM pipelines, evals.

- `app/llm/`: the one client every LLM call goes through (rate limits, retries, cache, strict JSON outputs)
- `app/filings/`: EDGAR download and 10-K parsing into Items and paragraphs with stable IDs
- `app/analysis/`: map-reduce claim extraction; every quote is located in the source (exact, normalized, elided with "...", then fuzzy) and claims whose quote can't be found are rejected

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
