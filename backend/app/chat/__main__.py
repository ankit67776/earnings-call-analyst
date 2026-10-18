"""Ask a question from the command line.

uv run python -m app.chat HSY "How did the pandemic change what consumers bought?"
"""

import argparse
import asyncio
import logging
import sys

from app import db
from app.chat.answer import ask
from app.config import settings
from app.embeddings import get_embedding_client
from app.llm import get_llm_client
from app.retrieval.passages import clock


async def _ask(ticker: str, question: str, model: str, embedding_model: str) -> None:
    conn = await db.connect()
    llm, embeddings = get_llm_client(), get_embedding_client(embedding_model)
    try:
        company = await conn.fetchval(
            "select company from documents where ticker = $1 limit 1", ticker
        )
        result = await ask(
            conn,
            llm,
            embeddings,
            ticker=ticker,
            question=question,
            company=company or ticker,
            model=model,
        )
    finally:
        await conn.close()
        await llm.aclose()
        await embeddings.aclose()
    print(f"\n{result.text}\n")
    for s in result.sentences:
        for c in s.citations:
            hit = next(h for h in result.sources if h.passage_id == c.passage_id)
            where = f"call {clock(c.at)}" if c.at is not None else hit.title[:60]
            print(f"  [{c.label}] {where} ({c.match}): {c.source_text[:120]}")
    for text in result.dropped_sentences:
        print(f"  dropped (no quote found): {text}")
    print(
        f"\n{result.model}: {result.prompt_tokens}+{result.completion_tokens} tokens, "
        f"LLM {result.llm_ms / 1000:.1f}s, retrieval {result.retrieval_ms:.0f}ms"
        f"{' (cached)' if result.cached else ''}; quotes {result.quotes - result.dropped_quotes}"
        f"/{result.quotes} verified"
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m app.chat")
    parser.add_argument("ticker", type=str.upper)
    parser.add_argument("question")
    parser.add_argument("--model", default=settings.chat_model)
    parser.add_argument("--embeddings", default=settings.embedding_model)
    args = parser.parse_args()
    asyncio.run(_ask(args.ticker, args.question, args.model, args.embeddings))


if __name__ == "__main__":
    main()
