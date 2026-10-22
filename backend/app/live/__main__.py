"""Stream an Earnings-21 call into live speech-to-text, as if it were happening now.

uv run python -m app.live listen COP --minutes 2              # 1x, first two minutes
uv run python -m app.live listen SITM --start 30 --minutes 5 --no-cache   # fresh run
"""

import argparse
import asyncio
import logging
import statistics
import sys
from contextlib import aclosing

from app.calls import earnings21
from app.config import settings
from app.live.replay import Replay
from app.live.stt import StreamRequest, Update, get_live_transcriber


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def _print_final(update: Update) -> None:
    runs: list[tuple[int | None, list[str]]] = []
    for w in update.words:
        if runs and runs[-1][0] == w.speaker:
            runs[-1][1].append(w.text)
        else:
            runs.append((w.speaker, [w.text]))
    for speaker, words in runs:
        label = "S?" if speaker is None else f"S{speaker}"
        print(f"[{_clock(update.start)}] {label}: {' '.join(words)}", flush=True)


async def _listen(args: argparse.Namespace) -> None:
    file_id = earnings21.CALLS[args.ticker]
    replay = Replay(
        earnings21.audio_path(file_id).read_bytes(),
        speed=args.speed,
        start=args.start * 60,
        duration=args.minutes * 60 if args.minutes else None,
    )
    transcriber = get_live_transcriber()
    request = StreamRequest(model=args.model)
    finals: list[Update] = []
    print(
        f"{args.ticker} ({file_id}): {replay.duration / 60:.1f} min from "
        f"{_clock(replay.offset)} at {args.speed:g}x, {transcriber.provider.name} {args.model}"
    )
    stream = transcriber.stream(replay, request, use_cache=not args.no_cache)
    async with aclosing(stream) as updates:
        async for update in updates:
            if update.kind == "final":
                finals.append(update)
                _print_final(update)
            elif update.kind == "interim" and args.interim:
                print(f"  ~ {update.text}", flush=True)
            elif update.kind == "utterance_end" and args.interim:
                print("  -- utterance end", flush=True)

    words = [w for u in finals for w in u.words]
    # How long after the end of its audio each final arrived (from the recorded run).
    lags = sorted(u.received - (u.end - replay.offset) / args.speed for u in finals)
    print(
        f"\n{len(words)} words in {len(finals)} finals, {len({w.speaker for w in words})} speakers"
    )
    if len(lags) >= 2:
        p95 = statistics.quantiles(lags, n=20)[-1]
        print(f"final arrives after its audio: p50 {statistics.median(lags):.2f}s, p95 {p95:.2f}s")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(prog="python -m app.live")
    commands = parser.add_subparsers(dest="command", required=True)
    listen = commands.add_parser("listen", help="replay a call into streaming speech-to-text")
    listen.add_argument("ticker", type=str.upper, choices=earnings21.CALLS)
    listen.add_argument("--speed", type=float, default=1.0, help="times real time (default 1)")
    listen.add_argument("--start", type=float, default=0.0, help="minutes into the call")
    listen.add_argument("--minutes", type=float, help="how much to play (default: the rest)")
    listen.add_argument("--model", default=settings.live_stt_model)
    listen.add_argument("--interim", action="store_true", help="also print interim results")
    listen.add_argument("--no-cache", action="store_true", help="stream even if recorded")
    args = parser.parse_args()
    asyncio.run(_listen(args))


if __name__ == "__main__":
    main()
