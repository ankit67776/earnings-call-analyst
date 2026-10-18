from typing import Any

import httpx2

from app.embeddings.types import EmbeddingError, EmbedResult, InputType, TransientEmbeddingError
from app.llm.types import Limits

# Accounts without a payment method get 3 RPM and 10K TPM (the free token allowance
# still applies once one is added, with limits in the millions of tokens a minute).
# https://docs.voyageai.com/docs/rate-limits
NO_PAYMENT_METHOD_LIMITS = Limits(rpm=3, tpm=10_000, rpd=1_000_000, tpd=1_000_000_000)


class VoyageProvider:
    name = "voyage"
    base_url = "https://api.voyageai.com/v1"
    max_batch = 1000
    max_batch_tokens = 120_000
    counts_texts_as_requests = False

    def __init__(
        self,
        api_key: str,
        *,
        limits: Limits = NO_PAYMENT_METHOD_LIMITS,
        http: httpx2.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise EmbeddingError("VOYAGE_API_KEY is not set")
        self._limits = limits
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http or httpx2.AsyncClient(base_url=self.base_url, timeout=timeout)

    def limits(self, model: str) -> Limits:
        return self._limits

    async def embed(
        self, model: str, texts: list[str], input_type: InputType, dimensions: int
    ) -> EmbedResult:
        payload: dict[str, Any] = {
            "model": model,
            "input": texts,
            "input_type": input_type,
            "output_dimension": dimensions,
        }
        try:
            response = await self._http.post("/embeddings", json=payload, headers=self._headers)
        except (httpx2.TimeoutException, httpx2.TransportError) as e:
            raise TransientEmbeddingError(f"voyage request failed: {e!r}") from e
        status = response.status_code
        if status == 429 or status >= 500:
            raise TransientEmbeddingError(
                f"voyage {status}: {_detail(response)}", retry_after=_retry_after(response)
            )
        if status >= 400:
            raise EmbeddingError(f"voyage {status}: {_detail(response)}")
        data = response.json()
        rows = sorted(data["data"], key=lambda d: d["index"])
        return EmbedResult(
            vectors=[r["embedding"] for r in rows],
            tokens=(data.get("usage") or {}).get("total_tokens"),
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def _retry_after(response: httpx2.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _detail(response: httpx2.Response) -> str:
    try:
        return str(response.json().get("detail") or response.text[:300])
    except ValueError:
        return response.text[:300]
