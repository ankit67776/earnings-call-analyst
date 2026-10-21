import json

import pytest

from app.analysis.chunking import Chunk
from app.calls.transcript import LabeledSegment
from app.contradictions.check import Claim, render, verify
from app.contradictions.claims import render_call_chunk, render_speakers
from app.filings.tenk import Paragraph
from app.llm.client import LLMClient
from app.retrieval.search import Hit
from tests.fakes import FakeProvider, result

pytestmark = pytest.mark.anyio


def filing_hit(i: int, title: str, text: str) -> Hit:
    return Hit(
        passage_id=i, ticker="ACME", source="filing", document_id=7, call_id=None, title=title,
        text=text, section="7", paragraph_ids=None, first_segment=None, last_segment=None,
        start=None, end=None, speakers=None, score=0.1,
    )  # fmt: skip


DEBT = "At December 31, 2020, we had $35.0 million outstanding under our credit facility."
FOUNDRY = "We rely on a single foundry, Bosch, to manufacture our MEMS wafers."
WEATHER = "Severe weather could disrupt our operations."
HITS = [
    filing_hit(1, "Item 7. MD&A › Liquidity", DEBT),
    filing_hit(2, "Item 1A. Risk Factors", FOUNDRY),
    filing_hit(3, "Item 1A. Risk Factors", WEATHER),
]
CLAIM = Claim("metric", "Acme had no bank debt at the end of 2020.", "Jo", "we have no bank debt")
FILINGS = {7: "10-K for fiscal 2020, filed 2021-02-16"}


def reply(*judgments: dict) -> LLMClient:
    payload = json.dumps({"judgments": list(judgments)})
    return LLMClient(FakeProvider(result(payload)), default_model="m")


def judgment(source: str, verdict: str, quote: str = "", reason: str = "r") -> dict:
    return {"source": source, "reason": reason, "verdict": verdict, "filing_quote": quote}


async def check(llm: LLMClient):
    return await verify(
        llm, CLAIM, HITS, company="Acme Corp", call="Q3 2020 earnings call", filings=FILINGS
    )


async def test_a_contradiction_is_flagged_only_with_its_10k_quote_found() -> None:
    out = await check(
        reply(
            judgment("F1", "contradiction", "we had $35.0 million outstanding"),
            judgment("[f2]", "consistent", "We rely on a single foundry"),
            judgment("F3", "contradiction", "we had no debt at all"),  # not in the passage
        )
    )
    first, second, third = out.pairs
    assert (first.verdict, first.match, first.source_text) == (
        "contradiction", "exact", "we had $35.0 million outstanding"
    )  # fmt: skip
    assert first.flagged and first.hit.passage_id == 1
    assert (second.verdict, second.match, second.flagged) == ("consistent", "exact", False)
    assert (third.verdict, third.match, third.flagged) == ("contradiction", None, False)
    assert out.contradictions == [first]


async def test_a_passage_the_verifier_skips_has_no_verdict() -> None:
    out = await check(reply(judgment("F2", "unrelated"), judgment("F2", "contradiction", "Bosch")))
    assert [p.verdict for p in out.pairs] == [None, "unrelated", None]  # first F2 wins
    assert out.contradictions == []


async def test_no_candidates_means_no_llm_call() -> None:
    provider = FakeProvider()
    out = await verify(
        LLMClient(provider, default_model="m"), CLAIM, [], company="Acme", call="c", filings={}
    )
    assert out.pairs == [] and provider.requests == []


def test_render_shows_the_claim_and_labels_each_passage() -> None:
    text = render(CLAIM, HITS[:2], company="Acme", call="Q3 2020 earnings call", filings=FILINGS)
    assert 'On the Q3 2020 earnings call, Jo said (metric):\nQuote: "we have no bank debt"' in text
    assert f"[F1] 10-K for fiscal 2020, filed 2021-02-16 · Item 7. MD&A › Liquidity\n{DEBT}" in text
    assert "[F2]" in text and "[F3]" not in text


def test_render_speakers_shows_how_each_speaker_is_introduced() -> None:
    segments = [
        LabeledSegment(1, 1, "Operator", 0, 5, "Welcome. I'll hand over to Jo, our CFO."),
        LabeledSegment(2, 2, "Jo", 5, 9, "Thanks. Revenue grew."),
        LabeledSegment(3, 3, "Operator", 9, 12, "Our first question comes from Al with Big Bank."),
        LabeledSegment(4, 4, "Al", 12, 15, "How is demand?"),
        LabeledSegment(5, 5, "Jo", 15, 18, "Strong."),
    ]
    text = render_speakers("Acme Corp", segments)
    assert text.startswith(
        "Company: Acme Corp\n\nOpening of the call:\n"
        "Operator: Welcome. I'll hand over to Jo, our CFO.\n\nJo: Thanks. Revenue grew."
    )
    assert (
        "[1] Operator, 17 words in all.\n"
        "  Just before they first speak: nothing, they open the call\n"
        '  They begin: "Welcome. I\'ll hand over to Jo, our CFO. ..."'
    ) in text
    assert text.endswith(
        "[3] Al, 3 words in all.\n"
        "  Just before they first speak: \"... Welcome. I'll hand over to Jo, our CFO. Thanks. "
        'Revenue grew. Our first question comes from Al with Big Bank."\n'
        '  They begin: "How is demand? ..."'
    )


def test_render_call_chunk_labels_passages_with_their_speakers() -> None:
    chunk = Chunk(
        "call",
        "Q3 2020 earnings call",
        [
            Paragraph("P1", "call", 1, "text", "Operator: Welcome."),
            Paragraph("P2", "call", 2, "text", "Jo CFO: We have no bank debt."),
        ],
    )
    assert render_call_chunk(chunk, "Acme Corp") == (
        "Company: Acme Corp\n"
        "Transcript of the Q3 2020 earnings call, consecutive passages:\n"
        "\n[P1] Operator: Welcome.\n"
        "\n[P2] Jo CFO: We have no bank debt."
    )
