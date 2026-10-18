from app.calls.transcript import LabeledSegment
from app.filings.tenk import Paragraph, Section
from app.retrieval.passages import call_passages, clock, filing_passages, render_call_text
from app.retrieval.search import fuse


def words(n: int, word: str = "word") -> str:
    return " ".join([word] * n) + "."


SECTION = Section(
    "1A",
    "Risk Factors",
    [
        Paragraph("1A-a", "1A", 1, "heading", "Supply risks"),
        Paragraph("1A-b", "1A", 2, "text", words(100, "cocoa")),
        Paragraph("1A-c", "1A", 3, "text", words(100, "sugar")),
        Paragraph("1A-d", "1A", 4, "text", words(100, "milk")),
        Paragraph("1A-e", "1A", 5, "heading", "Legal risks"),
        Paragraph("1A-f", "1A", 6, "text", words(30, "law")),
    ],
)


def test_filing_passages_start_at_headings_and_stay_under_the_word_limit() -> None:
    passages = filing_passages([SECTION], max_words=250)
    assert [p.paragraph_ids for p in passages] == [
        ("1A-a", "1A-b", "1A-c"),
        ("1A-d",),
        ("1A-e", "1A-f"),
    ]
    assert passages[0].title == "Item 1A. Risk Factors"
    # A passage that continues under an earlier heading names it in its title.
    assert passages[1].title == "Item 1A. Risk Factors › Supply risks"
    assert passages[2].text.startswith("Legal risks\n\nlaw law")
    assert [p.position for p in passages] == [1, 2, 3]


def test_sections_outside_the_index_are_skipped() -> None:
    other = Section("8", "Financial Statements", [Paragraph("8-a", "8", 1, "text", "x")])
    assert all(p.section == "1A" for p in filing_passages([other, SECTION]))


def test_a_long_paragraph_is_cut_at_sentence_ends() -> None:
    sentences = " ".join(f"Sentence number {i} has six words." for i in range(100))
    section = Section("7", "MD&A", [Paragraph("7-a", "7", 1, "text", sentences)])
    passages = filing_passages([section], max_words=50)
    assert len(passages) == 13  # 8 six-word sentences each
    assert all(len(p.text.split()) <= 50 for p in passages)
    assert all(p.paragraph_ids == ("7-a",) for p in passages)
    assert passages[1].text.startswith("Sentence number 8 ")


def seg(n: int, turn: int, speaker: str, text: str) -> LabeledSegment:
    return LabeledSegment(n, turn, speaker, n * 10.0, n * 10.0 + 9, text)


def test_call_passages_break_at_turns_once_long_enough() -> None:
    segments = [
        seg(1, 1, "Operator", "Next question comes from Jane."),
        seg(2, 2, "Jane", words(70, "margin")),
        seg(3, 3, "CFO", words(80, "pricing")),
        seg(4, 3, "CFO", words(80, "volume")),
    ]
    passages = call_passages(segments, "Q3 2020 earnings call", max_words=250, min_words=60)
    # The operator's short turn runs on into the question; the answer starts a new passage.
    assert [(p.first_segment, p.last_segment) for p in passages] == [(1, 2), (3, 4)]
    assert passages[0].speakers == ("Operator", "Jane")
    assert passages[0].text.startswith("Operator: Next question comes from Jane.\n\nJane: margin")
    assert passages[1].title == "Q3 2020 earnings call, 0:30–0:49"


def test_render_call_text_reports_where_each_segment_starts() -> None:
    segments = [seg(1, 1, "A", "Hello."), seg(2, 1, "A", "Again."), seg(3, 2, "B", "Hi.")]
    text, offsets = render_call_text(segments)
    assert text == "A: Hello. Again.\n\nB: Hi."
    assert [text[o:].split()[0] for o in offsets] == ["Hello.", "Again.", "Hi."]


def test_clock() -> None:
    assert (clock(42.9), clock(605), clock(3725)) == ("0:42", "10:05", "1:02:05")


def row(i: int) -> dict:
    return {
        "id": i, "ticker": "T", "document_id": 1, "call_id": None, "title": "t", "text": "x",
        "section": "7", "paragraph_ids": [], "first_segment": None, "last_segment": None,
        "start_s": None, "end_s": None, "speakers": None,
    }  # fmt: skip


def test_rank_fusion_favours_agreement_near_the_top() -> None:
    fts = [row(1), row(2), row(3)]
    vector = [row(3), row(4), row(2)]
    hits = fuse(fts, vector, fts_weight=1.0)
    # 2 and 3 are in both lists; 3 is first in one and third in the other, beating 2.
    assert [h.passage_id for h in hits] == [3, 2, 1, 4]
    assert (hits[0].fts_rank, hits[0].vector_rank) == (3, 1)
    assert hits[2].vector_rank is None
    # Weighted down, keyword rank alone no longer beats a vector rank.
    assert [h.passage_id for h in fuse(fts, vector)] == [3, 2, 4, 1]
