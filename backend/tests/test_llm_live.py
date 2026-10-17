"""Real API calls. Spends a few hundred tokens: uv run pytest -m live"""

import pytest
from pydantic import BaseModel

from app.config import settings
from app.llm.client import LLMClient
from app.llm.providers.groq import GroqProvider
from app.llm.ratelimit import RateLimiter

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(not settings.groq_api_key, reason="GROQ_API_KEY not set"),
]


class Item(BaseModel):
    name: str
    amount_usd_millions: float | None


class Extraction(BaseModel):
    items: list[Item]


@pytest.mark.parametrize("model", [settings.analysis_model, settings.live_model])
async def test_groq_strict_json_round_trip(model: str) -> None:
    provider = GroqProvider(settings.groq_api_key)
    client = LLMClient(provider, default_model=model, limiter=RateLimiter(provider.limits))
    try:
        parsed = await client.complete_json(
            [
                {"role": "system", "content": "Extract every revenue line item."},
                {
                    "role": "user",
                    "content": "Product revenue was $412.5 million and services revenue "
                    "was $88 million. Licensing revenue was not disclosed.",
                },
            ],
            Extraction,
            max_tokens=1024,
            use_cache=False,
        )
    finally:
        await client.aclose()

    amounts = {item.amount_usd_millions for item in parsed.value.items}
    assert {412.5, 88.0} <= amounts
    assert parsed.completion.prompt_tokens > 0 and parsed.completion.completion_tokens > 0
