import re
from typing import Any

import httpx2

from app.embeddings.types import EmbeddingError, EmbedResult, InputType, TransientEmbeddingError
from app.llm.types import Limits

# Free tier, per model: each text in a batch counts as one request. The per-minute figure
# is from the API's own quota errors; the daily one is the commonly published free-tier
# figure, and the API's 429s are the backstop if it's lower.
FREE_TIER_LIMITS = Limits(rpm=100, tpm=1_000_000, rpd=1_000, tpd=100_000_000)


class GeminiProvider:
    """Gemini API embeddings (batchEmbedContents)."""

    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    max_batch = 100
    max_batch_tokens = 200_000
    counts_texts_as_requests = True

    def __init__(
        self,
        api_key: str,
        *,
        limits: Limits = FREE_TIER_LIMITS,
        http: httpx2.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise EmbeddingError("GEMINI_API_KEY is not set")
        self._limits = limits
        self._headers = {"x-goog-api-key": api_key}
        self._http = http or httpx2.AsyncClient(base_url=self.base_url, timeout=timeout)

    def limits(self, model: str) -> Limits:
        return self._limits

    async def embed(
        self, model: str, texts: list[str], input_type: InputType, dimensions: int
    ) -> EmbedResult:
        payload = {"requests": [_request(model, t, input_type, dimensions) for t in texts]}
        try:
            response = await self._http.post(
                f"/models/{model}:batchEmbedContents", json=payload, headers=self._headers
            )
        except (httpx2.TimeoutException, httpx2.TransportError) as e:
            raise TransientEmbeddingError(f"gemini request failed: {e!r}") from e
        status = response.status_code
        if status == 429 or status >= 500:
            error = _error(response)
            raise TransientEmbeddingError(
                f"gemini {status}: {error.get('message', '')[:300]}",
                retry_after=_retry_delay(error),
            )
        if status >= 400:
            raise EmbeddingError(f"gemini {status}: {_error(response).get('message', '')[:300]}")
        data = response.json()
        usage = data.get("usageMetadata") or {}
        return EmbedResult(
            vectors=[e["values"] for e in data["embeddings"]],
            tokens=usage.get("promptTokenCount"),
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def _request(model: str, text: str, input_type: InputType, dimensions: int) -> dict[str, Any]:
    request: dict[str, Any] = {"model": f"models/{model}", "outputDimensionality": dimensions}
    if model.startswith("gemini-embedding-001"):
        request["taskType"] = "RETRIEVAL_QUERY" if input_type == "query" else "RETRIEVAL_DOCUMENT"
    else:
        # Newer models take the task as a prefix of the text instead of a task type.
        if input_type == "query":
            text = f"task: search result | query: {text}"
        else:
            text = f"title: none | text: {text}"
    request["content"] = {"parts": [{"text": text}]}
    return request


def _error(response: httpx2.Response) -> dict[str, Any]:
    try:
        error = response.json().get("error")
    except ValueError:
        return {"message": response.text[:300]}
    return error if isinstance(error, dict) else {}


def _retry_delay(error: dict[str, Any]) -> float | None:
    for detail in error.get("details") or []:
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and (m := re.fullmatch(r"([\d.]+)s", delay)):
            return float(m.group(1))
    return None
