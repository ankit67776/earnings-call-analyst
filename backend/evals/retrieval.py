"""Score retrieval on the golden questions: keyword, vector and hybrid search, with each
embedding model.

A retrieved passage is relevant when it contains the question's gold quote, or the quote
of another passage that judging found also answers it. Matching on quotes rather than
passage IDs keeps the labels valid when passages are re-cut. Every question has a
relevant passage, so recall@k is the share of questions with one in the top k.

Needs the index in Postgres (python -m app.retrieval index / embed), so it runs locally.
Query embeddings are cached, so a re-run costs no quota.

    uv run python -m evals.retrieval --readme
    uv run python -m evals.retrieval --pool 5   # write candidates for relevance judging
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app import db
from app.analysis.grounding import locate
from app.embeddings import get_embedding_client
from app.retrieval.search import FTS_WEIGHT, Hit, search
from evals.build_retrieval import GOLDEN
from evals.readme import replace_block

RESULTS = Path(__file__).resolve().parent / "results" / "retrieval.json"
# Working file for judges, not committed: passage IDs change when the index is rebuilt.
POOL = Path(".cache") / "retrieval_pool.json"
MODELS = ("voyage/voyage-4", "gemini/gemini-embedding-2")
# (name, mode, embedding model, keyword weight in the fusion)
SYSTEMS = [("fts", "fts", None, 0.0)] + [
    system
    for m in MODELS
    for system in (
        (f"vector ({m})", "vector", m, 0.0),
        (f"hybrid, equal weights ({m})", "hybrid", m, 1.0),
        (f"hybrid ({m})", "hybrid", m, FTS_WEIGHT),
    )
]
K = 10


def relevant(question: dict, text: str) -> bool:
    quotes = [question["quote"]] + [a["quote"] for a in question["alt_quotes"]]
    return any(locate(q, text, fuzzy=False) is not None for q in quotes)


def first_relevant(question: dict, hits: list[Hit]) -> int | None:
    return next((n for n, h in enumerate(hits, 1) if relevant(question, h.text)), None)


def summarize(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks)
    return {
        "recall@1": sum(r is not None and r <= 1 for r in ranks) / n,
        "recall@3": sum(r is not None and r <= 3 for r in ranks) / n,
        "recall@5": sum(r is not None and r <= 5 for r in ranks) / n,
        "recall@10": sum(r is not None and r <= 10 for r in ranks) / n,
        "mrr@10": sum(1 / r for r in ranks if r is not None) / n,
    }


async def run(questions: list[dict], pool_k: int | None) -> list[dict[str, Any]]:
    conn = await db.connect()
    clients = {m: get_embedding_client(m) for m in MODELS}
    try:
        # One batched call per model up front: per-query calls would crawl under tight
        # per-minute request limits. search() then finds every query vector cached.
        for client in clients.values():
            await client.embed([q["question"] for q in questions], "query")
        results, pool = [], {q["id"]: {} for q in questions}
        for name, mode, model, weight in SYSTEMS:
            per_question = []
            for q in questions:
                hits = await search(
                    conn,
                    q["question"],
                    ticker=q["ticker"],
                    embeddings=clients[model] if model else None,
                    mode=mode,
                    k=K,
                    fts_weight=weight,
                )
                rank = first_relevant(q, hits)
                per_question.append(
                    {"id": q["id"], "source": q["source"], "rank": rank, "top": hits[0].title}
                )
                if pool_k:
                    for h in hits[:pool_k]:
                        if not relevant(q, h.text):
                            pool[q["id"]][h.passage_id] = {"title": h.title, "text": h.text}
            ranks = [r["rank"] for r in per_question]
            by_source = {
                s: summarize([r["rank"] for r in per_question if r["source"] == s])
                for s in ("filing", "call")
            }
            results.append(
                {
                    "system": name,
                    "mode": mode,
                    "model": model,
                    "fts_weight": weight,
                    "questions": len(questions),
                    **{k: round(v, 3) for k, v in summarize(ranks).items()},
                    "by_source": {
                        s: {k: round(v, 3) for k, v in m.items()} for s, m in by_source.items()
                    },
                    "misses": [r for r in per_question if r["rank"] is None],
                }
            )
        if pool_k:
            write_pool(questions, pool)
        return results
    finally:
        await conn.close()
        for client in clients.values():
            await client.aclose()


def write_pool(questions: list[dict], pool: dict[str, dict[int, dict]]) -> None:
    """Passages any system ranked high that don't hold the gold quote: some may answer
    the question too, and a judge decides which."""
    out = [
        {
            "question": q["id"],
            "ticker": q["ticker"],
            "text": q["question"],
            "answer": q["answer"],
            "gold_quote": q["quote"],
            "candidates": [{"passage_id": pid, **c} for pid, c in pool[q["id"]].items()],
        }
        for q in questions
    ]
    POOL.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{sum(len(o['candidates']) for o in out)} candidates to judge -> {POOL}")


def _pct(v: float) -> str:
    return f"{100 * v:.0f}%"


def table(results: list[dict]) -> str:
    rows = [
        "| Search | Recall@1 | Recall@5 | Recall@10 | MRR@10 | Recall@5, call | Recall@5, 10-K |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        s = r["by_source"]
        rows.append(
            f"| {r['system']} | {_pct(r['recall@1'])} | {_pct(r['recall@5'])} "
            f"| {_pct(r['recall@10'])} | {r['mrr@10']:.2f} | {_pct(s['call']['recall@5'])} "
            f"| {_pct(s['filing']['recall@5'])} |"
        )
    return "\n".join(rows)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m evals.retrieval")
    parser.add_argument("--readme", action="store_true", help="update the README table")
    parser.add_argument("--pool", type=int, metavar="K", help="write top-K candidates to judge")
    args = parser.parse_args()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    questions = golden["questions"]
    results = asyncio.run(run(questions, args.pool))
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(table(results))
    if args.readme:
        sources = {s: sum(q["source"] == s for q in questions) for s in ("call", "filing")}
        note = (
            f"{len(questions)} questions over three companies' FY2020 10-Ks and Q3 2020 calls "
            f"({sources['call']} answered on the call, {sources['filing']} in the 10-K), "
            f"searching {_passage_count()} passages of about 250 words."
        )
        replace_block("retrieval", f"{table(results)}\n\n{note}")


def _passage_count() -> int:
    async def count() -> int:
        conn = await db.connect()
        try:
            return await conn.fetchval("select count(*) from passages")
        finally:
            await conn.close()

    return asyncio.run(count())


if __name__ == "__main__":
    main()
