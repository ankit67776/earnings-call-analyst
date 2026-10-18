import json

import pytest

from evals.build_golden import GOLDEN
from evals.merge_labels import ADJUDICATION, LABELERS, merge
from evals.metrics import Claim, Span, match, overlap, score, total
from evals.review import check
from evals.run import gold_claims


def claim(kind: str, *spans: tuple[str, int, int]) -> Claim:
    return Claim(kind, tuple(Span(*s) for s in spans))


def test_overlap_is_shared_share_of_the_shorter_span() -> None:
    assert overlap(Span("p1", 0, 100), Span("p1", 50, 70)) == 1.0
    assert overlap(Span("p1", 0, 100), Span("p1", 90, 110)) == pytest.approx(0.5)
    assert overlap(Span("p1", 0, 10), Span("p1", 10, 20)) == 0.0
    assert overlap(Span("p1", 0, 10), Span("p2", 0, 10)) == 0.0


def test_matching_is_one_to_one_best_overlap_first() -> None:
    gold = [claim("risk", ("p1", 0, 100)), claim("risk", ("p1", 100, 200))]
    predicted = [
        claim("risk", ("p1", 60, 140)),  # half in each: gets the one left over
        claim("risk", ("p1", 0, 95)),
        claim("risk", ("p1", 5, 90)),  # the same claim extracted twice
    ]
    assert match(predicted, gold) == [(0, 1), (1, 0)]


def test_an_alternative_gold_quote_matches_too() -> None:
    gold = [claim("risk", ("p1", 0, 50), ("p2", 10, 60))]
    assert match([claim("risk", ("p2", 20, 60))], gold) == [(0, 0)]


def test_score_counts_precision_recall_and_types() -> None:
    gold = [claim("risk", ("p1", 0, 50)), claim("metric", ("p2", 0, 40))]
    predicted = [
        claim("guidance", ("p1", 0, 50)),  # right span, wrong type
        claim("metric", ("p2", 5, 35)),
        claim("risk", ("p3", 0, 30)),  # not in the gold set
    ]
    tally, pairs = score(predicted, gold)
    assert pairs == [(0, 0), (1, 1)]
    rates = tally.rates()
    assert (rates["precision"], rates["recall"], rates["type_accuracy"]) == (
        pytest.approx(2 / 3),
        1.0,
        0.5,
    )
    assert tally.by_type["metric"].rates()["recall"] == 1.0
    assert tally.by_type["risk"].rates()["recall"] == 0.0  # matched only as guidance


def test_tallies_add_up_across_passages() -> None:
    a, _ = score([claim("risk", ("p1", 0, 9))], [claim("risk", ("p1", 0, 9))])
    b, _ = score([], [claim("risk", ("p2", 0, 9))])
    a.quotes, a.found, a.misattributed = 4, 3, 1
    overall = total([a, b])
    assert (overall.predicted, overall.gold, overall.matched) == (1, 2, 1)
    rates = overall.rates()
    assert (rates["grounding_rate"], rates["citation_accuracy"]) == (0.75, pytest.approx(2 / 3))


def test_golden_set_is_well_formed() -> None:
    data = json.loads(GOLDEN.read_text("utf-8"))
    assert check(data) == []
    assert len(data["passages"]) == 18
    for passage in data["passages"]:
        gold_claims(passage)  # every quote locates in its paragraph


def test_golden_claims_rebuild_from_labelers_and_adjudication() -> None:
    golden = json.loads(GOLDEN.read_text("utf-8"))
    a = json.loads((LABELERS / "a.json").read_text("utf-8"))
    b = json.loads((LABELERS / "b.json").read_text("utf-8"))
    decisions = {d["id"]: d for d in json.loads(ADJUDICATION.read_text("utf-8"))["decisions"]}
    rebuilt = merge(golden["passages"], a, b, decisions)
    assert [p["claims"] for p in rebuilt] == [p["claims"] for p in golden["passages"]]
