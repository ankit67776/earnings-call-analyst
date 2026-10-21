"""Build the contradiction golden set: claims from the three calls, each with the 10-K
passages stage 1 retrieves for it, and labels for every claim-passage pair.

    uv run python -m evals.build_contradictions claims   # record claims + candidates (DB)
    uv run python -m evals.build_contradictions seeds    # add seeded contradictions (DB)
    uv run python -m evals.build_contradictions merge    # labels from labelers + adjudication

`claims` takes every claim stored for the calls (python -m app.contradictions claims)
and records its candidates: passages can be re-cut and searches retuned, but the labels
belong to these exact pairs. Two annotators label every pair on their own
(labelers/contradictions_{a,b}.json) and a third pass settles disagreements
(contradictions_adjudication.json).

Real contradictions between a call and a 10-K are rare, so recall is measured on seeded
ones too: a fourth annotator takes pairs both annotators called consistent and edits
the call's claim and quote to contradict the 10-K quote, changing one fact
(labelers/contradictions_seeds.json). `seeds` retrieves candidates for each edited claim
the same way, and the two annotators label those pairs like the real ones: a 10-K can
state the same fact in several passages, so a seed may contradict more than the one it
was written against.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.config import settings
from app.contradictions.check import DEFAULT_K, Claim, candidates, describe_filing

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden" / "contradictions.json"
LABELERS = GOLDEN.parent / "labelers"
ADJUDICATION = GOLDEN.parent / "contradictions_adjudication.json"
SEEDS = LABELERS / "contradictions_seeds.json"
LABELS = ("contradiction", "consistent", "unrelated")


async def retrieve(conn, embeddings, claims: list[dict], k: int) -> None:
    """Fill in each claim's candidates, as the app's stage 1 finds them."""
    await embeddings.embed([c["claim"] for c in claims], "query")
    for c in claims:
        claim = Claim(c["type"], c["claim"], c["speaker"], c["quote"])
        hits = await candidates(conn, embeddings, claim, ticker=c["ticker"], k=k)
        c["candidates"] = [asdict(h) for h in hits]


async def record_claims(k: int, force: bool) -> None:
    from app import db
    from app.embeddings import get_embedding_client

    if GOLDEN.exists() and not force:
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        if any("labels" in c for c in golden["claims"]):
            sys.exit(f"{GOLDEN} already has labels; pass --force to rebuild it")
    conn = await db.connect()
    embeddings = get_embedding_client()
    try:
        calls = await conn.fetch(
            "select id, ticker, company, fiscal_year, quarter from calls order by ticker"
        )
        rows = await conn.fetch(
            "select call_id, position, type, claim, speaker, quote, at_s, model, prompt_version"
            " from call_claims order by call_id, position"
        )
        by_call = {c["id"]: c for c in calls}
        claims = [
            {
                "id": f"{by_call[r['call_id']]['ticker']}-{r['position']:02d}",
                "ticker": by_call[r["call_id"]]["ticker"],
                "seeded": False,
                "type": r["type"],
                "claim": r["claim"],
                "speaker": r["speaker"],
                "quote": r["quote"],
                "at": r["at_s"],
            }
            for r in rows
        ]
        await retrieve(conn, embeddings, claims, k)
        documents = {h["document_id"] for c in claims for h in c["candidates"]}
        filings = await conn.fetch(
            "select id, form, fiscal_year, filed from documents where id = any($1::bigint[])",
            list(documents),
        )
        extraction = {(r["model"], r["prompt_version"]) for r in rows}
    finally:
        await conn.close()
        await embeddings.aclose()
    [(model, version)] = extraction
    golden = {
        "version": 1,
        "k": k,
        "embedding_model": settings.embedding_model,
        "claims_model": model,
        "claims_prompt_version": version,
        "calls": {
            c["ticker"]: {
                "call_id": c["id"],
                "company": c["company"],
                "call": f"Q{c['quarter']} {c['fiscal_year']} earnings call",
            }
            for c in calls
        },
        "filings": {str(r["id"]): describe_filing(r) for r in filings},
        "claims": sorted(claims, key=lambda c: c["id"]),
    }
    write(golden)
    pairs = sum(len(c["candidates"]) for c in claims)
    print(f"{len(claims)} claims, {pairs} pairs -> {GOLDEN}")


async def add_seeds() -> None:
    from app import db
    from app.embeddings import get_embedding_client

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    data = json.loads(SEEDS.read_text(encoding="utf-8"))
    originals = {c["id"]: c for c in golden["claims"] if not c["seeded"]}
    seeded = []
    for n, s in enumerate(data["seeds"], 1):
        original = originals[s["from"]]
        passage = original["candidates"][s["rank"] - 1]["text"]
        for quote in s["contradicts"]:
            if quote not in passage and not any(quote in h["text"] for h in original["candidates"]):
                raise ValueError(f"seed {n}: 10-K quote not in {s['from']}'s candidates: {quote}")
        seeded.append(
            {
                "id": f"{original['ticker']}-S{n:02d}",
                "ticker": original["ticker"],
                "seeded": True,
                "from": s["from"],
                "edit": s["edit"],
                "type": original["type"],
                "claim": s["claim"],
                "speaker": original["speaker"],
                "quote": s["quote"],
                "at": original["at"],
                "contradicts": s["contradicts"],
                "why": s["why"],
            }
        )
    conn = await db.connect()
    embeddings = get_embedding_client(golden["embedding_model"])
    try:
        await retrieve(conn, embeddings, seeded, golden["k"])
    finally:
        await conn.close()
        await embeddings.aclose()
    golden["claims"] = [c for c in golden["claims"] if not c["seeded"]] + seeded
    write(golden)
    found = sum(
        any(q in h["text"] for h in c["candidates"] for q in c["contradicts"]) for c in seeded
    )
    print(f"{len(seeded)} seeded claims; the contradicted passage was retrieved for {found}")


def load_labels(path: Path) -> dict[tuple[str, int], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {(x["claim"], x["rank"]): x for x in data["labels"]}


def check_labels(path: Path, ticker: str | None = None, seeded: bool = False) -> list[str]:
    """Problems with a labels file for the real (or seeded) claims: missing or unknown
    pairs, unknown labels, quotes that aren't in their passage. Empty when it's good."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    claims = {
        c["id"]: c
        for c in golden["claims"]
        if c["seeded"] == seeded and (ticker is None or c["ticker"] == ticker)
    }
    labels = load_labels(path)
    problems = []
    for cid, c in claims.items():
        for rank, hit in enumerate(c["candidates"], 1):
            x = labels.get((cid, rank))
            if x is None:
                problems.append(f"{cid} #{rank}: no label")
            elif x["label"] not in LABELS:
                problems.append(f"{cid} #{rank}: unknown label {x['label']!r}")
            elif x["label"] != "unrelated" and not x.get("quote"):
                problems.append(f"{cid} #{rank}: {x['label']} needs the passage's quote")
            elif x.get("quote") and x["quote"] not in hit["text"]:
                problems.append(f"{cid} #{rank}: quote not found verbatim in the passage")
    problems += [f"{k[0]} #{k[1]}: not a pair in the set" for k in labels if k[0] not in claims]
    return problems


def check_seeds(path: Path) -> list[str]:
    """Problems with a seeds file: unknown claims, missing fields, 10-K quotes that aren't
    in the seeded claim's original candidates."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    claims = {c["id"]: c for c in golden["claims"] if not c["seeded"]}
    problems = []
    for n, s in enumerate(json.loads(path.read_text(encoding="utf-8"))["seeds"], 1):
        c = claims.get(s.get("from"))
        if c is None:
            problems.append(f"seed {n}: no claim {s.get('from')!r}")
            continue
        fields = ("rank", "edit", "claim", "quote", "contradicts", "why")
        missing = [k for k in fields if not s.get(k)]
        if missing:
            problems.append(f"seed {n}: missing {', '.join(missing)}")
            continue
        texts = [h["text"] for h in c["candidates"]]
        for quote in s["contradicts"]:
            if not any(quote in t for t in texts):
                problems.append(f"seed {n}: not found verbatim in {s['from']}'s passages: {quote}")
    return problems


def merge() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    a = load_labels(LABELERS / "contradictions_a.json")
    b = load_labels(LABELERS / "contradictions_b.json")
    decided = load_labels(ADJUDICATION) if ADJUDICATION.exists() else {}
    status = Counter()
    for c in golden["claims"]:
        labels = []
        for rank, hit in enumerate(c["candidates"], 1):
            key = (c["id"], rank)
            if key not in a or key not in b:
                raise ValueError(f"{key} is missing a label")
            if a[key]["label"] == b[key]["label"]:
                gold, state = a[key], "agreed"
            elif key in decided:
                gold, state = decided[key], "adjudicated"
            else:
                raise ValueError(f"{key}: annotators disagree and there's no adjudication")
            if gold["label"] not in LABELS:
                raise ValueError(f"{key}: unknown label {gold['label']!r}")
            quote = gold.get("quote") or ""
            if gold["label"] != "unrelated" and quote not in hit["text"]:
                raise ValueError(f"{key}: quote not in the passage: {gold['quote']}")
            status[state] += 1
            labels.append({"rank": rank, "label": gold["label"], "quote": quote, "status": state})
        c["labels"] = labels
    write(golden)
    for seeded, name in ((False, "real"), (True, "seeded")):
        gold = Counter(
            x["label"] for c in golden["claims"] if c["seeded"] == seeded for x in c["labels"]
        )
        print(f"{name} pairs: {dict(gold)}")
    print(dict(status))


def agreement() -> dict[str, Any]:
    """How often the two annotators agree, on the three labels and on contradiction or
    not, before adjudication."""
    a = load_labels(LABELERS / "contradictions_a.json")
    b = load_labels(LABELERS / "contradictions_b.json")
    keys = sorted(a.keys() & b.keys())
    same = sum(a[k]["label"] == b[k]["label"] for k in keys)
    flag_a = {k for k in keys if a[k]["label"] == "contradiction"}
    flag_b = {k for k in keys if b[k]["label"] == "contradiction"}
    both = len(flag_a & flag_b)
    return {
        "pairs": len(keys),
        "agreement": round(same / len(keys), 3) if keys else None,
        "contradictions_a": len(flag_a),
        "contradictions_b": len(flag_b),
        "contradictions_both": both,
        "disagreements": [
            {"claim": k[0], "rank": k[1], "a": a[k]["label"], "b": b[k]["label"]}
            for k in keys
            if a[k]["label"] != b[k]["label"]
        ],
    }


def write(golden: dict) -> None:
    GOLDEN.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m evals.build_contradictions")
    commands = parser.add_subparsers(dest="command", required=True)
    claims = commands.add_parser("claims", help="record stored call claims and candidates")
    claims.add_argument("-k", type=int, default=DEFAULT_K)
    claims.add_argument("--force", action="store_true")
    commands.add_parser("seeds", help="add seeded contradictions and their candidates")
    commands.add_parser("merge", help="write gold labels from the labelers' files")
    commands.add_parser("agreement", help="compare the two annotators")
    check = commands.add_parser("check", help="validate a labels file")
    check.add_argument("path", type=Path)
    check.add_argument("--ticker", type=str.upper)
    check.add_argument("--seeded", action="store_true", help="the seeded claims' pairs")
    args = parser.parse_args()
    if args.command == "claims":
        asyncio.run(record_claims(args.k, args.force))
    elif args.command == "seeds":
        asyncio.run(add_seeds())
    elif args.command == "merge":
        merge()
    elif args.command == "check":
        if args.path.name.endswith("_seeds.json"):
            problems = check_seeds(args.path)
        else:
            problems = check_labels(args.path, args.ticker, args.seeded)
        print("\n".join(problems) or "ok")
        sys.exit(1 if problems else 0)
    else:
        print(json.dumps(agreement(), indent=1))


if __name__ == "__main__":
    main()
