# Earnings Call Analyst

An AI analyst for public-company earnings calls. It reads a company's 10-K and its earnings-call audio, then:

- extracts key claims (guidance, metrics, risks) with **every quote verified against the source**
- answers questions over the call + filings with **citations, or refuses** when there's no evidence
- flags **contradictions** between what management says on the call and what the 10-K states
- runs **live** during a call: streaming transcription, follow-up question suggestions, real-time contradiction flags

Every AI component ships with an **eval** and published numbers.

## Stack
- **Backend:** Python 3.12, FastAPI, Postgres + pgvector (hosted on Neon), Postgres-backed job queue
- **Frontend:** Next.js (App Router, TypeScript, Tailwind)
- **AI:** provider-agnostic LLM client: Groq free tier (gpt-oss-120b for analysis, gpt-oss-20b for live), Claude optional; Whisper (Groq) for batch STT, Deepgram for live STT; Voyage embeddings

## Run locally
```bash
cp .env.example .env          # add API keys
# Database: create a free Neon project and put its URL in .env (no local install).
# Prefer running Postgres locally instead? `docker compose up -d` also works.
cd backend && uv sync && uv run python -m app.db migrate && uv run uvicorn app.main:app --reload
cd frontend && npm install && npm run dev
```

See [data/README.md](data/README.md) for datasets.
