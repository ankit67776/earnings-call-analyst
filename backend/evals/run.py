"""Run the claim extractor over the golden set and score it.

Each golden passage goes through the same map, ground and reduce steps as a filing,
as one chunk. Responses are cached in evals/cache, which is committed: `--offline`
replays them without an API key (as CI does), and a prompt or model change shows up
as a cache miss that needs a live run.

    uv run python -m evals.run                       # analysis model, live, cached
    uv run python -m evals.run --model openai/gpt-oss-20b --model openai/gpt-oss-120b
    uv run python -m evals.run --offline --min-recall 0.6   # CI gate
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.analysis.chunking import Chunk
from app.analysis.extract import PROMPT_VERSION, SectionAnalysis, analyze_chunks
from app.config import settings
from app.filings.tenk import Paragraph
from app.llm import LLMClient, LLMError
from app.llm.cache import DiskCache
from app.llm.ratelimit import RateLimiter
from app.llm.types import CompletionRequest, ProviderResult
from evals.build_golden import GOLDEN
from evals.metrics import Claim, Span, Tally, score, total

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
RESULTS = HERE / "results" / "extraction.json"
README = HERE.parent.parent / "README.md"
START, END = "<!-- eval:extraction -->", "<!-- /eval:extraction -->"


class Replay:
    """Stands in for the provider when only cached responses may be used."""

    name = "groq"  # cache keys include the provider that produced the response

    def limits(self, model: str):
        raise LLMError("offline")

    async def complete(self, request: CompletionRequest) -> ProviderResult:
        raise LLMError(
            f"{request.model}: no cached response in evals/cache for this request. "
            "Run the eval live once to record it."
        )

    async def aclose(self) -> None:
        pass


def make_client(offline: bool) -> LLMClient:
    if offline:
        return LLMClient(Replay(), default_model=settings.analysis_model, cache=DiskCache(CACHE))
    from app.llm.providers.groq import GroqProvider

    provider = GroqProvider(settings.groq_api_key)
    usage = Path(settings.llm_cache_dir) / "usage.json"
    return LLMClient(
        provider,
        default_model=settings.analysis_model,
        limiter=RateLimiter(provider.limits, usage_file=usage),
        cache=DiskCache(CACHE),
    )


def gold_claims(passage: dict) -> list[Claim]:
    texts = {p["id"]: p["text"] for p in passage["paragraphs"]}
    claims = []
    for c in passage["claims"]:
        if c.get("status") == "rejected":
            continue
        spans = []
        for at in [
            {"paragraph_id": c["paragraph_id"], "quote": c["quote"]},
            *c.get("alt_quotes", []),
        ]:
            start = texts[at["paragraph_id"]].index(at["quote"])
            spans.append(Span(at["paragraph_id"], start, start + len(at["quote"])))
        claims.append(Claim(c["type"], tuple(spans)))
    return claims


def predicted_claims(analysis: SectionAnalysis) -> list[Claim]:
    return [
        Claim(
            str(f.type),
            tuple(Span(c.paragraph_id, c.start, c.end) for c in f.citations if c.start is not None),
        )
        for f in analysis.findings
    ]


def as_chunk(passage: dict) -> Chunk:
    paragraphs = [
        Paragraph(p["id"], passage["section"], i, p["kind"], p["text"])
        for i, p in enumerate(passage["paragraphs"], 1)
    ]
    return Chunk(
        passage["section"], passage["section_title"], paragraphs, passage["context_heading"]
    )


async def evaluate(client: LLMClient, golden: dict, model: str) -> dict[str, Any]:
    async def one(passage: dict) -> dict[str, Any]:
        analysis = await analyze_chunks(
            client,
            passage["section"],
            [as_chunk(passage)],
            company=passage["company"],
            model=model,
        )
        if analysis.failed_chunks:
            raise LLMError(f"{passage['id']}: {analysis.failed_chunks[0][1]}")
        gold = gold_claims(passage)
        predicted = predicted_claims(analysis)
        tally, pairs = score(predicted, gold)
        quotes = sum(r.claims for r in analysis.chunk_results)
        tally.quotes, tally.found = quotes, quotes - len(analysis.rejected)
        tally.misattributed = analysis.misattributed
        matched_p = {i for i, _ in pairs}
        matched_g = {j for _, j in pairs}
        live = [c for c in passage["claims"] if c["status"] != "rejected"]
        return {
            "id": passage["id"],
            "tally": tally,
            "prompt_tokens": analysis.prompt_tokens,
            "completion_tokens": analysis.completion_tokens,
            "latency_ms": sum(r.latency_ms for r in analysis.chunk_results),
            # For error analysis: what the model added and what it missed.
            "extra": [
                {"type": str(f.type), "claim": f.claim, "quote": f.citations[0].quote}
                for i, f in enumerate(analysis.findings)
                if i not in matched_p
            ],
            "missed": [
                {"type": c["type"], "claim": c["claim"], "quote": c["quote"]}
                for j, c in enumerate(live)
                if j not in matched_g
            ],
            "dropped": [
                {"type": str(f.type), "quote": f.citations[0].quote} for f in analysis.rejected
            ],
            "type_mismatches": [
                {"gold": gold[j].type, "predicted": predicted[i].type, "claim": live[j]["claim"]}
                for i, j in pairs
                if gold[j].type != predicted[i].type
            ],
        }

    passages = await asyncio.gather(*(one(p) for p in golden["passages"]))
    overall = total(p["tally"] for p in passages)
    by_company: dict[str, Tally] = {}
    by_section: dict[str, Tally] = {}
    for golden_passage, result in zip(golden["passages"], passages, strict=True):
        by_company.setdefault(golden_passage["ticker"], Tally()).add(result["tally"])
        by_section.setdefault(golden_passage["section"], Tally()).add(result["tally"])
    statuses = [c["status"] for p in golden["passages"] for c in p["claims"]]
    return {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "passages": len(passages),
        "gold_claims": overall.gold,
        "unreviewed_gold_claims": statuses.count("draft"),
        "predicted_claims": overall.predicted,
        **_rounded(overall.rates()),
        "by_type": {
            k: {
                "gold": t.gold,
                "predicted": t.predicted,
                **_rounded(t.rates(), "precision", "recall"),
            }
            for k, t in sorted(overall.by_type.items())
        },
        "by_company": {
            k: _rounded(t.rates(), "precision", "recall") for k, t in by_company.items()
        },
        "by_section": {
            k: _rounded(t.rates(), "precision", "recall") for k, t in by_section.items()
        },
        "prompt_tokens": sum(p["prompt_tokens"] for p in passages),
        "completion_tokens": sum(p["completion_tokens"] for p in passages),
        "llm_latency_s": round(sum(p["latency_ms"] for p in passages) / 1000, 1),
        "errors": [
            {
                "passage": p["id"],
                **{k: p[k] for k in ("extra", "missed", "dropped", "type_mismatches")},
            }
            for p in passages
            if p["extra"] or p["missed"] or p["dropped"] or p["type_mismatches"]
        ],
    }


def _rounded(rates: dict[str, float | None], *keys: str) -> dict[str, float | None]:
    return {
        k: None if v is None else round(v, 3) for k, v in rates.items() if not keys or k in keys
    }


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{100 * v:.0f}%"


def table(results: list[dict]) -> str:
    rows = [
        "| Model | Precision | Recall | F1 | Type accuracy | Quotes found | Right paragraph "
        "| Tokens per passage | LLM seconds per passage |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        tokens = (r["prompt_tokens"] + r["completion_tokens"]) / r["passages"]
        rows.append(
            f"| {r['model']} | {_pct(r['precision'])} | {_pct(r['recall'])} | {_pct(r['f1'])} "
            f"| {_pct(r['type_accuracy'])} | {_pct(r['grounding_rate'])} "
            f"| {_pct(r['citation_accuracy'])} | {tokens:,.0f} "
            f"| {r['llm_latency_s'] / r['passages']:.1f} |"
        )
    return "\n".join(rows)


def update_readme(results: list[dict], golden: dict) -> None:
    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        raise SystemExit(f"README.md has no {START} ... {END} block")
    r = results[0]
    note = (
        f"{r['passages']} passages from three FY2020 10-Ks, {r['gold_claims']} labeled claims, "
        f"prompt v{r['prompt_version']}."
    )
    if r["unreviewed_gold_claims"]:
        note += f" {r['unreviewed_gold_claims']} labels are still unreviewed drafts."
    block = f"{START}\n{table(results)}\n\n{note}\n{END}"
    before, rest = text.split(START, 1)
    after = rest.split(END, 1)[1]
    README.write_text(before + block + after, encoding="utf-8")


async def main_async(args: argparse.Namespace) -> int:
    golden = json.loads(GOLDEN.read_text("utf-8"))
    client = make_client(args.offline)
    try:
        results = [await evaluate(client, golden, model) for model in args.model]
    finally:
        await client.aclose()

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(table(results))
    print(f"\nDetails, including every miss and extra claim: {RESULTS}")
    if args.readme:
        update_readme(results, golden)

    failed = []
    for r in results:
        for name, floor in (
            ("precision", args.min_precision),
            ("recall", args.min_recall),
            ("grounding_rate", args.min_grounding),
        ):
            if floor is not None and (r[name] or 0) < floor:
                failed.append(f"{r['model']} {name} {_pct(r[name])} is below {_pct(floor)}")
    for line in failed:
        print(f"FAIL: {line}", file=sys.stderr)
    return 1 if failed else 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("--model", action="append", help="repeat to compare models")
    parser.add_argument("--offline", action="store_true", help="cached responses only")
    parser.add_argument("--readme", action="store_true", help="update the README table")
    parser.add_argument("--min-precision", type=float)
    parser.add_argument("--min-recall", type=float)
    parser.add_argument("--min-grounding", type=float)
    args = parser.parse_args()
    args.model = args.model or [settings.analysis_model]
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
