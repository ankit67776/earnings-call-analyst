"""Agreement between two independent labelings of the golden passages.

Both labelings are scored with the same span matching as the eval. Positive agreement is
the F1 of one labeling against the other (symmetric); type agreement is the share of
matched claims both labelers gave the same type. Every disagreement is written out for
adjudication.

    uv run python -m evals.agreement first.json second.json --out disagreements.json
"""

import argparse
import json
import sys
from pathlib import Path

from evals.build_golden import GOLDEN
from evals.metrics import score, total
from evals.run import gold_claims


def labeled(passages: list[dict], labels: dict) -> list[dict]:
    """Golden passages carrying the claims from `labels` instead of their own."""
    claims = {p["id"]: p["claims"] for p in labels["passages"]}
    return [p | {"claims": claims.get(p["id"], [])} for p in passages]


def compare(first: dict, second: dict) -> dict:
    passages = json.loads(GOLDEN.read_text("utf-8"))["passages"]
    a, b = labeled(passages, first), labeled(passages, second)
    tallies, disagreements = [], []
    for pa, pb in zip(a, b, strict=True):
        ca = [c for c in pa["claims"] if c.get("status") != "rejected"]
        cb = [c for c in pb["claims"] if c.get("status") != "rejected"]
        tally, pairs = score(gold_claims(pb), gold_claims(pa))  # b predicted, a reference
        tallies.append(tally)
        in_a = {j for _, j in pairs}
        in_b = {i for i, _ in pairs}
        for j, c in enumerate(ca):
            if j not in in_a:
                disagreements.append({"passage": pa["id"], "kind": "only_first", "first": c})
        for i, c in enumerate(cb):
            if i not in in_b:
                disagreements.append({"passage": pa["id"], "kind": "only_second", "second": c})
        for i, j in pairs:
            if cb[i]["type"] != ca[j]["type"]:
                disagreements.append(
                    {"passage": pa["id"], "kind": "type", "first": ca[j], "second": cb[i]}
                )
    overall = total(tallies)
    rates = overall.rates()
    return {
        "first_claims": overall.gold,
        "second_claims": overall.predicted,
        "matched": overall.matched,
        "positive_agreement": round(rates["f1"] or 0, 3),
        "type_agreement": round(rates["type_accuracy"] or 0, 3),
        "disagreements": disagreements,
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m evals.agreement")
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    first = json.loads(args.first.read_text("utf-8"))
    second = json.loads(args.second.read_text("utf-8"))
    result = compare(first, second)
    kinds = [d["kind"] for d in result["disagreements"]]
    print(
        f"first {result['first_claims']} claims, second {result['second_claims']}, "
        f"matched {result['matched']}: positive agreement {result['positive_agreement']:.0%}, "
        f"type agreement {result['type_agreement']:.0%}; "
        f"{kinds.count('only_first')} only in first, {kinds.count('only_second')} only in "
        f"second, {kinds.count('type')} type differences"
    )
    if args.out:
        args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", "utf-8")


if __name__ == "__main__":
    main()
