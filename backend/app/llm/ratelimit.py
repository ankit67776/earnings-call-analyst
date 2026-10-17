import asyncio
import json
import os
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.llm.types import BudgetExceededError, CompletionRequest, Limits

WINDOW_SECONDS = 60.0


def estimate_tokens(request: CompletionRequest) -> int:
    """Upper-bound guess at a request's token cost: ~4 chars per prompt token plus the
    full output allowance. Settled against real usage once the response arrives."""
    chars = sum(len(m["content"]) for m in request.messages)
    if request.json_schema is not None:
        chars += len(json.dumps(request.json_schema))
    return chars // 4 + 8 * len(request.messages) + request.max_tokens


@dataclass
class _Entry:
    at: float
    tokens: int


@dataclass
class Reservation:
    model: str
    entry: _Entry


class RateLimiter:
    """Client-side pacing for per-model RPM/TPM limits and RPD/TPD budgets.

    Per-minute limits use a sliding 60s window of reservations. Each call reserves
    its estimated tokens up front and settles to the real count afterwards, so
    concurrent callers don't overshoot. Daily totals persist to `usage_file` so
    separate dev runs share one budget; the day rolls over at UTC midnight (the
    provider's own window may differ, its 429s are the backstop).
    """

    def __init__(
        self,
        limits: Callable[[str], Limits],
        usage_file: Path | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        today: Callable[[], str] = lambda: datetime.now(UTC).date().isoformat(),
    ) -> None:
        self._limits = limits
        self._usage_file = usage_file
        self._clock = clock
        self._sleep = sleep
        self._today = today
        self._windows: dict[str, deque[_Entry]] = defaultdict(deque)
        self._paused_until: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._day, self._daily = self._load_usage()

    async def acquire(self, model: str, tokens: int) -> Reservation:
        limits = self._limits(model)
        if tokens > limits.tpm:
            raise BudgetExceededError(
                f"request needs ~{tokens} tokens but {model} allows {limits.tpm}/min; "
                "use smaller chunks or a lower max_tokens"
            )
        async with self._locks[model]:
            while True:
                now = self._clock()
                paused = self._paused_until.get(model, 0.0)
                if paused > now:
                    await self._sleep(paused - now)
                    continue

                daily = self._daily_usage(model)
                if daily["tokens"] + tokens > limits.tpd or daily["requests"] + 1 > limits.rpd:
                    raise BudgetExceededError(
                        f"{model} daily budget used up ({daily['tokens']}/{limits.tpd} tokens, "
                        f"{daily['requests']}/{limits.rpd} requests today)"
                    )

                window = self._windows[model]
                while window and window[0].at <= now - WINDOW_SECONDS:
                    window.popleft()
                wait = self._wait_for_room(window, tokens, limits, now)
                if wait <= 0:
                    break
                await self._sleep(wait)

            entry = _Entry(now, tokens)
            window.append(entry)
            daily["tokens"] += tokens
            daily["requests"] += 1
            self._save_usage()
            return Reservation(model, entry)

    def settle(self, reservation: Reservation, actual_tokens: int) -> None:
        """Replace a reservation's estimate with the tokens the call really used."""
        delta = actual_tokens - reservation.entry.tokens
        reservation.entry.tokens = actual_tokens
        daily = self._daily_usage(reservation.model)
        daily["tokens"] = max(0, daily["tokens"] + delta)
        self._save_usage()

    def pause(self, model: str, seconds: float) -> None:
        """Hold all calls to `model`, e.g. for a 429's retry-after."""
        until = self._clock() + seconds
        self._paused_until[model] = max(self._paused_until.get(model, 0.0), until)

    def _wait_for_room(
        self, window: deque[_Entry], tokens: int, limits: Limits, now: float
    ) -> float:
        used = sum(e.tokens for e in window)
        count = len(window)
        if used + tokens <= limits.tpm and count + 1 <= limits.rpm:
            return 0.0
        # Walk the window oldest-first until enough has expired to fit this call.
        for entry in window:
            used -= entry.tokens
            count -= 1
            if used + tokens <= limits.tpm and count + 1 <= limits.rpm:
                return entry.at + WINDOW_SECONDS - now
        return WINDOW_SECONDS

    def _daily_usage(self, model: str) -> dict[str, int]:
        today = self._today()
        if today != self._day:
            self._day, self._daily = today, {}
        return self._daily.setdefault(model, {"tokens": 0, "requests": 0})

    def _load_usage(self) -> tuple[str, dict[str, dict[str, int]]]:
        today = self._today()
        if self._usage_file is None or not self._usage_file.exists():
            return today, {}
        try:
            data = json.loads(self._usage_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return today, {}
        if data.get("date") != today:
            return today, {}
        return today, data.get("models", {})

    def _save_usage(self) -> None:
        if self._usage_file is None:
            return
        self._usage_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._usage_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": self._day, "models": self._daily}), encoding="utf-8")
        os.replace(tmp, self._usage_file)
