from typing import Protocol

from app.embeddings.types import EmbedResult, InputType
from app.llm.types import Limits


class EmbeddingProvider(Protocol):
    """One embedding API. Raises TransientEmbeddingError for retryable failures and
    EmbeddingError for anything else."""

    name: str
    # Most texts and estimated tokens one request may carry.
    max_batch: int
    max_batch_tokens: int
    # True if the provider's per-minute and per-day request limits count each text of a
    # batch rather than each HTTP request.
    counts_texts_as_requests: bool

    def limits(self, model: str) -> Limits: ...

    async def embed(
        self, model: str, texts: list[str], input_type: InputType, dimensions: int
    ) -> EmbedResult: ...

    async def aclose(self) -> None: ...
