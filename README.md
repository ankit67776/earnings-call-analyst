# Earnings Call Analyst

An AI analyst for public-company earnings calls. It reads a company's 10-K and its earnings-call audio, then:

- extracts key claims (guidance, metrics, risks) with **every quote verified against the source**
- answers questions over the call + filings with **citations, or refuses** when there's no evidence
- flags **contradictions** between what management says on the call and what the 10-K states
- runs **live** during a call: streaming transcription, follow-up question suggestions, real-time contradiction flags

Every AI component ships with an **eval** and published numbers.

## Evals

**Claim extraction** is scored against a golden set of 64 claims in 18 passages from the
FY2020 10-Ks of Hershey, SiTime and ConocoPhillips, sampled from risk factors and MD&A.
Two annotators labeled the passages independently (91% agreement), and a third pass
settled the disagreements. A predicted claim counts as correct when its quote overlaps a
labeled claim's quote in the same paragraph. See
[backend/evals/README.md](backend/evals/README.md) for how the labels were made, the
labeling rules and the metric definitions.

<!-- eval:extraction -->
| Model | Precision | Recall | F1 | Type accuracy | Quotes found | Right paragraph | Tokens per passage | LLM seconds per passage |
|---|---|---|---|---|---|---|---|---|
| openai/gpt-oss-120b | 69% | 59% | 64% | 92% | 100% | 100% | 1,383 | 1.5 |
| openai/gpt-oss-20b | 65% | 66% | 65% | 98% | 100% | 23% | 1,367 | 1.0 |

18 passages from three FY2020 10-Ks, 64 labeled claims, prompt v1.
<!-- /eval:extraction -->

CI replays recorded model responses (`evals/cache/`) and fails if the scores drop below
their floors. A prompt or model change needs one live run to record new responses:

```bash
cd backend
uv run python -m evals.run --model openai/gpt-oss-120b --model openai/gpt-oss-20b --readme
```

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
