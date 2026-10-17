import pytest
from pydantic import BaseModel, field_validator

from app.llm.cache import DiskCache
from app.llm.client import LLMClient
from app.llm.ratelimit import RateLimiter
from app.llm.types import (
    LLMError,
    OutputValidationError,
    RateLimitedError,
    TransientError,
    TruncatedOutputError,
)
from tests.fakes import FakeProvider, FakeTime, result

pytestmark = pytest.mark.anyio

MESSAGES = [{"role": "user", "content": "hi"}]


class Answer(BaseModel):
    answer: str
    confidence: float

    @field_validator("confidence")
    @classmethod
    def in_range(cls, v: float) -> float:
        if not 0 <= v <= 1:
            raise ValueError("confidence must be in [0, 1]")
        return v


def make_client(provider, time, *, cache=None, limiter=True) -> LLMClient:
    return LLMClient(
        provider,
        default_model="fake-model",
        limiter=RateLimiter(provider.limits, clock=time.clock, sleep=time.sleep)
        if limiter
        else None,
        cache=cache,
        sleep=time.sleep,
        clock=time.clock,
    )


async def test_retries_transient_errors_with_backoff() -> None:
    time = FakeTime()
    provider = FakeProvider(TransientError("503"), TransientError("timeout"), result("done"))
    completion = await make_client(provider, time).complete(MESSAGES)
    assert completion.text == "done"
    assert completion.attempts == 3
    assert len(time.sleeps) == 2
    assert 1 <= time.sleeps[0] < 1.5 and 2 <= time.sleeps[1] < 2.5


async def test_rate_limit_pauses_the_model_for_retry_after() -> None:
    time = FakeTime()
    provider = FakeProvider(RateLimitedError("429", retry_after=7), result())
    completion = await make_client(provider, time).complete(MESSAGES)
    assert completion.attempts == 2
    assert time.now == pytest.approx(7)


async def test_gives_up_when_retry_after_is_too_long() -> None:
    provider = FakeProvider(RateLimitedError("429", retry_after=3_600), result())
    with pytest.raises(RateLimitedError):
        await make_client(provider, FakeTime()).complete(MESSAGES)
    assert len(provider.requests) == 1


async def test_gives_up_after_max_attempts() -> None:
    provider = FakeProvider(*[TransientError("503")] * 4)
    with pytest.raises(TransientError):
        await make_client(provider, FakeTime()).complete(MESSAGES)
    assert len(provider.requests) == 4


async def test_permanent_errors_are_not_retried() -> None:
    provider = FakeProvider(LLMError("401 bad key"), result())
    with pytest.raises(LLMError, match="bad key"):
        await make_client(provider, FakeTime()).complete(MESSAGES)
    assert len(provider.requests) == 1


async def test_truncated_output_raises_and_is_not_cached(tmp_path) -> None:
    provider = FakeProvider(result(finish_reason="length"), result("full"))
    client = make_client(provider, FakeTime(), cache=DiskCache(tmp_path))
    with pytest.raises(TruncatedOutputError):
        await client.complete(MESSAGES)
    assert (await client.complete(MESSAGES)).text == "full"


async def test_cache_hit_skips_the_provider(tmp_path) -> None:
    provider = FakeProvider(result("first"))
    client = make_client(provider, FakeTime(), cache=DiskCache(tmp_path))
    first = await client.complete(MESSAGES)
    second = await client.complete(MESSAGES)
    assert len(provider.requests) == 1
    assert (first.cached, second.cached) == (False, True)
    assert second.text == "first" and second.total_tokens == first.total_tokens


async def test_cache_key_covers_request_options(tmp_path) -> None:
    provider = FakeProvider(result("a"), result("b"), result("c"))
    client = make_client(provider, FakeTime(), cache=DiskCache(tmp_path))
    await client.complete(MESSAGES)
    await client.complete(MESSAGES, max_tokens=100)
    await client.complete(MESSAGES, use_cache=False)
    assert len(provider.requests) == 3


async def test_complete_json_sends_strict_schema_and_parses() -> None:
    provider = FakeProvider(result('{"answer": "yes", "confidence": 0.9}'))
    parsed = await make_client(provider, FakeTime()).complete_json(MESSAGES, Answer)
    assert parsed.value == Answer(answer="yes", confidence=0.9)
    request = provider.requests[0]
    assert request.schema_name == "Answer"
    assert request.json_schema["required"] == ["answer", "confidence"]
    assert request.json_schema["additionalProperties"] is False


async def test_invalid_json_output_raises_and_is_evicted_from_cache(tmp_path) -> None:
    provider = FakeProvider(
        result('{"answer": "yes", "confidence": 7}'),
        result('{"answer": "yes", "confidence": 0.7}'),
    )
    client = make_client(provider, FakeTime(), cache=DiskCache(tmp_path))
    with pytest.raises(OutputValidationError, match="confidence"):
        await client.complete_json(MESSAGES, Answer)
    parsed = await client.complete_json(MESSAGES, Answer)
    assert parsed.value.confidence == 0.7
    assert len(provider.requests) == 2
