import json

import pytest

from app.analysis.chunking import Chunk
from app.analysis.extract import (
    Citation,
    ClaimType,
    Finding,
    analyze_section,
    reduce_findings,
    render_chunk,
)
from app.filings.tenk import Paragraph, Section
from app.llm.client import LLMClient
from app.llm.types import BudgetExceededError, ProviderResult, TruncatedOutputError
from tests.fakes import FakeTime

pytestmark = pytest.mark.anyio

P1 = "Revenue increased 38% to $116.2 million in 2020, driven by mobile and IoT demand."
P2 = "Apple accounted for approximately 24% of our revenue in 2020."
P3 = "We expect revenue to grow in 2021. Revenue rose 38% to $116.2 million last year."

SECTION = Section(
    "7",
    "Management's Discussion and Analysis",
    [
        Paragraph("7-aaa", "7", 1, "heading", "Results of Operations"),
        Paragraph("7-bbb", "7", 2, "text", P1),
        Paragraph("7-ccc", "7", 3, "text", P2),
        Paragraph("7-ddd", "7", 4, "heading", "Outlook"),
        Paragraph("7-eee", "7", 5, "text", P3),
    ],
)


def claims(*items: tuple[str, str, str, str]) -> ProviderResult:
    payload = {
        "claims": [
            {"type": t, "paragraph_id": pid, "quote": quote, "claim": claim}
            for t, pid, quote, claim in items
        ]
    }
    return ProviderResult(json.dumps(payload), "stop", "fake-model", 100, 50)


class ByContentProvider:
    """Answers each chunk by a marker in its text, so concurrency can't reorder replies."""

    name = "fake"

    def __init__(self, replies: dict[str, ProviderResult | Exception]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    def limits(self, model):  # pragma: no cover - no limiter in these tests
        raise NotImplementedError

    async def complete(self, request):
        prompt = request.messages[-1]["content"]
        self.prompts.append(prompt)
        reply = next(r for marker, r in self.replies.items() if marker in prompt)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def aclose(self) -> None:
        pass


def make_client(provider) -> LLMClient:
    time = FakeTime()
    return LLMClient(provider, default_model="fake-model", sleep=time.sleep, clock=time.clock)


REPLIES = {
    "[7-bbb]": claims(
        ("metric", "7-bbb", "Revenue increased 38% to $116.2 million in 2020", "Revenue rose 38%."),
        # Cited the wrong paragraph; the quote is in 7-ccc.
        ("metric", "7-bbb", "Apple accounted for approximately 24% of our revenue", "Apple 24%."),
        ("risk", "7-ccc", "We depend on a single foundry.", "Foundry risk."),  # made up
    ),
    "[7-eee]": claims(
        ("guidance", "7-eee", "We expect revenue to grow in 2021.", "Revenue to grow in 2021."),
        # Same claim as the first one, stated again in another paragraph.
        ("metric", "7-eee", "Revenue rose 38% to $116.2 million last year.", "Revenue rose 38%."),
    ),
}


async def test_map_ground_and_reduce() -> None:
    provider = ByContentProvider(REPLIES)
    analysis = await analyze_section(make_client(provider), SECTION, company="Acme", max_chars=200)

    assert analysis.chunks == 2
    assert [(f.type, f.claim) for f in analysis.findings] == [
        (ClaimType.metric, "Revenue rose 38%."),
        (ClaimType.metric, "Apple 24%."),
        (ClaimType.guidance, "Revenue to grow in 2021."),
    ]
    revenue, apple, _ = analysis.findings
    assert [(c.paragraph_id, c.match) for c in revenue.citations] == [
        ("7-bbb", "exact"),
        ("7-eee", "exact"),
    ]
    assert apple.citations[0].paragraph_id == "7-ccc"  # re-attributed to the real paragraph
    assert analysis.misattributed == 1
    assert apple.citations[0].source_text == "Apple accounted for approximately 24% of our revenue"

    assert [f.claim for f in analysis.rejected] == ["Foundry risk."]
    assert analysis.match_counts() == {
        "exact": 4,
        "normalized": 0,
        "elided": 0,
        "fuzzy": 0,
        "unverified": 1,
    }
    assert (analysis.llm_calls, analysis.prompt_tokens, analysis.completion_tokens) == (2, 200, 100)


async def test_a_failed_chunk_is_reported_and_the_rest_still_run() -> None:
    provider = ByContentProvider(
        {"[7-bbb]": TruncatedOutputError("hit max_tokens"), "[7-eee]": REPLIES["[7-eee]"]}
    )
    analysis = await analyze_section(make_client(provider), SECTION, max_chars=200)
    assert analysis.failed_chunks == [(0, "hit max_tokens")]
    assert len(analysis.findings) == 2


async def test_reports_progress_before_and_after_each_chunk() -> None:
    seen: list[tuple[int, int]] = []

    async def progress(done: int, total: int) -> None:
        seen.append((done, total))

    provider = ByContentProvider(REPLIES)
    await analyze_section(make_client(provider), SECTION, max_chars=200, on_progress=progress)
    assert seen == [(0, 2), (1, 2), (2, 2)]


async def test_running_out_of_daily_budget_stops_the_run() -> None:
    provider = ByContentProvider({"[7-": BudgetExceededError("daily budget used up")})
    with pytest.raises(BudgetExceededError):
        await analyze_section(make_client(provider), SECTION, max_chars=200)


def test_render_chunk_labels_paragraphs_and_context() -> None:
    chunk = Chunk("7", "MD&A", SECTION.paragraphs[1:3], context_heading="Results of Operations")
    text = render_chunk(chunk, "Acme")
    assert text.splitlines()[:3] == [
        "Company: Acme",
        "Section: Item 7. MD&A",
        '(Continues under the heading "Results of Operations")',
    ]
    assert f"[7-bbb] {P1}" in text and f"[7-ccc] {P2}" in text


def test_reduce_keeps_claims_with_different_numbers_apart() -> None:
    def finding(claim: str, pid: str, start: int) -> Finding:
        citation = Citation(pid, "q", "exact", start, start + 10, "q")
        return Finding(ClaimType.metric, claim, [citation])

    merged = reduce_findings(
        [
            finding("Revenue increased 38% in 2020.", "7-a", 0),
            finding("Revenue increased 31% in 2020.", "7-b", 0),
            finding("In 2020 revenue increased 38%.", "7-c", 0),
            # Quoted from the same sentence as the first, but a different claim.
            finding("Gross margin was 50% in 2020.", "7-a", 5),
            # Same sentence, same numbers, reworded: merged, citation not repeated.
            finding("2020 revenue grew by 38%.", "7-a", 3),
        ]
    )
    assert [len(f.citations) for f in merged] == [2, 1, 1]
    assert [c.paragraph_id for c in merged[0].citations] == ["7-a", "7-c"]
    assert merged[2].claim == "Gross margin was 50% in 2020."
