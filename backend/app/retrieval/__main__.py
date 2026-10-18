"""Build and query the retrieval index.

uv run python -m app.retrieval index                       # passages for every filing and call
uv run python -m app.retrieval embed voyage/voyage-4 gemini/gemini-embedding-2
uv run python -m app.retrieval search HSY "How did COVID affect demand?" --mode hybrid
"""

import argparse
import asyncio
import logging
import sys

from app import db
from app.config import settings
from app.embeddings import get_embedding_client
from app.retrieval.index import embed_passages, index_call, index_document
from app.retrieval.search import search


async def _index() -> None:
    conn = await db.connect()
    try:
        for d in await conn.fetch("select id, ticker from documents order by id"):
            n = await index_document(conn, d["id"])
            print(f"{d['ticker']} 10-K (document {d['id']}): {n} passages")
        for c in await conn.fetch("select id, ticker from calls order by id"):
            n = await index_call(conn, c["id"])
            print(f"{c['ticker']} call (call {c['id']}): {n} passages")
    finally:
        await conn.close()


async def _embed(models: list[str], ticker: str | None) -> None:
    async def one(spec: str) -> None:
        conn = await db.connect()
        client = get_embedding_client(spec)
        try:
            added = await embed_passages(conn, client, ticker=ticker)
            s = client.stats
            print(
                f"{client.key}: {added} passages embedded "
                f"({s.cached} cached, {s.requests} requests, {s.tokens:,} tokens)"
            )
        finally:
            await client.aclose()
            await conn.close()

    # Each model has its own provider limits, so they can run side by side.
    await asyncio.gather(*(one(spec) for spec in models))


async def _search(ticker: str, query: str, mode: str, model: str, k: int) -> None:
    conn = await db.connect()
    client = get_embedding_client(model) if mode != "fts" else None
    try:
        hits = await search(conn, query, ticker=ticker, embeddings=client, mode=mode, k=k)
        for n, h in enumerate(hits, 1):
            ranks = f"fts={h.fts_rank} vector={h.vector_rank}"
            print(f"\n{n}. [{h.source} #{h.passage_id}] {h.title}  ({h.score:.4f}, {ranks})")
            print("   " + h.text[:300].replace("\n", " "))
    finally:
        await conn.close()
        if client is not None:
            await client.aclose()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m app.retrieval")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("index", help="(re)build passages for every stored filing and call")
    embed = commands.add_parser("embed", help="embed passages that have no vector yet")
    embed.add_argument("models", nargs="*", help="provider/model (default: EMBEDDING_MODEL)")
    embed.add_argument("--ticker")
    find = commands.add_parser("search", help="query one company's filings and calls")
    find.add_argument("ticker", type=str.upper)
    find.add_argument("query")
    find.add_argument("--mode", choices=["fts", "vector", "hybrid"], default="hybrid")
    find.add_argument("--model", default=settings.embedding_model)
    find.add_argument("-k", type=int, default=5)
    args = parser.parse_args()
    if args.command == "index":
        asyncio.run(_index())
    elif args.command == "embed":
        asyncio.run(_embed(args.models or [settings.embedding_model], args.ticker))
    else:
        asyncio.run(_search(args.ticker, args.query, args.mode, args.model, args.k))


if __name__ == "__main__":
    main()
