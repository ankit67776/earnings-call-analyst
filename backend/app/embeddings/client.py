import asyncio
import hashlib
import logging
import random
import sqlite3
from array import array
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.embeddings.providers import EmbeddingProvider
from app.embeddings.types import EmbeddingError, InputType, TransientEmbeddingError
from app.llm.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

# Every model is asked for vectors of this size, so they share one pgvector column.
DIMENSIONS = 1024


class VectorCache:
    """Embeddings already computed, so re-indexing and re-running evals cost no quota.

    One SQLite file rather than a file per vector: there are thousands of them.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The client can be built in one thread (FastAPI runs sync dependencies in a pool)
        # and used from the event loop's; calls never overlap, so sharing is safe.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "create table if not exists vectors (key text primary key, vector blob not null)"
        )

    def get(self, keys: list[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        for i in range(0, len(keys), 500):
            batch = keys[i : i + 500]
            marks = ",".join("?" * len(batch))
            for key, blob in self._db.execute(
                f"select key, vector from vectors where key in ({marks})", batch
            ):
                found[key] = array("f", blob).tolist()
        return found

    def put(self, items: dict[str, list[float]]) -> None:
        self._db.executemany(
            "insert or replace into vectors (key, vector) values (?, ?)",
            [(k, array("f", v).tobytes()) for k, v in items.items()],
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


@dataclass
class EmbeddingStats:
    texts: int = 0
    cached: int = 0
    requests: int = 0
    tokens: int = 0


class EmbeddingClient:
    """The one entry point pipelines use to embed text.

    Adds what every call needs on top of a provider: batching within the provider's
    request and per-minute limits, client-side rate limiting, retries with backoff and an
    on-disk vector cache.
    """

    def __init__(
        self,
        provider: EmbeddingProvider,
        model: str,
        *,
        dimensions: int = DIMENSIONS,
        limiter: RateLimiter | None = None,
        cache: VectorCache | None = None,
        max_attempts: int = 6,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self.model = model
        self.dimensions = dimensions
        self.stats = EmbeddingStats()
        self._limiter = limiter
        self._cache = cache
        self._max_attempts = max_attempts
        self._sleep = sleep

    @property
    def key(self) -> str:
        """Names the vector space: vectors from different keys must never be compared."""
        return f"{self.provider.name}/{self.model}"

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed([text], "query"))[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.embed(texts, "document")

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        keys = [self._cache_key(t, input_type) for t in texts]
        found = self._cache.get(list(set(keys))) if self._cache is not None else {}
        self.stats.texts += len(texts)
        self.stats.cached += sum(k in found for k in keys)
        missing = list({k: t for k, t in zip(keys, texts, strict=True) if k not in found}.items())
        for batch in self._batches(missing):
            vectors = await self._embed_batch([t for _, t in batch], input_type)
            fresh = {k: v for (k, _), v in zip(batch, vectors, strict=True)}
            if self._cache is not None:
                self._cache.put(fresh)
            found |= fresh
        return [found[k] for k in keys]

    def _batches(self, items: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
        limits = self.provider.limits(self.model)
        max_texts = self.provider.max_batch
        if self.provider.counts_texts_as_requests:
            max_texts = min(max_texts, limits.rpm)
        max_tokens = int(0.9 * min(self.provider.max_batch_tokens, limits.tpm))
        batches: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        tokens = 0
        for item in items:
            cost = _estimate_tokens(item[1])
            if current and (len(current) >= max_texts or tokens + cost > max_tokens):
                batches.append(current)
                current, tokens = [], 0
            current.append(item)
            tokens += cost
        if current:
            batches.append(current)
        return batches

    async def _embed_batch(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        estimate = sum(_estimate_tokens(t) for t in texts)
        requests = len(texts) if self.provider.counts_texts_as_requests else 1
        attempt = 0
        while True:
            attempt += 1
            reservation = None
            if self._limiter is not None:
                reservation = await self._limiter.acquire(self.model, estimate, requests)
            try:
                result = await self.provider.embed(self.model, texts, input_type, self.dimensions)
            except TransientEmbeddingError as e:
                if reservation is not None:
                    self._limiter.settle(reservation, 0, served=False)
                if attempt >= self._max_attempts:
                    raise
                delay = e.retry_after or min(5.0 * 2 ** (attempt - 1), 60.0)
                delay += random.uniform(0, 1)
                logger.warning(
                    "embedding retry model=%s attempt=%d delay=%.0fs: %s",
                    self.model,
                    attempt,
                    delay,
                    e,
                )
                if self._limiter is not None:
                    self._limiter.pause(self.model, delay)
                else:
                    await self._sleep(delay)
                continue
            except BaseException:
                if reservation is not None:
                    self._limiter.settle(reservation, 0)
                raise
            break
        tokens = result.tokens if result.tokens is not None else estimate
        if reservation is not None:
            self._limiter.settle(reservation, tokens)
        if len(result.vectors) != len(texts):
            raise EmbeddingError(
                f"{self.key} returned {len(result.vectors)} vectors for {len(texts)} texts"
            )
        self.stats.requests += 1
        self.stats.tokens += tokens
        logger.info("embedded %d texts with %s (%d tokens)", len(texts), self.key, tokens)
        return result.vectors

    def _cache_key(self, text: str, input_type: InputType) -> str:
        blob = f"{self.key}\n{self.dimensions}\n{input_type}\n{text}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    async def aclose(self) -> None:
        await self.provider.aclose()
        if self._cache is not None:
            self._cache.close()


def _estimate_tokens(text: str) -> int:
    # Filings are dense with numbers, which tokenize finer than prose.
    return len(text) // 3 + 1


@lru_cache
def get_embedding_client(spec: str | None = None) -> EmbeddingClient:
    """Client for "provider/model" (default: EMBEDDING_MODEL from .env)."""
    spec = spec or settings.embedding_model
    provider_name, _, model = spec.partition("/")
    provider: EmbeddingProvider
    if provider_name == "voyage":
        from app.embeddings.providers.voyage import VoyageProvider

        provider = VoyageProvider(settings.voyage_api_key)
    elif provider_name == "gemini":
        from app.embeddings.providers.gemini import GeminiProvider

        provider = GeminiProvider(settings.gemini_api_key)
    else:
        raise EmbeddingError(f"unsupported embedding provider in {spec!r}")
    if not model:
        raise EmbeddingError(f"embedding model {spec!r} should look like 'voyage/voyage-4'")
    state = Path(settings.embedding_cache_dir)
    return EmbeddingClient(
        provider,
        model,
        limiter=RateLimiter(provider.limits, usage_file=state / f"usage-{provider.name}.json"),
        cache=VectorCache(state / "vectors.sqlite"),
    )
