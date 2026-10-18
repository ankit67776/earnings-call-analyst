"""Sample the passages of the extraction golden set from downloaded 10-Ks.

Three passages per section (risk factors and MD&A) per company, picked with a fixed
seed so the sample is reproducible. Each passage is a chunk of whole paragraphs, cut
the way the extractor cuts a section, but smaller (2,500 characters) so a person can
label every claim in it. Claims come from merge_labels.py; re-running this keeps the
claims of passages that are still sampled.

    uv run python -m evals.build_golden
"""

import json
import random
import sys
from pathlib import Path

from app.analysis.chunking import chunk_section
from app.filings.tenk import FOCUS_SECTIONS, parse_10k

RAW = Path(__file__).resolve().parents[2] / "data" / "raw" / "10k"
GOLDEN = Path(__file__).resolve().parent / "golden" / "extraction.json"
FILINGS = [("HSY", 2020), ("SITM", 2020), ("COP", 2020)]
PER_SECTION = 3
MAX_CHARS = 2_500
SEED = 2020


def sample() -> list[dict]:
    rng = random.Random(SEED)
    passages = []
    for ticker, year in FILINGS:
        meta = json.loads((RAW / f"{ticker}_10-K_FY{year}.json").read_text(encoding="utf-8"))
        doc = parse_10k((RAW / f"{ticker}_10-K_FY{year}.htm").read_bytes())
        for item in FOCUS_SECTIONS:
            section = doc.section(item)
            chunks = chunk_section(section, max_chars=MAX_CHARS)
            for i in sorted(rng.sample(range(len(chunks)), PER_SECTION)):
                chunk = chunks[i]
                passages.append(
                    {
                        "id": f"{ticker}-{item}-{i:02d}",
                        "ticker": ticker,
                        "company": meta["company"],
                        "fiscal_year": year,
                        "url": meta["url"],
                        "section": item,
                        "section_title": section.title,
                        "context_heading": chunk.context_heading,
                        "paragraphs": [
                            {"id": p.id, "kind": p.kind, "text": p.text} for p in chunk.paragraphs
                        ],
                        "claims": [],
                    }
                )
    return passages


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    current = json.loads(GOLDEN.read_text("utf-8")) if GOLDEN.exists() else {}
    old = {p["id"]: p["claims"] for p in current.get("passages", [])}
    passages = sample()
    for p in passages:
        p["claims"] = old.get(p["id"], [])
    data = current | {"version": 1, "max_chars": MAX_CHARS, "seed": SEED, "passages": passages}
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    chars = sum(len(q["text"]) for p in passages for q in p["paragraphs"])
    print(f"{len(passages)} passages, {chars:,} characters -> {GOLDEN}")


if __name__ == "__main__":
    main()
