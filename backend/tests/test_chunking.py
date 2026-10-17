from app.analysis.chunking import chunk_section
from app.filings.tenk import Paragraph, Section


def make_section(*items: tuple[str, int]) -> Section:
    """Paragraphs given as (kind, length); text is "<n>:" padded to the length."""
    paragraphs = []
    for n, (kind, length) in enumerate(items, start=1):
        text = f"{n}:".ljust(length, "x")
        paragraphs.append(Paragraph(f"1A-{n}", "1A", n, kind, text))
    return Section("1A", "Risk Factors", paragraphs)


def ids(chunk) -> list[int]:
    return [p.index for p in chunk.paragraphs]


def test_packs_whole_paragraphs_up_to_the_limit() -> None:
    section = make_section(("text", 400), ("text", 400), ("text", 400), ("text", 1500))
    chunks = chunk_section(section, max_chars=1000)
    assert [ids(c) for c in chunks] == [[1, 2], [3], [4]]  # an oversized paragraph stands alone
    assert all(c.section == "1A" and c.section_title == "Risk Factors" for c in chunks)


def test_starts_chunks_at_headings_and_carries_heading_context() -> None:
    section = make_section(
        ("heading", 50),
        ("text", 400),
        ("text", 400),
        ("heading", 50),  # past half the limit: starts a new chunk
        ("text", 700),
        ("text", 700),  # doesn't fit: new chunk, under the previous heading
    )
    chunks = chunk_section(section, max_chars=1000)
    assert [ids(c) for c in chunks] == [[1, 2, 3], [4, 5], [6]]
    assert [c.context_heading for c in chunks] == [None, None, section.paragraphs[3].text]


def test_never_ends_a_chunk_on_a_heading() -> None:
    section = make_section(("text", 300), ("heading", 100), ("text", 700))
    chunks = chunk_section(section, max_chars=1000)
    assert [ids(c) for c in chunks] == [[1], [2, 3]]


def test_tables_are_skipped_unless_asked_for() -> None:
    section = make_section(("text", 100), ("table", 100), ("text", 100))
    assert [ids(c) for c in chunk_section(section)] == [[1, 3]]
    assert [ids(c) for c in chunk_section(section, include_tables=True)] == [[1, 2, 3]]
