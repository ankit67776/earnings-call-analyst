from typing import Protocol

from app.llm.types import CompletionRequest, Limits, ProviderResult


class Provider(Protocol):
    """One LLM API. Raises TransientError (incl. RateLimitedError) for retryable
    failures and LLMError for anything else."""

    name: str

    def limits(self, model: str) -> Limits: ...

    async def complete(self, request: CompletionRequest) -> ProviderResult: ...

    async def aclose(self) -> None: ...
