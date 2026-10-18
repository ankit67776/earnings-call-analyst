"""Sample the passages behind the retrieval golden set from the stored index.

For each company, passages from its 10-K and from its earnings call are picked with a
fixed seed. An annotator writes, for each passage in order, one question an analyst
might ask that the passage answers, plus the answer and the exact quote that states it
(skipping passages with nothing specific to ask about, until ten per source). Questions
and quotes come from labelers/ files; re-running this keeps them for passages still
sampled.

    uv run python -m evals.build_retrieval
"""

import asyncio
import json
import random
import sys
from pathlib import Path

from app import db

GOLDEN = Path(__file__).resolve().parent / "golden" / "retrieval.json"
TICKERS = ("HSY", "SITM", "COP")
# Spares for the passages an annotator skips.
PER_SOURCE = 14
MIN_WORDS = 60
SEED = 2021


async def sample() -> list[dict]:
    rng = random.Random(SEED)
    conn = await db.connect()
    try:
        out = []
        for ticker in TICKERS:
            company = await conn.fetchval(
                "select company from documents where ticker = $1 order by fiscal_year desc",
                ticker,
            )
            for source in ("filing", "call"):
                rows = await conn.fetch(
                    """
                    select position, title, text from passages
                    where ticker = $1 and (call_id is not null) = ($2 = 'call')
                    order by position
                    """,
                    ticker,
                    source,
                )
                eligible = [
                    r
                    for r in rows
                    if len(r["text"].split()) >= MIN_WORDS
                    # The operator's opening and the safe-harbor statement.
                    and not (source == "call" and r["position"] <= 2)
                    # Mostly a table: rows of numbers with little to ask about in words.
                    and r["text"].count("\n") < len(r["text"].split()) / 8
                ]
                for r in sorted(rng.sample(eligible, PER_SOURCE), key=lambda r: r["position"]):
                    out.append(
                        {
                            "id": f"{ticker}-{source}-{r['position']:03d}",
                            "ticker": ticker,
                            "company": company,
                            "source": source,
                            "title": r["title"],
                            "text": r["text"],
                        }
                    )
        return out
    finally:
        await conn.close()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    current = json.loads(GOLDEN.read_text("utf-8")) if GOLDEN.exists() else {}
    old = {q["passage"]: q for q in current.get("questions", [])}
    passages = asyncio.run(sample())
    ids = {p["id"] for p in passages}
    data = {
        "version": 1,
        "seed": SEED,
        "passages": passages,
        "questions": [q for q in old.values() if q["passage"] in ids],
    }
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(passages)} passages -> {GOLDEN}")


if __name__ == "__main__":
    main()
