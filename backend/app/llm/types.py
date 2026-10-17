from dataclasses import dataclass
from typing import Any, Literal, TypedDict


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


ReasoningEffort = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    messages: list[Message]
    max_tokens: int = 2048
    temperature: float = 0.0
    reasoning_effort: ReasoningEffort | None = "low"
    # When set, the provider must return JSON matching this schema (strict mode).
    json_schema: dict[str, Any] | None = None
    schema_name: str = "output"


@dataclass(frozen=True)
class ProviderResult:
    text: str
    finish_reason: str | None
    model: str
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    attempts: int
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class Limits:
    rpm: int
    tpm: int
    rpd: int
    tpd: int


class LLMError(Exception):
    pass


class TransientError(LLMError):
    """Worth retrying: timeouts, 5xx, connection resets."""


class RateLimitedError(TransientError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BudgetExceededError(LLMError):
    """The request can't fit in the provider's per-minute or per-day token budget."""


class TruncatedOutputError(LLMError):
    """The model hit max_tokens before finishing."""


class OutputValidationError(LLMError):
    pass
