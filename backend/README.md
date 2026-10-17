# Backend

FastAPI service: ingestion, LLM pipelines, evals.

- `app/llm/`: the one client every LLM call goes through (rate limits, retries, cache, strict JSON outputs)
- `app/filings/`: EDGAR download and 10-K parsing into Items and paragraphs with stable IDs

```bash
uv sync
uv run uvicorn app.main:app --reload   # http://localhost:8000/health
uv run pytest
uv run pytest -m live   # real provider calls, spends a few hundred tokens
uv run ruff check .
```
