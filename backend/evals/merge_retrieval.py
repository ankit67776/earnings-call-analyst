"""Build the retrieval golden questions from the annotators' and judges' files.

Each annotator's question keeps its quote, which must appear verbatim in the sampled
passage. Pooled judging (labelers/judgments_*.json) found other passages that answer a
question too; their quotes are added as alternatives, so retrieving any of them counts.
Questions dropped on review are listed with the reason in retrieval_adjudication.json.

    uv run python -m evals.merge_retrieval
"""

import json
import sys
from pathlib import Path

from evals.build_retrieval import GOLDEN

LABELERS = GOLDEN.parent / "labelers"
ADJUDICATION = GOLDEN.parent / "retrieval_adjudication.json"
POOL = Path(".cache") / "retrieval_pool.json"  # written by evals.retrieval --pool


def load_judgments() -> dict[str, list[dict]]:
    """Question ID -> alternative quotes, each with the title of the passage it's from."""
    titles = {}
    if POOL.exists():
        for q in json.loads(POOL.read_text(encoding="utf-8")):
            for c in q["candidates"]:
                titles[(q["question"], c["passage_id"])] = (c["title"], c["text"])
    alts: dict[str, list[dict]] = {}
    for path in sorted(LABELERS.glob("judgments_*.json")):
        for q in json.loads(path.read_text(encoding="utf-8"))["questions"]:
            for r in q["relevant"]:
                title, text = titles.get((q["question"], r["passage_id"]), ("", None))
                if text is not None and r["quote"] not in text:
                    raise ValueError(f"{path.name} {q['question']}: quote not in passage")
                alts.setdefault(q["question"], []).append({"title": title, "quote": r["quote"]})
    return alts


def merge(golden: dict, labeler_files: list[Path], alts: dict, dropped: dict) -> list[dict]:
    passages = {p["id"]: p for p in golden["passages"]}
    questions = []
    for path in labeler_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        for q in data["questions"]:
            passage = passages[q["passage"]]
            if q["quote"] not in passage["text"]:
                raise ValueError(f"{path.name} {q['passage']}: quote not in passage: {q['quote']}")
            qid = f"{q['passage']}-{data['annotator']}"
            if qid in dropped:
                continue
            questions.append(
                {
                    "id": qid,
                    "passage": q["passage"],
                    "ticker": passage["ticker"],
                    "source": passage["source"],
                    "question": q["question"],
                    "answer": q["answer"],
                    "quote": q["quote"],
                    "alt_quotes": alts.get(qid, []),
                }
            )
    return questions


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    dropped = (
        json.loads(ADJUDICATION.read_text(encoding="utf-8"))["dropped"]
        if ADJUDICATION.exists()
        else {}
    )
    files = sorted(LABELERS.glob("retrieval_*.json"))
    golden["questions"] = merge(golden, files, load_judgments(), dropped)
    GOLDEN.write_text(json.dumps(golden, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    alts = sum(len(q["alt_quotes"]) for q in golden["questions"])
    print(
        f"{len(golden['questions'])} questions from {len(files)} annotators "
        f"({len(dropped)} dropped on review), {alts} alternative quotes from judging"
    )


if __name__ == "__main__":
    main()
