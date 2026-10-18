import json

import pytest

from app.chat.answer import REFUSAL, answer, render_sources, to_json
from app.llm.client import LLMClient
from app.retrieval.search import Hit
from tests.fakes import FakeProvider, result

pytestmark = pytest.mark.anyio


def hit(i: int, source: str, title: str, text: str) -> Hit:
    return Hit(
        passage_id=i, ticker="HSY", source=source, document_id=1 if source == "filing" else None,
        call_id=2 if source == "call" else None, title=title, text=text,
        section="7" if source == "filing" else None, paragraph_ids=None,
        first_segment=None, last_segment=None, start=None, end=None, speakers=None, score=0.1,
    )  # fmt: skip


SALES = "Net sales increased 2.3% in 2020, driven by price realization."
HALLOWEEN = "Michele Buck: We expect Halloween to be down mid-single digits."
HITS = [
    hit(10, "filing", "Item 7. MD&A", SALES),
    hit(11, "call", "Q3 2020 earnings call, 5:02–6:10", HALLOWEEN),
]


def reply(payload: dict) -> LLMClient:
    return LLMClient(FakeProvider(result(json.dumps(payload))), default_model="m")


async def test_every_kept_sentence_has_a_verified_quote() -> None:
    llm = reply(
        {
            "answerable": True,
            "sentences": [
                {
                    "text": "Hershey's net sales rose 2.3% in 2020.",
                    "support": [{"source": "S1", "quote": "Net sales increased 2.3% in 2020"}],
                },
                {
                    "text": "Michele Buck expected Halloween to be down mid-single digits.",
                    # Wrong label: found in S2 instead.
                    "support": [{"source": "S1", "quote": "Halloween to be down"}],
                },
                {
                    "text": "Margins hit a record.",
                    "support": [{"source": "S1", "quote": "record gross margin"}],
                },
            ],
        }
    )
    out = await answer(llm, "How did sales do?", HITS, company="Hershey")

    assert out.answered
    assert [s.text for s in out.sentences] == [
        "Hershey's net sales rose 2.3% in 2020.",
        "Michele Buck expected Halloween to be down mid-single digits.",
    ]
    first, second = out.sentences[0].citations[0], out.sentences[1].citations[0]
    assert (first.label, first.match, first.source_text) == (
        "S1", "exact", "Net sales increased 2.3% in 2020"
    )  # fmt: skip
    assert (second.label, second.passage_id) == ("S2", 11)
    assert out.dropped_sentences == ["Margins hit a record."]
    assert (out.quotes, out.dropped_quotes) == (3, 1)
    assert not out.unsupported


async def test_unanswerable_is_a_refusal() -> None:
    out = await answer(
        reply({"answerable": False, "sentences": []}), "Q4 2021 EPS?", HITS, company="Hershey"
    )
    assert not out.answered and not out.unsupported
    assert out.text == REFUSAL


async def test_an_answer_with_no_verifiable_quote_becomes_a_refusal() -> None:
    llm = reply(
        {
            "answerable": True,
            "sentences": [
                {"text": "Sales doubled.", "support": [{"source": "S1", "quote": "doubled"}]}
            ],
        }
    )
    out = await answer(llm, "How did sales do?", HITS, company="Hershey")
    assert not out.answered and out.unsupported
    assert out.text == REFUSAL


async def test_no_passages_means_no_llm_call() -> None:
    provider = FakeProvider()
    out = await answer(LLMClient(provider, default_model="m"), "q", [], company="Hershey")
    assert not out.answered and provider.requests == []


async def test_prompt_numbers_the_sources_and_api_shape() -> None:
    provider = FakeProvider(
        result(
            json.dumps(
                {
                    "answerable": True,
                    "sentences": [
                        {"text": "Up 2.3%.", "support": [{"source": "[S1]", "quote": "2.3%"}]}
                    ],
                }
            )
        )
    )
    out = await answer(LLMClient(provider, default_model="m"), "Sales?", HITS, company="Hershey")
    prompt = provider.requests[0].messages[1]["content"]
    assert prompt.startswith(f"Sources:\n\n{render_sources(HITS)}")
    assert "[S2] Earnings call: Q3 2020 earnings call, 5:02–6:10\nMichele Buck:" in prompt
    data = to_json(out)
    [citation] = data["sentences"][0]["citations"]
    assert (citation["label"], citation["quote"], citation["source"]) == ("S1", "2.3%", "filing")
    assert [s["label"] for s in data["sources"]] == ["S1", "S2"]


async def test_a_truncated_answer_gets_one_retry_with_more_room() -> None:
    provider = FakeProvider(
        result("", finish_reason="length"),
        result('{"answerable": false, "sentences": []}'),
    )
    out = await answer(LLMClient(provider, default_model="m"), "q", HITS, company="Hershey")
    assert not out.answered
    assert [r.max_tokens for r in provider.requests] == [2000, 4000]
