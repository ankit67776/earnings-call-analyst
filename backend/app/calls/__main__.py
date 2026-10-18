"""Transcribe the project's Earnings-21 calls.

uv run python -m app.calls transcribe SITM HSY COP
uv run python -m app.calls transcribe COP --model whisper-large-v3-turbo
"""

import argparse
import asyncio
import logging
import sys

from app.calls import earnings21
from app.calls.ingest import ingest_earnings21
from app.calls.stt import SpeechRequest, get_transcriber
from app.calls.transcript import transcribe_audio
from app.config import settings


async def _transcribe(tickers: list[str], model: str) -> None:
    transcriber = get_transcriber()
    try:
        for ticker in tickers:
            file_id = earnings21.CALLS[ticker]
            data = earnings21.audio_path(file_id).read_bytes()
            result = await transcribe_audio(
                transcriber, data, SpeechRequest(model=model), filename=f"{file_id}.mp3"
            )
            words = sum(len(s.text.split()) for s in result.segments)
            print(
                f"{ticker} ({file_id}) {model}: {result.duration / 60:.1f} min, "
                f"{len(result.segments)} segments, {words} words, "
                f"{result.latency_ms / 1000:.1f}s{' (cached)' if result.cached else ''}"
            )
    finally:
        await transcriber.aclose()


async def _load(tickers: list[str], model: str) -> None:
    from app import db

    transcriber = get_transcriber()
    conn = await db.connect()
    try:
        for ticker in tickers:
            call_id = await ingest_earnings21(conn, transcriber, ticker, model=model)
            segments, turns = await conn.fetchrow(
                "select count(*), max(turn) from call_segments where call_id = $1", call_id
            )
            print(f"{ticker}: call {call_id}, {segments} segments in {turns} speaker turns")
    finally:
        await conn.close()
        await transcriber.aclose()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="python -m app.calls")
    commands = parser.add_subparsers(dest="command", required=True)
    transcribe = commands.add_parser("transcribe", help="speech-to-text for Earnings-21 calls")
    transcribe.add_argument("tickers", nargs="+", type=str.upper, choices=earnings21.CALLS)
    transcribe.add_argument("--model", default=settings.stt_model)
    load = commands.add_parser("load", help="transcribe Earnings-21 calls and store them")
    load.add_argument("tickers", nargs="+", type=str.upper, choices=earnings21.CALLS)
    load.add_argument("--model", default=settings.stt_model)
    args = parser.parse_args()
    run = _transcribe if args.command == "transcribe" else _load
    asyncio.run(run(args.tickers, args.model))


if __name__ == "__main__":
    main()
