from app.filings.tenk import parse_10k

FLOWING = """<html><body>
<div style="display: none"><ix:header><ix:hidden>hidden xbrl fact</ix:hidden></ix:header></div>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
<table>
  <tr><td>Item 1A.</td><td>Risk Factors</td><td>5</td></tr>
  <tr><td>Item 7.</td><td>Management's Discussion and Analysis</td><td>9</td></tr>
</table>
<p style="font-weight:bold">Item 1A. Risk Factors</p>
<p><span style="font-weight:700">Demand for our products may fall.</span></p>
<p>Our revenue depends on a few customers, and losing any of them</p>
<p>5</p>
<hr style="page-break-after:always"/>
<p><a href="#toc">Table of Contents</a></p>
<p>Acme Corp | 2020 Form 10-K | Page 6</p>
<p>would hurt our results.</p>
<table><tr><td>&#8226;</td><td>supply disruptions;</td></tr></table>
<p>We rely on one foundry.</p>
<p>We rely on one foundry.</p>
<p><b>PART II</b></p>
<div><b>Item 7.</b></div>
<div><b>Management's Discussion and Analysis</b></div>
<div>Revenue grew&#160;38% in 2020.</div>
<table>
  <tr><td>Year</td><td>2020</td><td>2019</td></tr>
  <tr><td>Revenue</td><td>$</td><td>116,156</td><td>$</td><td>84,074</td></tr>
  <tr><td>Gross margin</td><td>45.4</td><td>%</td><td>45.9</td><td>%</td></tr>
</table>
<div>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</div>
<div>None.</div>
</body></html>"""


def _line(top: float, *pieces: tuple[float, str] | tuple[float, str, bool]) -> str:
    out = []
    for left, text, *bold in pieces:
        weight = "font-weight:bold;" if bold and bold[0] else ""
        out.append(
            f'<div style="position:absolute;{weight}left:{left}px;top:{top}px;">{text}</div>'
        )
    return "".join(out)


def _page(*lines: str) -> str:
    return f'<div style="position:relative;width:816px;height:1056px;">{"".join(lines)}</div>'


# A PDF-converted filing: every printed line is an absolutely positioned box.
LAYOUT = f"""<html><body>
{_page(
    _line(96, (96, "Item 1A.", True), (140, "&#160;"), (156, "RISK FACTORS", True)),
    _line(128, (96, "Our results depend on oil prices, which have been ")),
    _line(144, (96, "volatile and may fall further.")),
    _line(176, (96, "Lower prices could reduce our ")),
    _line(992, (401, "23")),
)}
{_page(
    _line(96, (96, "earnings and cash flows.")),
    _line(128, (120, "&#9679;"), (144, "Reduced demand; ")),
    _line(144, (144, "continued into a second line.")),
    _line(176, (96, "Item 7.", True), (140, "&#160;"), (156, "MANAGEMENT&#8217;S ", True),
          (250, "DISCUSSION AND ", True)),
    _line(192, (156, "ANALYSIS", True)),
    _line(224, (96, "Results by segment are below.")),
    _line(256, (551, "Millions of Dollars")),
    _line(272, (96, "Years Ended December 31"), (539, "2020"), (616, "2019")),
    _line(305, (96, "Alaska"), (490, "$"), (536, "(719)"), (608, "1,520")),
    _line(321, (96, "Canada"), (536, "(326)"), (619, "279")),
    _line(353, (96, "See the discussion beginning on page "), (358, "75.")),
    _line(385, (96, "Exhibits and SCHEDULE"), (220, "S are listed separately.")),
)}
</body></html>"""  # fmt: skip


def texts(doc, section):
    return [(p.kind, p.text) for p in doc.section(section).paragraphs]


def test_flowing_html_sections_skip_the_table_of_contents() -> None:
    doc = parse_10k(FLOWING.encode())
    assert [(s.id, s.title) for s in doc.sections] == [
        ("1A", "Risk Factors"),
        ("7", "Management's Discussion and Analysis"),
        ("7A", "Quantitative and Qualitative Disclosures About Market Risk"),
    ]


def test_flowing_html_paragraphs() -> None:
    doc = parse_10k(FLOWING.encode())
    assert texts(doc, "1A") == [
        ("heading", "Demand for our products may fall."),
        # Page number, "Table of Contents" link and running header removed; the
        # paragraph the page break split is joined back together.
        ("text", "Our revenue depends on a few customers, and losing any of them "
                 "would hurt our results."),
        ("text", "• supply disruptions;"),
        ("text", "We rely on one foundry."),
        ("text", "We rely on one foundry."),
    ]  # fmt: skip
    assert texts(doc, "7") == [
        ("text", "Revenue grew 38% in 2020."),
        ("table", "Year | 2020 | 2019\nRevenue | $116,156 | $84,074\nGross margin | 45.4% | 45.9%"),
    ]
    assert texts(doc, "7A") == [("text", "None.")]
    all_text = " ".join(p.text for s in doc.sections for p in s.paragraphs)
    assert "hidden xbrl fact" not in all_text


def test_layout_html_rebuilds_lines_paragraphs_and_tables() -> None:
    doc = parse_10k(LAYOUT.encode())
    assert [(s.id, s.title) for s in doc.sections] == [
        ("1A", "RISK FACTORS"),
        ("7", "MANAGEMENT’S DISCUSSION AND ANALYSIS"),
    ]
    assert texts(doc, "1A") == [
        ("text", "Our results depend on oil prices, which have been volatile and may "
                 "fall further."),
        # Continues across the page break, past the page number.
        ("text", "Lower prices could reduce our earnings and cash flows."),
        ("text", "● Reduced demand; continued into a second line."),
    ]  # fmt: skip
    assert texts(doc, "7") == [
        ("text", "Results by segment are below."),
        (
            "table",
            "Millions of Dollars\nYears Ended December 31 | 2020 | 2019\n"
            "Alaska | $(719) | 1,520\nCanada | (326) | 279",
        ),
        ("text", "See the discussion beginning on page 75."),
        ("text", "Exhibits and SCHEDULES are listed separately."),
    ]


def test_paragraph_ids_are_stable_and_unique() -> None:
    first = parse_10k(FLOWING.encode())
    again = parse_10k(FLOWING.encode())
    ids = [p.id for s in first.sections for p in s.paragraphs]
    assert ids == [p.id for s in again.sections for p in s.paragraphs]
    assert len(ids) == len(set(ids))
    repeated = [p.id for p in first.section("1A").paragraphs[-2:]]
    assert repeated[1] == repeated[0] + "-2"

    edited = parse_10k(FLOWING.replace("grew&#160;38%", "grew 39%").encode())
    before = {p.index: p.id for p in first.section("7").paragraphs}
    after = {p.index: p.id for p in edited.section("7").paragraphs}
    assert before[1] != after[1]  # the edited paragraph
    assert before[2] == after[2]  # everything else keeps its ID
    assert [p.id for p in first.section("1A").paragraphs] == [
        p.id for p in edited.section("1A").paragraphs
    ]
    assert all(p.id.startswith("1A-") for p in first.section("1A").paragraphs)
