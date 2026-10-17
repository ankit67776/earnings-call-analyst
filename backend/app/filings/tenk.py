"""Parse a 10-K's primary HTML document into Items (sections) and paragraphs.

EDGAR filings come in two shapes: flowing HTML (paragraphs are block elements, tables
are <table>) and PDF-converted HTML where every printed line is an absolutely
positioned box. Both are reduced to a flat list of blocks, then split at the
"Item N." headings.
"""

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from lxml import html

# Risk Factors and MD&A: where most of the claims worth checking live.
FOCUS_SECTIONS = ("1A", "7")

ITEMS = (
    "1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8",
    "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16",
)  # fmt: skip

Kind = Literal["text", "heading", "table"]


@dataclass(frozen=True)
class Paragraph:
    # Stable across re-parses: section + hash of the text (+ "-n" for repeats).
    id: str
    section: str
    # 1-based position within the section, for display.
    index: int
    kind: Kind
    text: str


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    paragraphs: list[Paragraph]


@dataclass(frozen=True)
class TenK:
    sections: list[Section]

    def section(self, item: str) -> Section | None:
        return next((s for s in self.sections if s.id == item.upper()), None)


def parse_10k(raw: bytes) -> TenK:
    doc = html.document_fromstring(raw, parser=html.HTMLParser(encoding="utf-8"))
    _drop_hidden(doc)
    positioned = doc.xpath(_TOP_POSITIONED)
    positioned_chars = sum(len(el.text_content()) for el in positioned)
    if positioned_chars > 0.5 * len(doc.body.text_content()):
        blocks = _layout_blocks(positioned)
    else:
        blocks = _flow_blocks(doc.body)
    blocks = _merge_split_paragraphs([b for b in blocks if not _is_furniture(b)])
    return TenK(_split_sections(blocks))


@dataclass
class _Block:
    text: str
    kind: Literal["text", "table"] = "text"
    bold: bool = False


# --- shared text helpers ---

_SPACE = re.compile(r"\s+")
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_BOLD_STYLE = re.compile(r"font-weight\s*:\s*(bold|[6-9]00)", re.I)


def _clean(text: str) -> str:
    return _SPACE.sub(" ", _INVISIBLE.sub("", text)).strip()


def _is_bold(el: html.HtmlElement) -> bool:
    return el.tag in ("b", "strong") or bool(_BOLD_STYLE.search(el.get("style") or ""))


def _drop_hidden(doc: html.HtmlElement) -> None:
    # Inline XBRL keeps machine-readable facts in a hidden <ix:header>.
    hidden = doc.xpath(
        "//script | //style | //head | //*[contains(translate(@style, ' ', ''), 'display:none')]"
        " | //*[substring(name(), string-length(name()) - 6) = ':header']"
    )
    for el in hidden:
        if el.getparent() is not None:
            el.drop_tree()


# --- flowing HTML ---

_BLOCK_TAGS = {
    "address", "article", "blockquote", "caption", "center", "dd", "div", "dl", "dt",
    "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
    "main", "ol", "p", "pre", "section", "ul",
}  # fmt: skip


class _FlowWalker:
    def __init__(self) -> None:
        self.blocks: list[_Block] = []
        self._parts: list[str] = []
        self._chars = 0
        self._bold_chars = 0

    def walk(self, el: html.HtmlElement, bold: bool = False) -> None:
        bold = bold or _is_bold(el)
        if el.tag == "table":
            self._flush()
            self._table(el)
            return
        is_block = el.tag in _BLOCK_TAGS
        if is_block:
            self._flush()
        if el.tag == "br":
            self._add(" ", bold)
        self._add(el.text, bold)
        for child in el:
            if isinstance(child.tag, str):
                self.walk(child, bold)
            self._add(child.tail, bold)
        if is_block:
            self._flush()

    def finish(self) -> list[_Block]:
        self._flush()
        return self.blocks

    def _add(self, text: str | None, bold: bool) -> None:
        if not text:
            return
        self._parts.append(text)
        n = len(text) - text.count(" ")
        self._chars += n
        if bold:
            self._bold_chars += n

    def _flush(self) -> None:
        text = _clean("".join(self._parts))
        if text:
            self.blocks.append(_Block(text, bold=self._bold_chars >= 0.9 * self._chars))
        self._parts, self._chars, self._bold_chars = [], 0, 0

    def _table(self, table: html.HtmlElement) -> None:
        rows: list[list[html.HtmlElement]] = []
        for tr in table.iter("tr"):
            cells = [td for td in tr if td.tag in ("td", "th") and _clean(td.text_content())]
            if cells:
                rows.append(cells)
        if not rows:
            return
        texts = [[_clean(td.text_content()) for td in row] for row in rows]
        first = texts[0]
        marker = len(first) == 2 and len(first[0]) <= 4 and not _NUMERIC.match(first[1])
        if len(rows) == 1 and len(first) <= 2 and (marker or len(first[-1]) > 40):
            # A one-row layout table, usually a bullet or footnote marker next to its text.
            walker = _FlowWalker()
            walker.walk(rows[0][-1])
            bold = all(b.bold for b in walker.finish())
            self.blocks.append(_Block(" ".join(first), bold=bold))
            return
        self.blocks.append(_table_block(texts))


def _flow_blocks(body: html.HtmlElement) -> list[_Block]:
    walker = _FlowWalker()
    walker.walk(body)
    return walker.finish()


# --- PDF-converted HTML (absolutely positioned lines) ---

_TOP_POSITIONED = (
    "//*[contains(translate(@style, ' ', ''), 'position:absolute')]"
    "[not(ancestor::*[contains(translate(@style, ' ', ''), 'position:absolute')])]"
)
_POS = re.compile(r"\b(top|left)\s*:\s*(-?[\d.]+)px")
# Table cells: amounts, percentages, dashes. A trailing "." means prose ("see page 75.").
_NUMERIC = re.compile(r"^[\s$€£%()\-–—.,*\d]*[\s$€£%()\-–—,*\d]$|^(n/?a|nm)$", re.I)
_BULLETS = "•●◦▪■–-·"


@dataclass
class _Piece:
    top: float
    left: float
    text: str
    bold: bool
    # Whether the source had whitespace before/after this piece, i.e. a word boundary.
    space_before: bool = False
    space_after: bool = False


@dataclass
class _Line:
    top: float
    pieces: list[_Piece] = field(default_factory=list)
    is_row: bool = False

    @property
    def text(self) -> str:
        out = self.pieces[0].text
        for prev, p in zip(self.pieces, self.pieces[1:], strict=False):
            spaced = prev.space_after or p.space_before or prev.text in _BULLETS
            out += (" " if spaced else "") + p.text
        return out

    @property
    def bold(self) -> bool:
        words = [p for p in self.pieces if not _NUMERIC.match(p.text)]
        return bool(words) and all(p.bold for p in words)

    @property
    def bullet(self) -> bool:
        return len(self.pieces) > 1 and self.pieces[0].text in _BULLETS


def _layout_blocks(positioned: list[html.HtmlElement]) -> list[_Block]:
    pages: dict[html.HtmlElement, list[_Piece]] = defaultdict(list)
    for el in positioned:
        pos = {k: float(v) for k, v in _POS.findall(el.get("style") or "")}
        raw = _INVISIBLE.sub("", el.text_content())
        if not raw or "top" not in pos:
            continue
        styled = [d for d in el.iterdescendants() if d.get("style") or d.tag in ("b", "strong")]
        bold = _is_bold(el) or (bool(styled) and all(_is_bold(d) for d in styled))
        # Whitespace-only pieces are kept until lines are built: they mark word gaps.
        piece = _Piece(pos["top"], pos.get("left", 0.0), _clean(raw), bold)
        piece.space_before, piece.space_after = raw[:1].isspace(), raw[-1:].isspace()
        pages[el.getparent()].append(piece)

    blocks: list[_Block] = []
    for pieces in pages.values():
        lines = _group_lines(pieces)
        while lines and _is_page_number(lines[-1].text):
            lines.pop()
        while lines and _is_page_number(lines[0].text):
            lines.pop(0)
        blocks.extend(_page_blocks(lines))
    return blocks


def _group_lines(pieces: list[_Piece]) -> list[_Line]:
    lines: list[_Line] = []
    for piece in sorted(pieces, key=lambda p: (p.top, p.left)):
        if lines and piece.top - lines[-1].top <= 3.0:
            lines[-1].pieces.append(piece)
        else:
            lines.append(_Line(piece.top, [piece]))
    for line in lines:
        pieces, gap = [], False
        for p in sorted(line.pieces, key=lambda p: p.left):
            if not p.text:
                gap = True
                continue
            p.space_before = p.space_before or gap
            pieces.append(p)
            gap = False
        line.pieces = pieces
        rest = pieces[1:]
        line.is_row = bool(rest) and all(_NUMERIC.match(p.text) for p in rest)
    return [line for line in lines if line.pieces]


def _page_blocks(lines: list[_Line]) -> list[_Block]:
    if not lines:
        return []
    gaps = [b.top - a.top for a, b in zip(lines, lines[1:], strict=False) if b.top > a.top]
    # Line spacing inside a paragraph; most gaps on a page are that, the rest are larger.
    spacing = sorted(gaps)[len(gaps) // 4] if gaps else 16.0
    steps = [0.0] + [b.top - a.top for a, b in zip(lines, lines[1:], strict=False)]
    near = [step <= 1.45 * spacing for step in steps]
    # Tables leave more space under their column headers than prose leaves between lines.
    near_row = [step <= 2.5 * spacing for step in steps]
    near[0] = near_row[0] = False
    margin = min(ln.pieces[0].left for ln in lines)

    # A lone "label | number" line with no row next to it is prose ending in a number.
    for i, line in enumerate(lines):
        if line.is_row and len(line.pieces) == 2:
            above = i > 0 and near[i] and lines[i - 1].is_row
            below = i + 1 < len(lines) and near[i + 1] and lines[i + 1].is_row
            line.is_row = above or below
    for i in range(len(lines) - 1, -1, -1):
        line, below = lines[i], lines[i + 1] if i + 1 < len(lines) else None
        if line.is_row or below is None or not below.is_row:
            continue
        between = i > 0 and lines[i - 1].is_row and near[i] and near[i + 1]
        # A caption over the number columns, e.g. "Millions of Dollars".
        caption = len(line.pieces) == 1 and line.pieces[0].left > margin + 200 and near_row[i + 1]
        line.is_row = between or caption

    blocks: list[_Block] = []
    current: list[_Line] = []

    def flush() -> None:
        if not current:
            return
        if current[0].is_row:
            blocks.append(_table_block([[p.text for p in ln.pieces] for ln in current]))
        else:
            text = current[0].text
            for ln in current[1:]:
                hyphenated = text.endswith("-") and text[-2:-1].isalpha()
                text += ("" if hyphenated else " ") + ln.text
            blocks.append(_Block(_clean(text), bold=all(ln.bold for ln in current)))
        current.clear()

    for i, line in enumerate(lines):
        if current:
            prev = current[-1]
            if line.is_row and prev.is_row:
                same = near_row[i]
            else:
                same = (
                    near[i]
                    and line.is_row == prev.is_row
                    and line.bold == prev.bold
                    and not line.bullet
                )
            if not same:
                flush()
        current.append(line)
    flush()
    return blocks


# --- cleanup and sectioning ---

_PAGE_NUMBER = re.compile(r"^(page\s+)?(\d{1,3}|[ivxlc]{1,7}|[a-z]-\d{1,3})$", re.I)
_FURNITURE = re.compile(
    r"^(table of contents|index|part\s+[iv]+\.?)$"
    # Running page headers, e.g. "The Hershey Company | 2020 Form 10-K | Page 8".
    r"|^(table of contents\s*)?.{0,100}\bform 10-k\b.{0,20}?(page\s*)?\d{1,3}$",
    re.I,
)
_TERMINAL = (".", "!", "?", ":", ";", '"', "”", ")")


def _is_page_number(text: str) -> bool:
    return bool(_PAGE_NUMBER.match(text))


def _is_furniture(block: _Block) -> bool:
    return block.kind == "text" and bool(
        _PAGE_NUMBER.match(block.text) or _FURNITURE.match(block.text)
    )


def _merge_split_paragraphs(blocks: list[_Block]) -> list[_Block]:
    """Rejoin paragraphs that a page break cut mid-sentence."""
    out: list[_Block] = []
    for block in blocks:
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.kind == block.kind == "text"
            and not prev.bold
            and not block.bold
            and not prev.text.endswith(_TERMINAL)
            and block.text[0].islower()
        ):
            prev.text += " " + block.text
        else:
            out.append(block)
    return out


_ITEM_HEADING = re.compile(
    r"^items?\s+(\d{1,2}[a-c]?)\b(?!\s*\()(?:\s*(?:and|&|,)\s*\d{1,2}[a-c]?\b)?"
    r"\s*[.:\-–—]?\s*(.*)$",
    re.I,
)


def _heading_item(block: _Block) -> tuple[str, str] | None:
    if block.kind != "text" or len(block.text) > 250:
        return None
    m = _ITEM_HEADING.match(block.text)
    if m is None or m.group(1).upper() not in ITEMS:
        return None
    if not block.bold and len(block.text) > 150:
        return None
    return m.group(1).upper(), m.group(2).strip().rstrip(".").strip()


def _split_sections(blocks: list[_Block]) -> list[Section]:
    # The table of contents repeats every heading; the body's copy comes last.
    last: dict[str, int] = {}
    for i, block in enumerate(blocks):
        if (found := _heading_item(block)) is not None:
            last[found[0]] = i
    starts = _increasing_by_item(sorted((i, item) for item, i in last.items()))

    sections = []
    for n, (start, item) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(blocks)
        _, title = _heading_item(blocks[start])
        body = blocks[start + 1 : end]
        if not title and body and body[0].bold and len(body[0].text) < 200:
            title, body = body[0].text, body[1:]
        sections.append(Section(item, title, _paragraphs(item, body)))
    return sections


def _increasing_by_item(starts: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Longest run of headings (in document order) whose Item numbers only increase."""
    rank = [ITEMS.index(item) for _, item in starts]
    best = [1] * len(starts)
    prev = [-1] * len(starts)
    for i in range(len(starts)):
        for j in range(i):
            if rank[j] < rank[i] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    if not starts:
        return []
    i = max(range(len(starts)), key=lambda k: best[k])
    chain = []
    while i != -1:
        chain.append(starts[i])
        i = prev[i]
    return chain[::-1]


def _paragraphs(section: str, blocks: list[_Block]) -> list[Paragraph]:
    seen: dict[str, int] = defaultdict(int)
    out = []
    for index, block in enumerate(blocks, start=1):
        digest = hashlib.sha1(block.text.encode("utf-8")).hexdigest()[:8]
        pid = f"{section}-{digest}"
        seen[pid] += 1
        if seen[pid] > 1:
            pid = f"{pid}-{seen[pid]}"
        kind: Kind = block.kind
        if kind == "text" and block.bold and len(block.text) < 500:
            kind = "heading"
        out.append(Paragraph(pid, section, index, kind, block.text))
    return out


_CURRENCY = {"$", "€", "£"}
_SUFFIXES = {"%", ")", "%)", ")%"}


def _table_block(rows: list[list[str]]) -> _Block:
    """Rows as "cell | cell" lines, with "$" and "%" cells folded into their numbers."""
    lines = []
    for cells in rows:
        # Header rows ("2020 | 2019 | %") start with a number; their "%" is a column label.
        labelled = not _NUMERIC.match(cells[0])
        merged: list[str] = []
        prefix = ""
        for cell in cells:
            if cell in _CURRENCY and labelled:
                prefix += cell
            elif cell in _SUFFIXES and merged and labelled:
                merged[-1] += cell
            else:
                merged.append(prefix + cell)
                prefix = ""
        lines.append(" | ".join(merged))
    return _Block("\n".join(lines), kind="table")
