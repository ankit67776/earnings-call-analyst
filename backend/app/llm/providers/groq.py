from typing import Any

import httpx2

from app.llm.types import (
    CompletionRequest,
    Limits,
    LLMError,
    ProviderResult,
    RateLimitedError,
    TransientError,
)

# Free-tier limits from https://console.groq.com/docs/rate-limits; check your
# organization's limits page if you're on a paid plan.
FREE_TIER_LIMITS = {
    "openai/gpt-oss-120b": Limits(rpm=30, tpm=8_000, rpd=1_000, tpd=200_000),
    "openai/gpt-oss-20b": Limits(rpm=30, tpm=8_000, rpd=1_000, tpd=200_000),
    # Also caps output at 1,000 tokens a minute; keep its max_tokens small.
    "qwen/qwen3.8-27b": Limits(rpm=30, tpm=8_000, rpd=1_000, tpd=200_000),
}
DEFAULT_LIMITS = Limits(rpm=30, tpm=6_000, rpd=1_000, tpd=100_000)


class GroqProvider:
    """Groq's OpenAI-compatible chat completions API."""

    name = "groq"
    base_url = "https://api.groq.com/openai/v1"

    def __init__(
        self, api_key: str, *, http: httpx2.AsyncClient | None = None, timeout: float = 120.0
    ) -> None:
        if not api_key:
            raise LLMError("GROQ_API_KEY is not set")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._http = http or httpx2.AsyncClient(base_url=self.base_url, timeout=timeout)

    def limits(self, model: str) -> Limits:
        return FREE_TIER_LIMITS.get(model, DEFAULT_LIMITS)

    async def complete(self, request: CompletionRequest) -> ProviderResult:
        try:
            response = await self._http.post(
                "/chat/completions", json=_payload(request), headers=self._headers
            )
        except (httpx2.TimeoutException, httpx2.TransportError) as e:
            raise TransientError(f"groq request failed: {e!r}") from e

        status = response.status_code
        if status == 429:
            raise RateLimitedError(
                f"groq rate limited: {_error_message(response)}",
                retry_after=_retry_after(response),
            )
        if status >= 500 or status in (408, 409):
            raise TransientError(f"groq {status}: {_error_message(response)}")
        if status >= 400:
            if _error_code(response) == "json_validate_failed":
                raise TransientError(f"groq {status}: {_error_message(response)}")
            raise LLMError(f"groq {status}: {_error_message(response)}")

        data = response.json()
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return ProviderResult(
            text=choice["message"].get("content") or "",
            finish_reason=choice.get("finish_reason"),
            model=data.get("model", request.model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def _payload(request: CompletionRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": request.messages,
        "temperature": request.temperature,
        "max_completion_tokens": request.max_tokens,
    }
    if request.reasoning_effort is not None:
        payload["reasoning_effort"] = request.reasoning_effort
        # Reasoning tokens still count toward usage; we just don't need the text back.
        payload["include_reasoning"] = False
    if request.json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": request.schema_name,
                "strict": True,
                "schema": request.json_schema,
            },
        }
    return payload


def _retry_after(response: httpx2.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _error_body(response: httpx2.Response) -> dict[str, Any]:
    try:
        error = response.json().get("error")
    except ValueError:
        return {}
    return error if isinstance(error, dict) else {}


def _error_code(response: httpx2.Response) -> str | None:
    return _error_body(response).get("code")


def _error_message(response: httpx2.Response) -> str:
    return _error_body(response).get("message") or response.text[:300]
