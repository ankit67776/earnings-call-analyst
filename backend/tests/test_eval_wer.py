import pytest

from app.calls.earnings21 import RefToken
from app.calls.transcript import LabeledSegment
from evals.wer import reference_words, score, speaker_accuracy


def ref(text: str, speaker: int = 0, entities: tuple[str, ...] = ()) -> list[RefToken]:
    return [RefToken(w, speaker, "", entities) for w in text.split()]


TOKENS = (
    ref("Revenue was")
    + ref("$32.7 million", entities=("MONEY",))
    + ref("in the")
    + ref("third quarter", entities=("DATE",))
    + ref("led by")
    + ref("Apple", entities=("ORG",))
)


def test_entities_keep_their_group_through_normalization() -> None:
    words, groups = reference_words(TOKENS)
    assert words == ["revenue", "was", "$32700000", "in", "the", "3rd", "quarter", "led", "by",
                     "apple"]  # fmt: skip
    assert groups == [None, None, "numbers", None, None, "numbers", "numbers", None, None, "names"]


def test_written_and_spoken_forms_score_the_same() -> None:
    result = score(TOKENS, "Revenue was 32.7 million dollars in the 3rd Quarter, led by Apple.")
    assert result.wer == 0.0
    assert result.entity_error("numbers") == 0.0


def test_errors_inside_entities_are_counted_by_group() -> None:
    result = score(TOKENS, "Revenue was $22.7 million in the third quarter led by apples")
    assert (result.substitutions, result.deletions, result.insertions) == (2, 0, 0)
    assert result.wer == pytest.approx(2 / 10)
    assert result.entity_error("numbers") == pytest.approx(1 / 3)
    assert result.entity_error("names") == 1.0


def test_speaker_accuracy_counts_correct_words_with_the_right_speaker() -> None:
    tokens = ref("how is demand", speaker=1) + ref("demand is strong", speaker=2)
    names = {1: "Analyst", 2: "CFO"}
    segments = [
        LabeledSegment(1, 1, "Analyst", 0, 2, "How is demand? Demand"),
        LabeledSegment(2, 2, "CFO", 2, 4, "is strong."),
    ]
    assert speaker_accuracy(tokens, names, segments) == pytest.approx(5 / 6)
