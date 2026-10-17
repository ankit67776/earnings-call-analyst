"""Check that a quote the model returned really appears in the source paragraph.

Tries an exact match, then a match after normalizing case, whitespace, quote marks and
dashes, then a close fuzzy match (for small copying slips). A quote shortened with
"..." matches as "elided" only if every fragment appears verbatim, in order, in the same
paragraph; it's reported separately because an elision can change meaning. Every match
is mapped back to a span of the original paragraph, so citations show the filing's own
text rather than the model's copy of it.
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

MatchKind = Literal["exact", "normalized", "elided", "fuzzy"]

# Share of the quote's characters that must line up with the source for a fuzzy match.
FUZZY_MIN_MATCH = 0.9
# The matched source span may be at most this much longer than the quote.
FUZZY_MAX_STRETCH = 1.15
# Shorter fragments around an ellipsis could match almost anywhere.
ELIDED_MIN_FRAGMENT = 20

_EDGE_ELLIPSIS = re.compile(r"^(\.\.\.\s*)+|(\s*\.\.\.)+$")

_FOLD = str.maketrans(
    # Curly single quotes and prime; curly double quotes and double prime; hyphens,
    # en/em dashes and minus.
    dict.fromkeys(map(chr, (0x2018, 0x2019, 0x201A, 0x201B, 0x2032)), "'")
    | dict.fromkeys(map(chr, (0x201C, 0x201D, 0x201E, 0x2033)), '"')
    | dict.fromkeys(map(chr, (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212)), "-")
)


@dataclass(frozen=True)
class Match:
    kind: MatchKind
    # Span in the original paragraph text.
    start: int
    end: int


def locate(quote: str, text: str, *, fuzzy: bool = True) -> Match | None:
    quote = quote.strip()
    if not quote:
        return None
    i = text.find(quote)
    if i >= 0:
        return Match("exact", i, i + len(quote))

    norm_quote, _ = normalize(quote)  # NFKC turns a "…" character into "..."
    norm_quote = _EDGE_ELLIPSIS.sub("", norm_quote)
    norm_text, index = normalize(text)
    if not norm_quote:
        return None
    i = norm_text.find(norm_quote)
    if i >= 0:
        return Match("normalized", index[i], index[i + len(norm_quote) - 1] + 1)

    if "..." in norm_quote:
        return _elided(norm_quote, norm_text, index)
    if not fuzzy:
        return None
    span = _fuzzy_span(norm_quote, norm_text)
    if span is None:
        return None
    return Match("fuzzy", index[span[0]], index[span[1] - 1] + 1)


def normalize(text: str) -> tuple[str, list[int]]:
    """Lowercased text with unified punctuation and collapsed whitespace, plus the
    original index of every output character."""
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).translate(_FOLD).lower():
            if c.isspace():
                if not out or out[-1] == " ":
                    continue
                c = " "
            out.append(c)
            index.append(i)
    if out and out[-1] == " ":
        out.pop()
        index.pop()
    return "".join(out), index


def _elided(quote: str, text: str, index: list[int]) -> Match | None:
    fragments = [f.strip(" ,;") for f in quote.split("...")]
    fragments = [f for f in fragments if f]
    if len(fragments) < 2 or min(map(len, fragments)) < ELIDED_MIN_FRAGMENT:
        return None
    start, pos = None, 0
    for fragment in fragments:
        i = text.find(fragment, pos)
        if i < 0:
            return None
        start = i if start is None else start
        pos = i + len(fragment)
    return Match("elided", index[start], index[pos - 1] + 1)


def _fuzzy_span(quote: str, text: str) -> tuple[int, int] | None:
    matcher = SequenceMatcher(None, quote, text, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size]
    needed = FUZZY_MIN_MATCH * len(quote)
    # Stray few-character matches outside the real passage stretch the span; drop them.
    while len(blocks) > 1 and blocks[0].size < 4 and _matched(blocks[1:]) >= needed:
        blocks.pop(0)
    while len(blocks) > 1 and blocks[-1].size < 4 and _matched(blocks[:-1]) >= needed:
        blocks.pop()
    if not blocks or _matched(blocks) < needed:
        return None
    start, end = blocks[0].b, blocks[-1].b + blocks[-1].size
    if end - start > FUZZY_MAX_STRETCH * len(quote):
        return None
    return start, end


def _matched(blocks) -> int:
    return sum(b.size for b in blocks)
