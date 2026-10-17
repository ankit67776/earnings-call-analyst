# Backend

FastAPI service: ingestion, LLM pipelines, evals.

```bash
uv sync
uv run uvicorn app.main:app --reload   # http://localhost:8000/health
uv run pytest
uv run pytest -m live   # real provider calls, spends a few hundred tokens
uv run ruff check .
```
