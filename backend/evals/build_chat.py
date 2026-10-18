"""Assemble the chat golden set: answerable questions drawn from the retrieval golden set
(they come with a reference answer and quote), plus unanswerable ones written to sit
close to what the sources discuss (labelers/unanswerable_*.json).

    uv run python -m evals.build_chat
"""

import json
import random
import sys
from pathlib import Path

from evals.build_retrieval import GOLDEN as RETRIEVAL

CHAT = Path(__file__).resolve().parent / "golden" / "chat.json"
LABELERS = CHAT.parent / "labelers"
# Answerable questions per (company, source): enough to measure, within one day's quota.
PER_GROUP = 5
SEED = 2022


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    retrieval = json.loads(RETRIEVAL.read_text(encoding="utf-8"))
    groups: dict[tuple[str, str], list[str]] = {}
    for q in retrieval["questions"]:
        groups.setdefault((q["ticker"], q["source"]), []).append(q["id"])
    rng = random.Random(SEED)
    answerable = [
        qid for key in sorted(groups) for qid in sorted(rng.sample(groups[key], PER_GROUP))
    ]

    unanswerable = []
    for path in sorted(LABELERS.glob("unanswerable_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        counts: dict[str, int] = {}
        for q in data["questions"]:
            counts[q["ticker"]] = counts.get(q["ticker"], 0) + 1
            unanswerable.append(
                {
                    "id": f"{q['ticker']}-none-{counts[q['ticker']]:02d}-{data['annotator']}",
                    "ticker": q["ticker"],
                    "kind": q["kind"],
                    "question": q["question"],
                    "near_misses": q.get("near_misses", []),
                    "why": q["why"],
                }
            )
    data = {"version": 1, "seed": SEED, "answerable": answerable, "unanswerable": unanswerable}
    CHAT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(answerable)} answerable, {len(unanswerable)} unanswerable -> {CHAT}")


if __name__ == "__main__":
    main()
