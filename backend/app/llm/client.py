import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.llm.cache import DiskCache
from app.llm.providers import Provider
from app.llm.ratelimit import RateLimiter, estimate_tokens
from app.llm.schema import strict_schema
from app.llm.types import (
    Completion,
    CompletionRequest,
    LLMError,
    Message,
    OutputValidationError,
    RateLimitedError,
    ReasoningEffort,
    TransientError,
    TruncatedOutputError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Parsed[T: BaseModel]:
    value: T
    completion: Completion


class LLMClient:
    """The one entry point pipelines use to call an LLM.

    Adds what every call needs on top of a provider: client-side rate limiting,
    retries with backoff, an optional on-disk response cache, token/latency logging,
    and strict JSON-schema outputs validated into Pydantic models.
    """

    def __init__(
        self,
        provider: Provider,
        *,
        default_model: str,
        limiter: RateLimiter | None = None,
        cache: DiskCache | None = None,
        max_attempts: int = 4,
        max_retry_wait: float = 90.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.provider = provider
        self.default_model = default_model
        self._limiter = limiter
        self._cache = cache
        self._max_attempts = max_attempts
        self._max_retry_wait = max_retry_wait
        self._sleep = sleep
        self._clock = clock

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        reasoning_effort: ReasoningEffort | None = "low",
        use_cache: bool = True,
    ) -> Completion:
        request = CompletionRequest(
            model=model or self.default_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        return await self._run(request, use_cache)

    async def complete_json[T: BaseModel](
        self,
        messages: list[Message],
        output: type[T],
        *,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        reasoning_effort: ReasoningEffort | None = "low",
        use_cache: bool = True,
    ) -> Parsed[T]:
        """Completion constrained to `output`'s JSON schema, parsed into an instance."""
        request = CompletionRequest(
            model=model or self.default_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            json_schema=strict_schema(output),
            schema_name=output.__name__,
        )
        completion = await self._run(request, use_cache)
        try:
            value = output.model_validate_json(completion.text)
        except ValidationError as e:
            if self._cache is not None:
                self._cache.delete(self.provider.name, request)
            raise OutputValidationError(
                f"{request.model} output doesn't match {output.__name__}: {e}"
            ) from e
        return Parsed(value, completion)

    async def _run(self, request: CompletionRequest, use_cache: bool) -> Completion:
        cache = self._cache if use_cache else None
        if cache is not None and (hit := cache.get(self.provider.name, request)) is not None:
            logger.info("llm cache hit model=%s tokens=%d", hit.model, hit.total_tokens)
            return hit

        estimate = estimate_tokens(request)
        attempt = 0
        while True:
            attempt += 1
            reservation = None
            if self._limiter is not None:
                reservation = await self._limiter.acquire(request.model, estimate)
            start = self._clock()
            try:
                result = await self.provider.complete(request)
            except TransientError as e:
                if reservation is not None:
                    self._limiter.settle(reservation, 0)
                delay = self._retry_delay(e, attempt, request.model)
                if delay is None:
                    raise
                logger.warning(
                    "llm retry model=%s attempt=%d delay=%.1fs: %s",
                    request.model,
                    attempt,
                    delay,
                    e,
                )
                await self._sleep(delay)
                continue
            except BaseException:
                if reservation is not None:
                    self._limiter.settle(reservation, 0)
                raise
            latency_ms = (self._clock() - start) * 1000
            if reservation is not None:
                self._limiter.settle(reservation, result.prompt_tokens + result.completion_tokens)
            break

        if result.finish_reason == "length":
            raise TruncatedOutputError(
                f"{request.model} hit max_tokens={request.max_tokens} "
                f"({result.completion_tokens} completion tokens); raise it or shrink the input"
            )

        completion = Completion(
            text=result.text,
            model=result.model,
            provider=self.provider.name,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=latency_ms,
            attempts=attempt,
        )
        logger.info(
            "llm call model=%s prompt=%d completion=%d latency_ms=%.0f attempts=%d",
            completion.model,
            completion.prompt_tokens,
            completion.completion_tokens,
            completion.latency_ms,
            completion.attempts,
        )
        if cache is not None:
            cache.put(self.provider.name, request, completion)
        return completion

    def _retry_delay(self, error: TransientError, attempt: int, model: str) -> float | None:
        """Seconds to wait before retrying, or None to give up."""
        if attempt >= self._max_attempts:
            return None
        if isinstance(error, RateLimitedError) and error.retry_after is not None:
            if error.retry_after > self._max_retry_wait:
                return None
            if self._limiter is not None:
                # The limiter holds every caller for this model, not just this one.
                self._limiter.pause(model, error.retry_after)
                return 0.0
            return error.retry_after
        return min(2.0 ** (attempt - 1), 30.0) + random.uniform(0, 0.5)

    def usage(self, model: str | None = None) -> dict[str, int | str | None]:
        """Today's use of `model` against the provider's daily budget. Totals are zero
        without a rate limiter, which is what tracks them."""
        model = model or self.default_model
        limits = self.provider.limits(model)
        used = self._limiter.usage(model) if self._limiter else {"tokens": 0, "requests": 0}
        return {
            "model": model,
            "day": used.get("day"),
            "tokens": used["tokens"],
            "requests": used["requests"],
            "token_limit": limits.tpd,
            "request_limit": limits.rpd,
            "resets_at": used.get("resets_at"),
        }

    async def aclose(self) -> None:
        await self.provider.aclose()


@lru_cache
def get_llm_client() -> LLMClient:
    """Client configured from settings (.env)."""
    if settings.llm_provider != "groq":
        raise LLMError(f"unsupported LLM_PROVIDER {settings.llm_provider!r}")
    from app.llm.providers.groq import GroqProvider

    provider = GroqProvider(settings.groq_api_key)
    state_dir = Path(settings.llm_cache_dir)
    return LLMClient(
        provider,
        default_model=settings.analysis_model,
        limiter=RateLimiter(provider.limits, usage_file=state_dir / "usage.json"),
        cache=DiskCache(state_dir) if settings.llm_cache_enabled else None,
    )
