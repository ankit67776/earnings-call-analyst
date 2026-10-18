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

**Speech-to-text** is scored on the three companies' Q3 2020 earnings calls against
Rev's Earnings-21 reference transcripts, which are human-made and verbatim. Both sides
are normalized the way the Whisper paper does (no punctuation or fillers, numbers as
digits). Numbers and names are scored separately using the dataset's entity tags: a
wrong figure matters more to an analyst than a dropped "um".

<!-- eval:stt -->
| Model | WER | SITM WER | HSY WER | COP WER | Number words wrong | Name words wrong | Speed |
|---|---|---|---|---|---|---|---|
| whisper-large-v3 | 9.6% | 7.5% | 6.3% | 13.6% | 8.0% | 20.8% | 184x real time |
| whisper-large-v3-turbo | 9.8% | 7.7% | 6.5% | 13.9% | 8.2% | 20.6% | 274x real time |

3 calls, 157 minutes of audio, 26,419 reference words. Both sides normalized with the Whisper English normalizer.
<!-- /eval:stt -->

Most errors are deletions: Whisper drops the false starts and repeated words ("the the
the", "we we") that a verbatim reference keeps. Speakers come from the dataset's
reference diarization, matched to Whisper's segments by time, and split mid-segment
where word timestamps show the speaker changing; 99.9% of correctly transcribed words
land with the reference speaker.

**Retrieval** is scored on 118 questions written by two independent annotators from 84
sampled passages, half from the calls and half from the 10-Ks. The questions paraphrase
rather than copy the passage, the way an analyst would ask. A retrieved passage counts
when it contains the annotated answer quote, or the quote of another passage that pooled
judging found answers it too. Keyword search is Postgres full-text search; vector
search is pgvector over each embedding model; hybrid fuses the two by reciprocal rank.

<!-- eval:retrieval -->
| Search | Recall@1 | Recall@5 | Recall@10 | MRR@10 | Recall@5, call | Recall@5, 10-K |
|---|---|---|---|---|---|---|
| fts | 58% | 88% | 95% | 0.71 | 86% | 90% |
| vector (voyage/voyage-4) | 82% | 99% | 100% | 0.90 | 98% | 100% |
| hybrid, equal weights (voyage/voyage-4) | 71% | 97% | 98% | 0.82 | 97% | 97% |
| hybrid (voyage/voyage-4) | 80% | 98% | 100% | 0.88 | 97% | 100% |
| vector (gemini/gemini-embedding-2) | 80% | 98% | 99% | 0.87 | 97% | 98% |
| hybrid, equal weights (gemini/gemini-embedding-2) | 72% | 97% | 99% | 0.83 | 95% | 98% |
| hybrid (gemini/gemini-embedding-2) | 80% | 98% | 100% | 0.87 | 97% | 98% |

118 questions over three companies' FY2020 10-Ks and Q3 2020 calls (59 answered on the call, 59 in the 10-K), searching 735 passages of about 250 words.
<!-- /eval:retrieval -->

Voyage edges out Gemini (MRR 0.90 against 0.87, a gap of a few questions), so the app
uses Voyage. Textbook hybrid search, equal-weight rank fusion, was the surprise: it
loses about ten points of recall@1 to vector search alone, because keyword search is
much weaker on paraphrased questions and drags good vector hits down. Weighting the
keyword ranks at a quarter (picked on one annotator's questions, checked on the other's)
brings hybrid level with vector search. The app keeps that hybrid for exact names and
figures, which these paraphrased questions don't test.

**Cited chat** is scored on 30 answerable questions from the retrieval set and 24
unanswerable ones written to sit close to what the sources discuss (details they never
state, false premises, periods they can't cover). Every sentence of an answer must carry
a quote that's found in a retrieved passage; sentences without one are dropped, and an
answer with none left becomes a refusal. Faithfulness and correctness are judged by
`qwen3.8-27b`, a different model family from the one answering.

<!-- eval:chat -->
| Model | Answered | Correct | Correct refusals | Faithful sentences | Quotes found | Context recall | Tokens per question |
|---|---|---|---|---|---|---|---|
| openai/gpt-oss-120b | 100% | 77% | 100% | 81% | 97% | 100% | 2,643 |

30 answerable and 24 unanswerable questions over three companies, 6 passages retrieved per question (voyage/voyage-4 hybrid search), prompt v1, judged by qwen/qwen3.8-27b.
<!-- /eval:chat -->

All 24 unanswerable questions were refused, including the false premises and the
details the sources talk around without stating, and every answerable one was answered.
Verification dropped one sentence whose quote wasn't in the sources. The weak spot is
subtler: about one sentence in five is only partly supported, because it adds a
qualifier ("roughly", "largely") or a figure that its quote doesn't carry. The quote is
real, so verification can't catch it; that's the next thing to fix. To check the judge,
a second labeler graded the same answers blind: they agreed on 85% of sentence verdicts
and 83% of correctness verdicts, and put the share of fully supported sentences at 83%
against the judge's 81%.

See [backend/evals/README.md](backend/evals/README.md) for how each golden set was made
and how every metric is defined.

## Stack
- **Backend:** Python 3.12, FastAPI, Postgres + pgvector (hosted on Neon), Postgres-backed job queue
- **Frontend:** Next.js (App Router, TypeScript, Tailwind)
- **AI:** provider-agnostic LLM client: Groq free tier (gpt-oss-120b for analysis and chat, gpt-oss-20b for live), Claude optional; Whisper on Groq for batch STT, Deepgram for live STT; provider-agnostic embedding client (Voyage and Gemini, compared on the retrieval eval)
- **Retrieval:** hybrid search, Postgres full-text search plus pgvector, fused by reciprocal rank

## Run locally
```bash
cp .env.example .env          # add API keys
# Database: create a free Neon project and put its URL in .env (no local install).
# Prefer running Postgres locally instead? `docker compose up -d` also works.
cd backend && uv sync && uv run python -m app.db migrate && uv run uvicorn app.main:app --reload
cd frontend && npm install && npm run dev
```

See [data/README.md](data/README.md) for datasets.
