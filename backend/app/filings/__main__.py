"""Fetch and inspect 10-K filings.

uv run python -m app.filings fetch SITM HSY COP --year 2020
uv run python -m app.filings fetch SITM COP --year 2020 --save   # also store in Postgres
uv run python -m app.filings show ../data/raw/10k/HSY_10-K_FY2020.htm --section 1A
"""

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from app.config import settings
from app.filings.edgar import EdgarClient, load_ref
from app.filings.tenk import parse_10k


async def fetch(tickers: list[str], year: int, dest: Path, *, save: bool = False) -> None:
    from app import db
    from app.store import save_filing

    client = EdgarClient(settings.sec_user_agent)
    conn = await db.connect() if save else None
    try:
        for ticker in tickers:
            ref = await client.find_annual_report(ticker, year)
            path = await client.download(ref, dest)
            print(f"{ref.ticker}: {ref.company}, period {ref.period}, filed {ref.filed} -> {path}")
            if conn is not None:
                document_id = await save_filing(conn, ref, parse_10k(path.read_bytes()))
                print(f"  stored as document {document_id}")
    finally:
        await client.aclose()
        if conn is not None:
            await conn.close()


def show(path: Path, section: str | None, width: int) -> None:
    doc = parse_10k(path.read_bytes())
    ref = load_ref(path)
    if ref is not None:
        print(f"{ref.company} {ref.form} FY{ref.fiscal_year} ({ref.accession})\n")
    if section is None:
        for s in doc.sections:
            kinds = Counter(p.kind for p in s.paragraphs)
            print(
                f"Item {s.id:<3} {s.title[:58]:<58} {len(s.paragraphs):>4} paragraphs "
                f"({kinds['heading']} headings, {kinds['table']} tables)"
            )
        return
    found = doc.section(section)
    if found is None:
        sys.exit(f"no Item {section} in {path.name}")
    print(f"Item {found.id}. {found.title}\n")
    for p in found.paragraphs:
        text = p.text.replace("\n", " / ")
        if len(text) > width:
            text = text[: width - 1] + "…"
        print(f"{p.index:>4} {p.kind:<7} {p.id:<14} {text}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m app.filings")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch_cmd = commands.add_parser("fetch", help="download annual reports from EDGAR")
    fetch_cmd.add_argument("tickers", nargs="+")
    fetch_cmd.add_argument("--year", type=int, required=True, help="fiscal year")
    fetch_cmd.add_argument("--dest", type=Path, default=Path(settings.raw_data_dir) / "10k")
    fetch_cmd.add_argument("--save", action="store_true", help="store the parsed text in Postgres")

    show_cmd = commands.add_parser("show", help="print a parsed 10-K's sections or paragraphs")
    show_cmd.add_argument("path", type=Path)
    show_cmd.add_argument("--section", help="e.g. 1A or 7; omit to list sections")
    show_cmd.add_argument("--width", type=int, default=160)

    args = parser.parse_args()
    if args.command == "fetch":
        asyncio.run(fetch(args.tickers, args.year, args.dest, save=args.save))
    else:
        show(args.path, args.section, args.width)


if __name__ == "__main__":
    main()
