"""How far to trust the chat eval's LLM judge: compare its verdicts with a second,
independent labeler's on the same answers.

    uv run python -m evals.judge_agreement export judge_input.json   # verdicts removed
    uv run python -m evals.judge_agreement score labelers/chat_judge_d.json

The labeler's file: {"answers": [{"id": ..., "sentences": ["supported", ...],
"correctness": "correct" | "partial" | "incorrect" | null}]}.
"""

import json
import sys
from pathlib import Path

from evals.chat import RESULTS

HERE = Path(__file__).resolve().parent


def export(out: Path) -> None:
    """The judged answers with the judge's verdicts and reasons stripped."""
    rows = json.loads(RESULTS.read_text(encoding="utf-8"))[0]["rows"]
    contexts = json.loads((HERE / "cache" / "chat_contexts.json").read_text(encoding="utf-8"))
    answers = []
    for r in rows:
        if not r["answered"]:
            continue
        cited = {c["label"] for s in r["sentences"] for c in s["citations"]}
        sources = {
            f"S{n}": f"{h['title']}\n{h['text']}"
            for n, h in enumerate(contexts[r["id"]], 1)
            if f"S{n}" in cited
        }
        answers.append(
            {
                "id": r["id"],
                "question": r["question"],
                "reference": r["reference"],
                "answer": r["answer"],
                "sentences": [
                    {"text": s["text"], "citations": s["citations"]} for s in r["sentences"]
                ],
                "cited_sources": sources,
            }
        )
    out.write_text(json.dumps({"answers": answers}, indent=1, ensure_ascii=False), "utf-8")
    print(f"{len(answers)} answers -> {out}")


def score(labels_path: Path) -> None:
    rows = {r["id"]: r for r in json.loads(RESULTS.read_text(encoding="utf-8"))[0]["rows"]}
    labels = json.loads(labels_path.read_text(encoding="utf-8"))["answers"]
    pairs: list[tuple[str, str]] = []
    binary: list[tuple[bool, bool]] = []
    correct_pairs: list[tuple[str, str]] = []
    for a in labels:
        judged = rows[a["id"]]
        for s, human in zip(judged["sentences"], a["sentences"], strict=True):
            if s["verdict"] is None:
                continue
            pairs.append((s["verdict"], human))
            binary.append((s["verdict"] == "supported", human == "supported"))
        if judged["correctness"] and a.get("correctness"):
            correct_pairs.append((judged["correctness"], a["correctness"]))

    def agree(p: list[tuple]) -> str:
        same = sum(x == y for x, y in p)
        return f"{same}/{len(p)} ({100 * same / max(1, len(p)):.0f}%)"

    print(f"sentence verdicts, 3-way: {agree(pairs)}")
    print(f"sentence verdicts, supported or not: {agree(binary)}")
    print(f"correctness verdicts: {agree(correct_pairs)}")
    disagreements = [
        (a["id"], s["text"], s["verdict"], human)
        for a in labels
        for s, human in zip(rows[a["id"]]["sentences"], a["sentences"], strict=True)
        if s["verdict"] is not None and s["verdict"] != human
    ]
    for qid, text, judge, human in disagreements:
        print(f"  {qid}: judge {judge}, labeler {human}: {text[:100]}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    command, path = sys.argv[1], Path(sys.argv[2])
    export(path) if command == "export" else score(path)
