from dataclasses import dataclass
from typing import Literal

# Retrieval models embed queries and documents differently.
InputType = Literal["query", "document"]


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    # As reported by the provider; None if it doesn't say.
    tokens: int | None


class EmbeddingError(Exception):
    pass


class TransientEmbeddingError(EmbeddingError):
    """Worth retrying: rate limits, timeouts, 5xx."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
