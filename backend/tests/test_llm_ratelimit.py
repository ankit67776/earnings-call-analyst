import pytest

from app.llm.ratelimit import RateLimiter, estimate_tokens
from app.llm.types import BudgetExceededError, CompletionRequest, Limits
from tests.fakes import FakeTime

pytestmark = pytest.mark.anyio

MODEL = "m"
LIMITS = Limits(rpm=3, tpm=1_000, rpd=100, tpd=2_000)


def make_limiter(time: FakeTime, usage_file=None, today=lambda: "2026-10-17") -> RateLimiter:
    return RateLimiter(
        lambda model: LIMITS, usage_file, clock=time.clock, sleep=time.sleep, today=today
    )


async def test_waits_for_tokens_to_leave_the_window() -> None:
    time = FakeTime()
    limiter = make_limiter(time)
    await limiter.acquire(MODEL, 600)
    time.now = 10.0
    await limiter.acquire(MODEL, 600)
    assert time.now == pytest.approx(60.0)


async def test_settling_below_the_estimate_frees_room() -> None:
    time = FakeTime()
    limiter = make_limiter(time)
    reservation = await limiter.acquire(MODEL, 600)
    limiter.settle(reservation, 100)
    await limiter.acquire(MODEL, 600)
    assert time.sleeps == []


async def test_waits_for_request_slots() -> None:
    time = FakeTime()
    limiter = make_limiter(time)
    for _ in range(3):
        await limiter.acquire(MODEL, 1)
    await limiter.acquire(MODEL, 1)
    assert time.now == pytest.approx(60.0)


async def test_rejects_a_request_bigger_than_the_minute_budget() -> None:
    with pytest.raises(BudgetExceededError, match="smaller chunks"):
        await make_limiter(FakeTime()).acquire(MODEL, 1_001)


async def test_pause_holds_calls() -> None:
    time = FakeTime()
    limiter = make_limiter(time)
    limiter.pause(MODEL, 12.5)
    await limiter.acquire(MODEL, 1)
    assert time.now == pytest.approx(12.5)


async def test_daily_budget_persists_across_instances_and_resets_next_day(tmp_path) -> None:
    usage = tmp_path / "usage.json"
    time = FakeTime()
    limiter = make_limiter(time, usage)
    for _ in range(2):
        time.now += 61  # keep the per-minute window clear
        limiter.settle(await limiter.acquire(MODEL, 900), 900)

    with pytest.raises(BudgetExceededError, match="daily budget"):
        await make_limiter(time, usage).acquire(MODEL, 900)

    await make_limiter(time, usage, today=lambda: "2026-10-18").acquire(MODEL, 900)


def test_estimate_counts_prompt_schema_and_output_allowance() -> None:
    request = CompletionRequest(
        model=MODEL,
        messages=[{"role": "user", "content": "x" * 400}],
        max_tokens=500,
    )
    assert estimate_tokens(request) == 100 + 8 + 500
    with_schema = CompletionRequest(
        model=MODEL, messages=request.messages, max_tokens=500, json_schema={"type": "object"}
    )
    assert estimate_tokens(with_schema) > estimate_tokens(request)
