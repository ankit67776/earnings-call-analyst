"""Evaluate the cited chat: does it answer what the sources answer, refuse what they
don't, and say only what its citations support?

Each question goes through the real pipeline: hybrid retrieval, the answer prompt and
quote verification. Answerable questions come from the retrieval golden set with the
annotator's reference answer; unanswerable ones were written to sit close to what the
sources discuss (golden/chat.json).

- Answered: share of answerable questions answered rather than refused.
- Correct refusals: share of unanswerable questions refused.
- Quotes found: share of the model's quotes located in the sources, before verification
  drops the rest (and any sentence left without one).
- Context recall: share of answerable questions whose gold passage was retrieved.
- Faithful: share of answer sentences their citations fully support (judge).
- Correct: share of answered, answerable questions that give the reference answer's key
  facts (judge).

The judge is a different model family from the one answering (Qwen vs gpt-oss), so it
doesn't grade its own style. Live runs record each question's retrieved passages
(cache/chat_contexts.json) and every LLM response (cache/), so --offline replays the
whole eval without a database or API key, as CI does.

    uv run python -m evals.chat --readme
    uv run python -m evals.chat --offline --min-refusal 0.8 --min-faithful 0.8
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from app.chat.answer import DEFAULT_K, PROMPT_VERSION, ChatAnswer, answer
from app.config import settings
from app.llm import LLMClient, LLMError
from app.retrieval.search import Hit
from evals.build_chat import CHAT
from evals.build_retrieval import GOLDEN as RETRIEVAL
from evals.readme import replace_block
from evals.retrieval import relevant
from evals.run import CACHE, make_client

RESULTS = Path(__file__).resolve().parent / "results" / "chat.json"
CONTEXTS = CACHE / "chat_contexts.json"
JUDGE_MODEL = "qwen/qwen3.8-27b"
# The judge writes its reason before its verdict instead of reasoning out of sight: its
# free tier allows only 1,000 output tokens a minute.


class SentenceVerdict(BaseModel):
    n: int
    reason: str
    verdict: Literal["supported", "partial", "unsupported"]


class FaithfulnessVerdict(BaseModel):
    sentences: list[SentenceVerdict]


class CorrectnessVerdict(BaseModel):
    reason: str
    verdict: Literal["correct", "partial", "incorrect"]


FAITHFULNESS_PROMPT = """\
You check an AI research assistant's answer against the sources it cites. For each \
numbered sentence, decide whether the text it cites supports it:
- supported: everything the sentence states is stated in, or directly follows from, \
the cited text.
- partial: the main point is supported, but the sentence adds something the cited text \
doesn't give (a number, a cause, a time, a speaker, a qualifier).
- unsupported: the cited text doesn't state it, or states something different.
Judge only against the text shown, never outside knowledge. Give a short reason first.\
"""

CORRECTNESS_PROMPT = """\
You compare an AI research assistant's answer to an analyst's question with a \
reference answer written by a person who read the source.
- correct: the answer gives the reference answer's key facts. Extra detail is fine \
unless it contradicts them.
- partial: it gives some of the key facts but misses or garbles others.
- incorrect: it misses the key facts, or contradicts them.
Wording and rounding differences don't matter. Give a short reason first.\
"""


def load_questions() -> list[dict[str, Any]]:
    retrieval = json.loads(RETRIEVAL.read_text(encoding="utf-8"))
    chat = json.loads(CHAT.read_text(encoding="utf-8"))
    by_id = {q["id"]: q for q in retrieval["questions"]}
    companies = {p["ticker"]: p["company"] for p in retrieval["passages"]}
    questions = [by_id[qid] | {"answerable": True} for qid in chat["answerable"]]
    questions += [q | {"answerable": False} for q in chat["unanswerable"]]
    return [q | {"company": companies[q["ticker"]]} for q in questions]


async def retrieve_all(questions: list[dict], k: int, embedding_model: str) -> dict[str, list]:
    from app import db
    from app.embeddings import get_embedding_client
    from app.retrieval.search import search

    conn = await db.connect()
    embeddings = get_embedding_client(embedding_model)
    try:
        await embeddings.embed([q["question"] for q in questions], "query")
        contexts = {}
        for q in questions:
            hits = await search(conn, q["question"], ticker=q["ticker"], embeddings=embeddings, k=k)
            contexts[q["id"]] = [asdict(h) for h in hits]
        return contexts
    finally:
        await conn.close()
        await embeddings.aclose()


async def judge_faithfulness(
    llm: LLMClient, question: str, result: ChatAnswer
) -> list[SentenceVerdict]:
    by_id = {h.passage_id: h for h in result.sources}
    lines, cited = [], {}
    for n, s in enumerate(result.sentences, 1):
        lines.append(f"{n}. {s.text}")
        for c in s.citations:
            lines.append(f'   cites [{c.label}]: "{c.source_text}"')
            cited[c.label] = by_id[c.passage_id]
    sources = "\n\n".join(f"[{label}] {h.title}\n{h.text}" for label, h in sorted(cited.items()))
    parsed = await llm.complete_json(
        [
            {"role": "system", "content": FAITHFULNESS_PROMPT},
            {
                "role": "user",
                "content": f"Question: {question}\n\nAnswer sentences:\n"
                + "\n".join(lines)
                + f"\n\nCited sources:\n\n{sources}",
            },
        ],
        FaithfulnessVerdict,
        model=JUDGE_MODEL,
        max_tokens=900,
        reasoning_effort=None,
    )
    return parsed.value.sentences


async def judge_correctness(llm: LLMClient, q: dict, result: ChatAnswer) -> CorrectnessVerdict:
    parsed = await llm.complete_json(
        [
            {"role": "system", "content": CORRECTNESS_PROMPT},
            {
                "role": "user",
                "content": f"Question: {q['question']}\n\nReference answer: {q['answer']}"
                f"\n\nAssistant's answer: {result.text}",
            },
        ],
        CorrectnessVerdict,
        model=JUDGE_MODEL,
        max_tokens=400,
        reasoning_effort=None,
    )
    return parsed.value


async def evaluate(
    llm: LLMClient, questions: list[dict], contexts: dict[str, list], model: str
) -> dict[str, Any]:
    """Answer every question, then judge the answers. A question whose answer or verdict
    fails (e.g. out of quota) is reported, not fatal: what succeeded is cached, so a
    re-run picks up where this one stopped."""
    answer_gate, judge_gate = asyncio.Semaphore(4), asyncio.Semaphore(1)
    errors: list[dict[str, str]] = []

    async def one(q: dict) -> dict[str, Any] | None:
        hits = [Hit(**h) for h in contexts[q["id"]]]
        try:
            async with answer_gate:
                result = await answer(llm, q["question"], hits, company=q["company"], model=model)
        except LLMError as e:
            errors.append({"id": q["id"], "step": "answer", "error": str(e)[:300]})
            return None
        verdicts: list[SentenceVerdict] = []
        correctness = None
        try:
            async with judge_gate:
                if result.answered:
                    verdicts = await judge_faithfulness(llm, q["question"], result)
                if q["answerable"] and result.answered:
                    correctness = await judge_correctness(llm, q, result)
        except LLMError as e:
            errors.append({"id": q["id"], "step": "judge", "error": str(e)[:300]})
        return {
            "id": q["id"],
            "ticker": q["ticker"],
            "answerable": q["answerable"],
            "kind": q.get("kind") or q["source"],
            "question": q["question"],
            "reference": q.get("answer"),
            "answer": result.text if result.answered else None,
            "answered": result.answered,
            "unsupported": result.unsupported,
            "gold_retrieved": q["answerable"] and any(relevant(q, h.text) for h in hits),
            "quotes": result.quotes,
            "dropped_quotes": result.dropped_quotes,
            "dropped_sentences": result.dropped_sentences,
            "sentences": [
                {
                    "text": s.text,
                    "citations": [{"label": c.label, "quote": c.source_text} for c in s.citations],
                    "verdict": next((v.verdict for v in verdicts if v.n == n), None),
                    "reason": next((v.reason for v in verdicts if v.n == n), None),
                }
                for n, s in enumerate(result.sentences, 1)
            ],
            "correctness": correctness.verdict if correctness else None,
            "correctness_reason": correctness.reason if correctness else None,
            "tokens": result.prompt_tokens + result.completion_tokens,
            "llm_ms": result.llm_ms,
        }

    rows = [r for r in await asyncio.gather(*(one(q) for q in questions)) if r is not None]
    return {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "complete": not errors,
        **summarize(rows),
        "errors": errors,
        "rows": rows,
    }


def summarize(rows: list[dict]) -> dict[str, Any]:
    yes = [r for r in rows if r["answerable"]]
    no = [r for r in rows if not r["answerable"]]
    answered = [r for r in yes if r["answered"]]
    judged = [r for r in answered if r["correctness"] is not None]
    sentences = [s for r in rows for s in r["sentences"]]
    quotes = sum(r["quotes"] for r in rows)

    def share(part: int, whole: int) -> float | None:
        return round(part / whole, 3) if whole else None

    return {
        "answerable": len(yes),
        "unanswerable": len(no),
        "answered": share(len(answered), len(yes)),
        "correct_refusals": share(sum(not r["answered"] for r in no), len(no)),
        "correct": share(sum(r["correctness"] == "correct" for r in judged), len(judged)),
        "partly_correct": share(sum(r["correctness"] == "partial" for r in judged), len(judged)),
        "faithful": share(
            sum(s["verdict"] == "supported" for s in sentences),
            sum(s["verdict"] is not None for s in sentences),
        ),
        "unjudged_sentences": sum(s["verdict"] is None for s in sentences),
        "quotes_found": share(quotes - sum(r["dropped_quotes"] for r in rows), quotes),
        "sentences_dropped": sum(len(r["dropped_sentences"]) for r in rows),
        "context_recall": share(sum(r["gold_retrieved"] for r in yes), len(yes)),
        "refusals_by_kind": {
            kind: share(
                sum(not r["answered"] for r in no if r["kind"] == kind),
                sum(r["kind"] == kind for r in no),
            )
            for kind in sorted({r["kind"] for r in no})
        },
        "tokens_per_question": round(sum(r["tokens"] for r in rows) / max(1, len(rows))),
        "llm_seconds_per_question": round(
            sum(r["llm_ms"] for r in rows) / max(1, len(rows)) / 1000, 1
        ),
    }


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{100 * v:.0f}%"


def table(results: list[dict]) -> str:
    rows = [
        "| Model | Answered | Correct | Correct refusals | Faithful sentences | Quotes found "
        "| Context recall | Tokens per question |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        rows.append(
            f"| {r['model']} | {_pct(r['answered'])} | {_pct(r['correct'])} "
            f"| {_pct(r['correct_refusals'])} | {_pct(r['faithful'])} | {_pct(r['quotes_found'])} "
            f"| {_pct(r['context_recall'])} | {r['tokens_per_question']:,} |"
        )
    return "\n".join(rows)


async def main_async(args: argparse.Namespace) -> int:
    questions = load_questions()
    if args.offline:
        contexts = json.loads(CONTEXTS.read_text(encoding="utf-8"))
    else:
        contexts = await retrieve_all(questions, DEFAULT_K, args.embeddings)
        CONTEXTS.write_text(json.dumps(contexts, ensure_ascii=False) + "\n", encoding="utf-8")
    llm = make_client(args.offline)
    try:
        results = [await evaluate(llm, questions, contexts, model) for model in args.model]
    finally:
        await llm.aclose()
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(table(results))
    print(f"\nEvery answer, verdict and reason: {RESULTS}")
    for r in results:
        for e in r["errors"]:
            print(f"{r['model']} {e['id']} {e['step']} failed: {e['error']}", file=sys.stderr)
        if not r["complete"]:
            print(f"{r['model']}: incomplete; re-run to finish from the cache", file=sys.stderr)
            return 1
    if args.readme:
        r = results[0]
        note = (
            f"{r['answerable']} answerable and {r['unanswerable']} unanswerable questions over "
            f"three companies, {DEFAULT_K} passages retrieved per question ({args.embeddings} "
            f"hybrid search), prompt v{r['prompt_version']}, judged by {JUDGE_MODEL}."
        )
        replace_block("chat", f"{table(results)}\n\n{note}")

    failed = []
    for r in results:
        for name, floor in (
            ("correct_refusals", args.min_refusal),
            ("faithful", args.min_faithful),
            ("answered", args.min_answered),
        ):
            if floor is not None and (r[name] or 0) < floor:
                failed.append(f"{r['model']} {name} {_pct(r[name])} is below {_pct(floor)}")
    for line in failed:
        print(f"FAIL: {line}", file=sys.stderr)
    return 1 if failed else 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m evals.chat")
    parser.add_argument("--model", action="append", help="repeat to compare models")
    parser.add_argument("--embeddings", default=settings.embedding_model)
    parser.add_argument("--offline", action="store_true", help="recorded responses only")
    parser.add_argument("--readme", action="store_true", help="update the README table")
    parser.add_argument("--min-refusal", type=float)
    parser.add_argument("--min-faithful", type=float)
    parser.add_argument("--min-answered", type=float)
    args = parser.parse_args()
    args.model = args.model or [settings.chat_model]
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
