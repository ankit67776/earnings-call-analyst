"""Build the golden claims from two independent labelings and their adjudication.

golden/labelers/a.json and b.json each label every passage. Claims both labelers found
(by the eval's span matching) are kept as "agreed", with the other labeler's quote as an
alternative span. Disagreements were settled by a third pass, recorded with reasons in
golden/adjudication.json: kept claims become "adjudicated", duplicates become
alternative quotes of the claim they repeat, and dropped ones are left out.

    uv run python -m evals.merge_labels
"""

import json
import sys

from evals.agreement import compare, labeled
from evals.build_golden import GOLDEN
from evals.metrics import score
from evals.review import check
from evals.run import gold_claims

LABELERS = GOLDEN.parent / "labelers"
ADJUDICATION = GOLDEN.parent / "adjudication.json"


def disputes(passages: list[dict], a: dict, b: dict) -> list[dict]:
    """Every disagreement, in a fixed order, identified by its content."""
    out = []
    for pa, pb in zip(labeled(passages, a), labeled(passages, b), strict=True):
        _, pairs = score(gold_claims(pb), gold_claims(pa))
        for i, j in pairs:
            ca, cb = pa["claims"][j], pb["claims"][i]
            if ca["type"] != cb["type"]:
                out.append(_item(pa["id"], "type", ca, types=sorted({ca["type"], cb["type"]})))
        in_a, in_b = {j for _, j in pairs}, {i for i, _ in pairs}
        out += [_item(pa["id"], "include", c) for j, c in enumerate(pa["claims"]) if j not in in_a]
        out += [_item(pa["id"], "include", c) for i, c in enumerate(pb["claims"]) if i not in in_b]
    for n, item in enumerate(out, 1):
        item["id"] = f"D{n}"
    return out


def _item(passage: str, question: str, claim: dict, **extra) -> dict:
    keys = ("type", "paragraph_id", "quote", "claim")
    return {"passage": passage, "question": question, **{k: claim[k] for k in keys}, **extra}


def _key(item: dict, question: str, passage: str | None = None) -> tuple[str, ...]:
    """Identifies a disagreement by passage, question and quoted span."""
    return (passage or item["passage"], question, item["paragraph_id"], item["quote"])


def _span(claim: dict) -> dict:
    return {"paragraph_id": claim["paragraph_id"], "quote": claim["quote"]}


def _add_alt(claim: dict, span: dict) -> None:
    if span == _span(claim):
        return
    alts = claim.setdefault("alt_quotes", [])
    if span not in alts:
        alts.append(span)


def merge(passages: list[dict], a: dict, b: dict, decisions: dict[str, dict]) -> list[dict]:
    items = {_key(d, d["question"]): d for d in decisions.values()}
    merged = []
    for pa, pb in zip(labeled(passages, a), labeled(passages, b), strict=True):
        _, pairs = score(gold_claims(pb), gold_claims(pa))
        claims: list[dict] = []
        agreed: dict[int, dict] = {}  # agreed claims, numbered as the adjudicator saw them
        kept: dict[str, dict] = {}  # adjudicated claims, by dispute id
        for i, j in pairs:
            ca, cb = pa["claims"][j], pb["claims"][i]
            claim = {k: ca[k] for k in ("type", "paragraph_id", "quote", "claim")}
            for span in [_span(cb), *ca.get("alt_quotes", []), *cb.get("alt_quotes", [])]:
                _add_alt(claim, span)
            if ca["type"] == cb["type"]:
                claim["status"] = "agreed"
                agreed[len(agreed) + 1] = claim
            else:
                d = items[_key(ca, "type", pa["id"])]
                claim |= {"type": d["type_decided"], "status": "adjudicated"}
                kept[d["id"]] = claim
            claims.append(claim)
        later: list[tuple[dict, dict]] = []
        in_a, in_b = {j for _, j in pairs}, {i for i, _ in pairs}
        loose = [c for j, c in enumerate(pa["claims"]) if j not in in_a]
        loose += [c for i, c in enumerate(pb["claims"]) if i not in in_b]
        for c in loose:
            d = items[_key(c, "include", pa["id"])]
            if d["decision"] == "keep":
                claim = {k: c[k] for k in ("paragraph_id", "quote", "claim")}
                claim = {"type": d["type_decided"], **claim, "status": "adjudicated"}
                for span in c.get("alt_quotes", []):
                    _add_alt(claim, span)
                kept[d["id"]] = claim
                claims.append(claim)
            elif d["decision"] == "alt_of":
                later.append((d, c))
        for d, c in later:
            if "alt_of_agreed" in d:
                target = agreed[d["alt_of_agreed"]]
            else:
                target = kept[d["alt_of_disputed"]]
            for span in [_span(c), *c.get("alt_quotes", [])]:
                _add_alt(target, span)
        for claim in claims:
            if not claim.get("alt_quotes"):
                claim.pop("alt_quotes", None)
        merged.append(pa | {"claims": claims})
    return merged


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    golden = json.loads(GOLDEN.read_text("utf-8"))
    a = json.loads((LABELERS / "a.json").read_text("utf-8"))
    b = json.loads((LABELERS / "b.json").read_text("utf-8"))
    record = json.loads(ADJUDICATION.read_text("utf-8"))
    decisions = {d["id"]: d for d in record["decisions"]}
    expected = disputes(golden["passages"], a, b)
    if [_key(d, d["question"]) for d in expected] != [
        _key(d, d["question"]) for d in decisions.values()
    ]:
        sys.exit("adjudication.json doesn't cover the current disagreements; re-adjudicate")
    golden["passages"] = merge(golden["passages"], a, b, decisions)
    golden["labeling"] = {k: v for k, v in compare(a, b).items() if k != "disagreements"} | {
        "disagreements": len(expected)
    }
    problems = check(golden)
    if problems:
        sys.exit("\n".join(problems))
    GOLDEN.write_text(json.dumps(golden, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    claims = [c for p in golden["passages"] for c in p["claims"]]
    statuses = {s: sum(c["status"] == s for c in claims) for s in ("agreed", "adjudicated")}
    print(f"{len(claims)} golden claims: {statuses}")


if __name__ == "__main__":
    main()
