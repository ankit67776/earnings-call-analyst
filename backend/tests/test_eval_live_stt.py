import pytest

from app.calls.earnings21 import RefToken
from app.live.stt import LiveWord
from evals.live_stt import diarization_accuracy, match_speakers, segments
from evals.wer import aligned_speakers


def ref(text: str, speaker: int) -> list[RefToken]:
    return [RefToken(w, speaker, "", ()) for w in text.split()]


def words(text: str, speaker: int) -> list[LiveWord]:
    return [LiveWord(w, 0.0, 0.0, speaker) for w in text.split()]


def test_segments_follow_diarization_labels() -> None:
    out = segments(words("Good afternoon.", 1) + words("Thanks.", 0) + words("Yes", 0))
    assert [(s.speaker, s.text) for s in out] == [("S1", "Good afternoon."), ("S0", "Thanks. Yes")]


def test_labels_match_the_speaker_they_share_most_words_with_one_to_one() -> None:
    # S0 carries the operator and the CEO (merged); S1 is the CEO's second half (split).
    pairs = [("Operator", "S0")] * 3 + [("CEO", "S0")] * 2 + [("CEO", "S1")] * 4
    assert match_speakers(pairs) == {"S1": "CEO", "S0": "Operator"}
    # The CEO's words under S0 are wrong: 7 of 9 right.
    assert diarization_accuracy(pairs) == pytest.approx(7 / 9)


def test_labels_are_scored_on_correctly_transcribed_words() -> None:
    tokens = ref("welcome to the call", 0) + ref("thank you", 1)
    names = {0: "Operator", 1: "CEO"}
    hyp = words("Welcome to the call.", 3) + words("Thank you.", 7)
    pairs = aligned_speakers(tokens, names, segments(hyp))
    assert diarization_accuracy(pairs) == 1.0
    assert diarization_accuracy([]) == 0.0
