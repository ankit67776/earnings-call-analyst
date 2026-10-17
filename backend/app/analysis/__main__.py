"""Extract grounded claims from a downloaded 10-K.

    uv run python -m app.analysis ../data/raw/10k/SITM_10-K_FY2020.htm --section 1A --max-chunks 2

Results are written next to the filing as <name>.<section>.findings.json.
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from app.analysis.extract import PROMPT_VERSION, SectionAnalysis, analyze_section
from app.filings.edgar import load_ref
from app.filings.tenk import FOCUS_SECTIONS, parse_10k
from app.llm import get_llm_client


async def run(path: Path, sections: list[str], model: str | None, max_chunks: int | None) -> None:
    doc = parse_10k(path.read_bytes())
    ref = load_ref(path)
    company = ref.company if ref else None
    client = get_llm_client()
    try:
        for item in sections:
            section = doc.section(item)
            if section is None:
                sys.exit(f"no Item {item} in {path.name}")
            analysis = await analyze_section(
                client, section, company=company, model=model, max_chunks=max_chunks
            )
            report(analysis)
            out = path.with_name(f"{path.stem}.{item}.findings.json")
            out.write_text(
                json.dumps(
                    {"filing": path.name, "prompt_version": PROMPT_VERSION} | asdict(analysis),
                    indent=1,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            print(f"wrote {out}\n")
    finally:
        await client.aclose()


def report(a: SectionAnalysis) -> None:
    print(f"\nItem {a.section}: {len(a.findings)} findings from {a.chunks} chunks ({a.model})")
    for f in a.findings:
        refs = ", ".join(f"{c.paragraph_id} ({c.match})" for c in f.citations)
        print(f"  [{f.type:<8}] {f.claim}\n             {refs}")
    for f in a.rejected:
        print(f"  REJECTED [{f.type}] {f.claim}")
        print(f"             quote not found: {f.citations[0].quote!r}")
    counts = a.match_counts()
    total = sum(counts.values())
    grounded = total - counts["unverified"]
    print(
        f"\nquotes grounded: {grounded}/{total} "
        f"(exact {counts['exact']}, normalized {counts['normalized']}, "
        f"elided {counts['elided']}, fuzzy {counts['fuzzy']})"
    )
    print(
        f"tokens: {a.prompt_tokens} prompt + {a.completion_tokens} completion over "
        f"{a.llm_calls} calls ({a.cached_calls} cached), {a.seconds:.0f}s"
    )
    for i, error in a.failed_chunks:
        print(f"chunk {i} failed: {error}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m app.analysis")
    parser.add_argument("path", type=Path, help="10-K downloaded with `python -m app.filings`")
    parser.add_argument("--section", action="append", help="Item to analyze (default: 1A and 7)")
    parser.add_argument("--model", help="defaults to ANALYSIS_MODEL")
    parser.add_argument("--max-chunks", type=int, help="only the first N chunks (cheap trial)")
    args = parser.parse_args()
    asyncio.run(run(args.path, args.section or list(FOCUS_SECTIONS), args.model, args.max_chunks))


if __name__ == "__main__":
    main()
