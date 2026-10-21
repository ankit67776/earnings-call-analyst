"""Find where what management said on a call contradicts the company's 10-K.

uv run python -m app.contradictions claims              # extract claims from every call
uv run python -m app.contradictions check SITM          # judge them against the 10-K
uv run python -m app.contradictions show SITM           # print what was flagged
"""

import argparse
import asyncio
import logging
import sys
from collections import Counter

from app import db
from app.config import settings
from app.contradictions.check import DEFAULT_K, check_call
from app.contradictions.claims import extract_call_claims, save_call_claims
from app.embeddings import get_embedding_client
from app.llm import get_llm_client
from app.retrieval.passages import clock


async def _calls(conn, tickers: list[str]) -> list:
    return await conn.fetch(
        "select id, ticker, fiscal_year, quarter from calls"
        " where cardinality($1::text[]) = 0 or ticker = any($1) order by ticker, id",
        tickers,
    )


async def _claims(tickers: list[str], model: str) -> None:
    conn = await db.connect()
    llm = get_llm_client()
    try:
        for call in await _calls(conn, tickers):
            result = await extract_call_claims(conn, llm, call["id"], model=model)
            await save_call_claims(conn, result)
            a = result.analysis
            print(
                f"\n{call['ticker']} Q{call['quarter']} {call['fiscal_year']} call: "
                f"{len(result.claims)} claims from {a.chunks} chunks, "
                f"{len(a.rejected)} rejected (quote not found), "
                f"{len(result.dropped)} dropped (not a company speaker), "
                f"{a.prompt_tokens + a.completion_tokens + result.speaker_tokens:,} tokens"
            )
            print(f"  company speakers: {', '.join(sorted(result.executives))}")
            for position, error in a.failed_chunks:
                print(f"  chunk {position} failed: {error}")
            for c in result.claims:
                print(f"  {clock(c.at):>7} [{c.type:<8}] {c.speaker}: {c.claim}")
            for c in result.dropped:
                print(f"  dropped {clock(c.at):>7} {c.speaker}: {c.claim}")
    finally:
        await conn.close()
        await llm.aclose()


async def _check(tickers: list[str], model: str, k: int) -> None:
    conn = await db.connect()
    llm, embeddings = get_llm_client(), get_embedding_client()
    try:
        for call in await _calls(conn, tickers):
            checks = await check_call(conn, llm, embeddings, call["id"], k=k, model=model)
            pairs = [p for c in checks for p in c.pairs]
            verdicts = Counter(p.verdict or "missing" for p in pairs)
            tokens = sum(c.prompt_tokens + c.completion_tokens for c in checks)
            print(
                f"\n{call['ticker']} Q{call['quarter']} {call['fiscal_year']} call: "
                f"{len(checks)} claims checked, {len(pairs)} pairs ({model}), "
                + ", ".join(f"{n} {v}" for v, n in verdicts.most_common())
                + f"; {tokens:,} tokens, {sum(c.cached for c in checks)} cached"
            )
        await _show(conn, tickers)
    finally:
        await conn.close()
        await llm.aclose()
        await embeddings.aclose()


async def _show(conn, tickers: list[str]) -> None:
    rows = await conn.fetch(
        """
        select c.ticker, cc.at_s, cc.speaker, cc.quote, cc.claim, k.match, k.source_text,
               k.filing_quote, k.reason, p.title
        from claim_checks k
        join call_claims cc on cc.id = k.claim_id
        join calls c on c.id = cc.call_id
        join passages p on p.id = k.passage_id
        where k.verdict = 'contradiction'
          and (cardinality($1::text[]) = 0 or c.ticker = any($1))
        order by c.ticker, cc.at_s, k.rank
        """,
        tickers,
    )
    for r in rows:
        shown = "" if r["match"] else "  (10-K quote not found: not shown)"
        print(f'\n{r["ticker"]} {clock(r["at_s"])} {r["speaker"]}: "{r["quote"]}"{shown}')
        print(f'  10-K, {r["title"]}: "{r["source_text"] or r["filing_quote"]}"')
        print(f"  {r['reason']}")


async def _show_only(tickers: list[str]) -> None:
    conn = await db.connect()
    try:
        await _show(conn, tickers)
    finally:
        await conn.close()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.contradictions")
    commands = parser.add_subparsers(dest="command", required=True)
    claims = commands.add_parser("claims", help="extract claims from calls")
    claims.add_argument("tickers", nargs="*", type=str.upper)
    claims.add_argument("--model", default=settings.call_claims_model)
    check = commands.add_parser("check", help="judge stored claims against the 10-K")
    check.add_argument("tickers", nargs="*", type=str.upper)
    check.add_argument("--model", default=settings.verifier_model)
    check.add_argument("-k", type=int, default=DEFAULT_K, help="10-K passages per claim")
    show = commands.add_parser("show", help="print the contradictions found")
    show.add_argument("tickers", nargs="*", type=str.upper)
    args = parser.parse_args()
    if args.command == "claims":
        asyncio.run(_claims(args.tickers, args.model))
    elif args.command == "check":
        logging.getLogger("app.contradictions").setLevel(logging.INFO)
        asyncio.run(_check(args.tickers, args.model, args.k))
    else:
        asyncio.run(_show_only(args.tickers))


if __name__ == "__main__":
    main()
