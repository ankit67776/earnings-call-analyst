import json

import httpx2
import pytest

from app.embeddings import EmbeddingClient, TransientEmbeddingError, VectorCache
from app.embeddings.providers.gemini import GeminiProvider
from app.embeddings.providers.voyage import VoyageProvider
from app.embeddings.types import EmbedResult
from app.llm.ratelimit import RateLimiter
from app.llm.types import Limits
from tests.fakes import FakeTime

pytestmark = pytest.mark.anyio


class FakeEmbedder:
    """Returns [len(text), index] vectors; an Exception in the script is raised instead."""

    name = "fake"
    max_batch = 3
    max_batch_tokens = 1_000

    def __init__(self, *script: Exception, counts_texts: bool = False, limits=None) -> None:
        self.script = list(script)
        self.batches: list[list[str]] = []
        self.counts_texts_as_requests = counts_texts
        self._limits = limits or Limits(rpm=100, tpm=100_000, rpd=10_000, tpd=1_000_000)

    def limits(self, model: str) -> Limits:
        return self._limits

    async def embed(self, model, texts, input_type, dimensions) -> EmbedResult:
        if self.script:
            raise self.script.pop(0)
        self.batches.append(list(texts))
        return EmbedResult([[float(len(t)), float(i)] for i, t in enumerate(texts)], 5)

    async def aclose(self) -> None:
        pass


async def test_batches_by_count_and_caches_by_text(tmp_path) -> None:
    provider = FakeEmbedder()
    client = EmbeddingClient(provider, "m", cache=VectorCache(tmp_path / "v.sqlite"))
    texts = ["a", "bb", "ccc", "dddd", "a"]

    vectors = await client.embed(texts, "document")
    again = await client.embed(texts, "document")

    assert provider.batches == [["a", "bb", "ccc"], ["dddd"]]  # the repeat is embedded once
    assert vectors == again
    assert vectors[0] == vectors[4] == [1.0, 0.0]
    assert client.stats.cached == 5  # the whole second call
    assert client.key == "fake/m"


async def test_queries_and_documents_are_cached_apart(tmp_path) -> None:
    provider = FakeEmbedder()
    client = EmbeddingClient(provider, "m", cache=VectorCache(tmp_path / "v.sqlite"))
    await client.embed(["same"], "document")
    await client.embed_query("same")
    assert len(provider.batches) == 2


async def test_batches_fit_the_per_minute_limits() -> None:
    limits = Limits(rpm=2, tpm=10, rpd=1_000, tpd=1_000_000)
    provider = FakeEmbedder(counts_texts=True, limits=limits)
    time = FakeTime()
    limiter = RateLimiter(provider.limits, clock=time.clock, sleep=time.sleep)
    client = EmbeddingClient(provider, "m", limiter=limiter)
    # ~3 tokens each by the client's estimate: at most 2 texts a minute and 9 tokens a batch.
    await client.embed(["x" * 6, "y" * 6, "z" * 6], "document")
    assert [len(b) for b in provider.batches] == [2, 1]
    assert time.now == pytest.approx(60.0)  # the second batch waited for the window


async def test_retries_transient_errors() -> None:
    time = FakeTime()
    provider = FakeEmbedder(TransientEmbeddingError("busy", retry_after=7))
    client = EmbeddingClient(provider, "m", sleep=time.sleep)
    assert await client.embed_query("hello") == [5.0, 0.0]
    assert time.sleeps[0] >= 7


def mock(handler, base_url: str) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), base_url=base_url)


async def test_voyage_request_and_response() -> None:
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "data": [{"embedding": [0.2], "index": 1}, {"embedding": [0.1], "index": 0}],
                "usage": {"total_tokens": 9},
            },
        )

    provider = VoyageProvider("k", http=mock(handler, VoyageProvider.base_url))
    result = await provider.embed("voyage-4", ["a", "b"], "query", 1024)
    assert seen["body"] == {
        "model": "voyage-4", "input": ["a", "b"], "input_type": "query", "output_dimension": 1024
    }  # fmt: skip
    assert result == EmbedResult([[0.1], [0.2]], 9)


async def test_gemini_prefixes_tasks_for_new_models_and_reads_retry_delay() -> None:
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.path, json.loads(request.content)))
        if len(seen) == 3:
            return httpx2.Response(
                429,
                json={
                    "error": {
                        "message": "quota",
                        "details": [{"@type": "RetryInfo", "retryDelay": "6s"}],
                    }
                },
            )
        return httpx2.Response(200, json={"embeddings": [{"values": [0.5]}]})

    provider = GeminiProvider("k", http=mock(handler, GeminiProvider.base_url))
    await provider.embed("gemini-embedding-2", ["What grew?"], "query", 1024)
    await provider.embed("gemini-embedding-001", ["Sales grew."], "document", 768)
    with pytest.raises(TransientEmbeddingError) as e:
        await provider.embed("gemini-embedding-2", ["x"], "document", 1024)

    path, body = seen[0]
    assert path.endswith("/models/gemini-embedding-2:batchEmbedContents")
    [request] = body["requests"]
    assert request["content"]["parts"][0]["text"] == "task: search result | query: What grew?"
    assert request["outputDimensionality"] == 1024
    [old] = seen[1][1]["requests"]
    assert old["taskType"] == "RETRIEVAL_DOCUMENT"
    assert old["content"]["parts"][0]["text"] == "Sales grew."
    assert e.value.retry_after == 6.0
