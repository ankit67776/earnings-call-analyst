"""Checks against the real Phase 0 filings. Skipped when data/raw isn't downloaded (e.g. CI);
fetch them with: uv run python -m app.filings fetch SITM HSY COP --year 2020"""

import re
from pathlib import Path

import pytest

from app.config import settings
from app.filings.tenk import FOCUS_SECTIONS, parse_10k

FILINGS = {
    # ticker: (first words of Item 1A's first text paragraph, a sentence from MD&A)
    "SITM": ("The effects of the ongoing COVID-19 pandemic", "We operate a fabless business model"),
    "HSY": ("You should carefully read the following", "2020 compared with 2019"),
    "COP": ("You should carefully consider the following", "Brent crude oil prices averaged $42"),
}


def load(ticker: str):
    path = Path(settings.raw_data_dir) / "10k" / f"{ticker}_10-K_FY2020.htm"
    if not path.exists():
        pytest.skip(f"{path} not downloaded")
    return parse_10k(path.read_bytes())


@pytest.mark.parametrize("ticker", FILINGS)
def test_focus_sections_parse_cleanly(ticker: str) -> None:
    doc = load(ticker)
    risk_start, mdna_text = FILINGS[ticker]

    assert [s.id for s in doc.sections][:2] == ["1", "1A"]
    for item in FOCUS_SECTIONS:
        section = doc.section(item)
        assert section is not None and len(section.paragraphs) >= 40, item
        for p in section.paragraphs:
            assert not re.fullmatch(r"\d{1,3}", p.text), p
            assert "Table of Contents" not in p.text, p
            assert "Form 10-K |" not in p.text, p

    first_text = next(p for p in doc.section("1A").paragraphs if p.kind == "text")
    assert first_text.text.startswith(risk_start)
    assert any(mdna_text in p.text for p in doc.section("7").paragraphs)
    assert any(p.kind == "table" for p in doc.section("7").paragraphs)

    ids = [p.id for s in doc.sections for p in s.paragraphs]
    assert len(ids) == len(set(ids))
