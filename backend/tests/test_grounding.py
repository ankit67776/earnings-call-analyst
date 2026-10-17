from app.analysis.grounding import locate, normalize

NBSP = "\N{NO-BREAK SPACE}"
LQ, RQ = "\N{LEFT DOUBLE QUOTATION MARK}", "\N{RIGHT DOUBLE QUOTATION MARK}"
DASH = "\N{EM DASH}"

PARAGRAPH = (
    "Revenue increased 38% to $116.2 million in 2020. Our largest end customer, "
    "Apple, accounted for approximately 24% of revenue. We expect gross margin "
    "to remain near 50% in 2021."
)


def matched(quote: str, text: str = PARAGRAPH, **kw) -> tuple[str, str] | None:
    m = locate(quote, text, **kw)
    return None if m is None else (m.kind, text[m.start : m.end])


def test_exact_match() -> None:
    assert matched("Apple, accounted for approximately 24% of revenue.") == (
        "exact",
        "Apple, accounted for approximately 24% of revenue.",
    )


def test_normalized_match_returns_the_source_text() -> None:
    text = f"Our CEO said the outlook was {LQ}strong{RQ} {DASH} and{NBSP}improving."
    assert matched('the outlook was "STRONG" - and   improving', text) == (
        "normalized",
        f"the outlook was {LQ}strong{RQ} {DASH} and{NBSP}improving",
    )


def test_fuzzy_match_tolerates_small_copying_slips() -> None:
    assert matched("We expect gross margins to remain near 50 % in 2021") == (
        "fuzzy",
        "We expect gross margin to remain near 50% in 2021",
    )
    assert matched("We expect gross margins to remain near 50 % in 2021", fuzzy=False) is None


def test_elided_quote_matches_when_every_fragment_is_verbatim_and_in_order() -> None:
    quote = "Revenue increased 38% to $116.2 million ... We expect gross margin to remain near 50%"
    assert matched(quote) == (
        "elided",
        PARAGRAPH[: PARAGRAPH.index(" in 2021.")],
    )
    # An ellipsis only at the start or end is just a partial sentence.
    assert matched("... accounted for approximately 24% of revenue ...") == (
        "normalized",
        "accounted for approximately 24% of revenue",
    )


def test_elided_quote_is_rejected_out_of_order_or_with_short_fragments() -> None:
    assert matched("We expect gross margin to remain near 50% ... Revenue increased 38%") is None
    assert matched("Revenue increased 38% ... in 2021.") is None  # "in 2021." is too short
    assert matched("Revenue increased 38% to $116.2 million ... gross margin will fall") is None


def test_rejects_quotes_that_are_not_in_the_source() -> None:
    assert matched("Revenue decreased 12% to $80.1 million in 2020.") is None
    assert matched("   ") is None


def test_normalize_maps_back_to_original_indices() -> None:
    original = f"  A{NBSP} {LQ}B{RQ}{DASH}C "
    text, index = normalize(original)
    assert text == 'a "b"-c'
    # Collapsed whitespace maps to the first character of the run.
    assert [original[i] for i in index] == ["A", NBSP, LQ, "B", RQ, DASH, "C"]
