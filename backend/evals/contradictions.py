"""Evaluate the contradiction finder on labeled pairs of call claims and 10-K passages.

The golden set (golden/contradictions.json) holds every claim pulled from the three
calls, the three 10-K passages stage 1 retrieved for each, and a label per pair, plus
seeded claims edited to contradict the 10-K. The verifier judges each claim's pairs
exactly as the app does, and its verdicts are scored against the labels. A pair counts
as flagged only as the app would show it: a contradiction verdict whose 10-K quote is
found in the passage.

- Precision: share of flagged pairs labeled contradiction.
- Recall: share of pairs labeled contradiction that were flagged.
- Seeded caught: share of seeded contradictions flagged against a passage they
  contradict. Retrieved: share whose contradicted passage stage 1 found at all.
- False alarms: share of real claims flagged although none of their pairs is labeled
  contradiction: the rate at which an analyst would be sent chasing nothing.
- Verdict agreement: share of real pairs whose verdict (contradiction, consistent or
  unrelated) matches the label.

Every response is cached in evals/cache, so --offline replays the eval without an API
key, as CI does.

    uv run python -m evals.contradictions --model openai/gpt-oss-120b --readme
    uv run python -m evals.contradictions --offline --min-precision 0.5 --min-seeded 0.7
"""

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from app.config import settings
from app.contradictions.check import PROMPT_VERSION, Claim, ClaimCheck, verify
from app.llm import LLMClient, LLMError
from app.retrieval.search import Hit
from evals.build_contradictions import GOLDEN
from evals.readme import replace_block
from evals.run import make_client

RESULTS = Path(__file__).resolve().parent / "results" / "contradictions.json"


async def evaluate(llm: LLMClient, golden: dict, model: str, limit: int | None) -> dict[str, Any]:
    """Judge every claim's pairs. A claim whose verification fails (e.g. out of quota) is
    reported, not fatal: what succeeded is cached, so a re-run picks up from there."""
    gate = asyncio.Semaphore(2)
    filings = {int(k): v for k, v in golden["filings"].items()}
    errors: list[dict[str, str]] = []
    # Seeded claims first: a run cut short by the daily quota still measures recall.
    claims = sorted(golden["claims"], key=lambda c: not c["seeded"])[:limit]

    async def one(c: dict) -> dict[str, Any] | None:
        call = golden["calls"][c["ticker"]]
        claim = Claim(c["type"], c["claim"], c["speaker"], c["quote"])
        hits = [Hit(**h) for h in c["candidates"]]
        try:
            async with gate:
                check = await verify(
                    llm, claim, hits, company=call["company"], call=call["call"],
                    filings=filings, model=model,
                )  # fmt: skip
        except LLMError as e:
            errors.append({"id": c["id"], "error": str(e)[:300]})
            return None
        return row(c, check)

    rows = [r for r in await asyncio.gather(*(one(c) for c in claims)) if r is not None]
    return {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "complete": not errors and len(rows) == len(golden["claims"]),
        **summarize(rows),
        "errors": errors,
        "rows": rows,
    }


def row(c: dict, check: ClaimCheck) -> dict[str, Any]:
    labels = {x["rank"]: x for x in c.get("labels", [])}
    return {
        "id": c["id"],
        "ticker": c["ticker"],
        "seeded": c["seeded"],
        "claim": c["claim"],
        "quote": c["quote"],
        "pairs": [
            {
                "rank": p.rank,
                "title": p.hit.title,
                "label": labels.get(p.rank, {}).get("label"),
                "gold_quote": labels.get(p.rank, {}).get("quote"),
                "verdict": p.verdict,
                "flagged": p.flagged,
                "reason": p.reason,
                "filing_quote": p.filing_quote,
                "match": p.match,
            }
            for p in check.pairs
        ],
        "tokens": check.prompt_tokens + check.completion_tokens,
        "llm_ms": check.llm_ms,
    }


def summarize(rows: list[dict]) -> dict[str, Any]:
    pairs = [(r, p) for r in rows for p in r["pairs"]]
    labeled = [(r, p) for r, p in pairs if p["label"] is not None]
    real = [(r, p) for r, p in labeled if not r["seeded"]]
    flagged = [p for _, p in labeled if p["flagged"]]
    gold = [p for _, p in labeled if p["label"] == "contradiction"]
    real_rows = [r for r in rows if not r["seeded"]]
    seeded = [r for r in rows if r["seeded"]]
    quoted = [p for _, p in pairs if p["verdict"] in ("contradiction", "consistent")]

    def share(part: int, whole: int) -> float | None:
        return round(part / whole, 3) if whole else None

    def any_pair(r: dict, test) -> bool:
        return any(test(p) for p in r["pairs"])

    def caught(p: dict) -> bool:
        return p["flagged"] and p["label"] == "contradiction"

    return {
        "claims": len(rows),
        "real_claims": len(real_rows),
        "seeded_claims": len(seeded),
        "labeled_pairs": len(labeled),
        "gold_contradictions": len(gold),
        "real_gold_contradictions": sum(p["label"] == "contradiction" for _, p in real),
        "flagged": len(flagged),
        "real_flagged": sum(p["flagged"] for _, p in real),
        "precision": share(sum(caught(p) for p in flagged), len(flagged)),
        "recall": share(sum(caught(p) for p in gold), len(gold)),
        "real_precision": share(
            sum(caught(p) for _, p in real),
            sum(p["flagged"] for _, p in real),
        ),
        "seeded_caught": share(
            sum(any_pair(r, caught) for r in seeded),
            len(seeded),
        ),
        "seeded_retrieved": share(
            sum(any_pair(r, lambda p: p["label"] == "contradiction") for r in seeded), len(seeded)
        ),
        "false_alarms": share(
            sum(
                any_pair(r, lambda p: p["flagged"])
                and not any_pair(r, lambda p: p["label"] == "contradiction")
                for r in real_rows
                if any(p["label"] is not None for p in r["pairs"])
            ),
            sum(any(p["label"] is not None for p in r["pairs"]) for r in real_rows),
        ),
        "verdict_agreement": share(sum(p["verdict"] == p["label"] for _, p in real), len(real)),
        # Label -> verdict counts over real pairs (verdict None: the verifier skipped it).
        "confusion": {
            label: dict(Counter(str(p["verdict"]) for _, p in real if p["label"] == label))
            for label in ("contradiction", "consistent", "unrelated")
        },
        "verdicts": dict(Counter(str(p["verdict"]) for _, p in pairs)),
        "unverified_contradictions": sum(
            p["verdict"] == "contradiction" and not p["flagged"] for _, p in pairs
        ),
        "quotes_found": share(sum(p["match"] is not None for p in quoted), len(quoted)),
        "tokens_per_claim": round(sum(r["tokens"] for r in rows) / max(1, len(rows))),
        "llm_seconds_per_claim": round(
            sum(r["llm_ms"] for r in rows) / max(1, len(rows)) / 1000, 1
        ),
    }


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{100 * v:.0f}%"


def table(results: list[dict]) -> str:
    rows = [
        "| Verifier | Precision | Recall | Seeded caught | False alarms | Verdict agreement "
        "| 10-K quotes found | Tokens per claim |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        rows.append(
            f"| {r['model']} | {_pct(r['precision'])} ({r['flagged']} flagged) "
            f"| {_pct(r['recall'])} | {_pct(r['seeded_caught'])} | {_pct(r['false_alarms'])} "
            f"| {_pct(r['verdict_agreement'])} | {_pct(r['quotes_found'])} "
            f"| {r['tokens_per_claim']:,} |"
        )
    return "\n".join(rows)


async def main_async(args: argparse.Namespace) -> int:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    llm = make_client(args.offline)
    try:
        results = [await evaluate(llm, golden, model, args.limit) for model in args.model]
    finally:
        await llm.aclose()
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(table(results))
    print(f"\nEvery verdict and reason: {RESULTS}")
    for r in results:
        for e in r["errors"]:
            print(f"{r['model']} {e['id']} failed: {e['error']}", file=sys.stderr)
        if not r["complete"]:
            print(f"{r['model']}: incomplete; re-run to finish from the cache", file=sys.stderr)
            return 1
    if args.readme:
        r = results[0]
        real_pairs = sum(len(c["candidates"]) for c in golden["claims"] if not c["seeded"])
        note = (
            f"{r['real_claims']} claims from three Q3 2020 calls, each judged against the "
            f"top {golden['k']} 10-K passages retrieved for it ({real_pairs} labeled pairs, "
            f"{r['real_gold_contradictions']} real contradictions), plus {r['seeded_claims']} "
            f"seeded contradictions ({r['labeled_pairs'] - real_pairs} pairs); "
            f"prompt v{r['prompt_version']}."
        )
        replace_block("contradictions", f"{table(results)}\n\n{note}")

    failed = []
    for r in results:
        for name, floor in (
            ("precision", args.min_precision),
            ("seeded_caught", args.min_seeded),
            ("verdict_agreement", args.min_agreement),
        ):
            if floor is not None and (r[name] or 0) < floor:
                failed.append(f"{r['model']} {name} {_pct(r[name])} is below {_pct(floor)}")
    for line in failed:
        print(f"FAIL: {line}", file=sys.stderr)
    return 1 if failed else 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m evals.contradictions")
    parser.add_argument("--model", action="append", help="repeat to compare verifiers")
    parser.add_argument("--offline", action="store_true", help="recorded responses only")
    parser.add_argument("--readme", action="store_true", help="update the README table")
    parser.add_argument("--limit", type=int, help="only the first N claims (a quick look)")
    parser.add_argument("--min-precision", type=float)
    parser.add_argument("--min-seeded", type=float)
    parser.add_argument("--min-agreement", type=float)
    args = parser.parse_args()
    args.model = args.model or [settings.verifier_model]
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
