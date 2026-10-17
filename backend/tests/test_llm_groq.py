import json

import httpx2
import pytest

from app.llm.providers.groq import GroqProvider
from app.llm.types import CompletionRequest, LLMError, RateLimitedError, TransientError

pytestmark = pytest.mark.anyio

REQUEST = CompletionRequest(
    model="openai/gpt-oss-20b",
    messages=[{"role": "user", "content": "hi"}],
    max_tokens=256,
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    schema_name="Empty",
)


def make_provider(handler) -> GroqProvider:
    http = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler), base_url=GroqProvider.base_url
    )
    return GroqProvider("test-key", http=http)


async def test_sends_strict_schema_and_parses_usage() -> None:
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "model": "openai/gpt-oss-20b",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 34},
            },
        )

    result = await make_provider(handler).complete(REQUEST)

    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    body = seen["body"]
    assert body["max_completion_tokens"] == 256
    assert body["reasoning_effort"] == "low"
    assert body["include_reasoning"] is False
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "Empty"
    assert (result.text, result.prompt_tokens, result.completion_tokens) == ("{}", 12, 34)


@pytest.mark.parametrize(
    ("status", "body", "headers", "error"),
    [
        (429, {"error": {"message": "slow down"}}, {"retry-after": "2.5"}, RateLimitedError),
        (503, {"error": {"message": "over capacity"}}, {}, TransientError),
        (400, {"error": {"code": "json_validate_failed", "message": "bad"}}, {}, TransientError),
        (401, {"error": {"message": "invalid api key"}}, {}, LLMError),
    ],
)
async def test_maps_http_errors(status, body, headers, error) -> None:
    provider = make_provider(lambda request: httpx2.Response(status, json=body, headers=headers))
    with pytest.raises(error) as info:
        await provider.complete(REQUEST)
    if status == 401:
        assert not isinstance(info.value, TransientError)
    if status == 429:
        assert info.value.retry_after == 2.5


async def test_network_errors_are_transient() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("reset", request=request)

    with pytest.raises(TransientError):
        await make_provider(handler).complete(REQUEST)


def test_requires_an_api_key() -> None:
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        GroqProvider("")
