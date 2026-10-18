"""Scoring extracted claims against the golden set.

Claims are compared by where their quotes sit in the filing, not by wording: a
predicted claim matches a gold claim when one of its quote spans overlaps one of the
gold claim's spans in the same paragraph by at least half the shorter span.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

MIN_OVERLAP = 0.5


@dataclass(frozen=True)
class Span:
    paragraph_id: str
    start: int
    end: int


@dataclass(frozen=True)
class Claim:
    """A gold or predicted claim: its type and every span quoting it."""

    type: str
    spans: tuple[Span, ...]


def overlap(a: Span, b: Span) -> float:
    """Shared characters as a share of the shorter span; 0 across paragraphs."""
    if a.paragraph_id != b.paragraph_id:
        return 0.0
    shared = min(a.end, b.end) - max(a.start, b.start)
    shortest = min(a.end - a.start, b.end - b.start)
    return shared / shortest if shared > 0 and shortest > 0 else 0.0


def similarity(a: Claim, b: Claim) -> float:
    return max((overlap(x, y) for x in a.spans for y in b.spans), default=0.0)


def match(
    predicted: list[Claim], gold: list[Claim], threshold: float = MIN_OVERLAP
) -> list[tuple[int, int]]:
    """One-to-one (predicted index, gold index) pairs, best overlap first; ties go to
    the earlier claim."""
    candidates = sorted(
        (
            (s, i, j)
            for i, p in enumerate(predicted)
            for j, g in enumerate(gold)
            if (s := similarity(p, g)) >= threshold
        ),
        key=lambda c: (-c[0], c[1], c[2]),
    )
    used_p: set[int] = set()
    used_g: set[int] = set()
    pairs = []
    for _, i, j in candidates:
        if i in used_p or j in used_g:
            continue
        used_p.add(i)
        used_g.add(j)
        pairs.append((i, j))
    return sorted(pairs)


@dataclass
class Tally:
    """Counts that add up across passages; rates are computed from the totals."""

    predicted: int = 0
    gold: int = 0
    matched: int = 0
    same_type: int = 0
    # Quotes the model gave, how many were found in the filing, and how many of those
    # sat in the paragraph it cited.
    quotes: int = 0
    found: int = 0
    misattributed: int = 0
    by_type: dict[str, "Tally"] = field(default_factory=dict)

    def add(self, other: "Tally") -> None:
        for name in ("predicted", "gold", "matched", "same_type", "quotes", "found"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.misattributed += other.misattributed
        for kind, tally in other.by_type.items():
            self.by_type.setdefault(kind, Tally()).add(tally)

    def rates(self) -> dict[str, float | None]:
        precision = _ratio(self.matched, self.predicted)
        recall = _ratio(self.matched, self.gold)
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall > 0
            else None
        )
        return {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "type_accuracy": _ratio(self.same_type, self.matched),
            "grounding_rate": _ratio(self.found, self.quotes),
            "citation_accuracy": _ratio(self.found - self.misattributed, self.found),
        }


def score(predicted: list[Claim], gold: list[Claim]) -> tuple[Tally, list[tuple[int, int]]]:
    """Precision/recall counts for one passage, overall and per claim type.

    Per type, precision counts predicted claims of that type and recall counts gold
    claims of that type; a matched pair counts only when both claims have that type.
    """
    pairs = match(predicted, gold)
    tally = Tally(
        predicted=len(predicted),
        gold=len(gold),
        matched=len(pairs),
        same_type=sum(predicted[i].type == gold[j].type for i, j in pairs),
    )
    for kind in {c.type for c in [*predicted, *gold]}:
        tally.by_type[kind] = Tally(
            predicted=sum(c.type == kind for c in predicted),
            gold=sum(c.type == kind for c in gold),
            matched=sum(gold[j].type == kind and predicted[i].type == kind for i, j in pairs),
            same_type=sum(gold[j].type == kind and predicted[i].type == kind for i, j in pairs),
        )
    return tally, pairs


def total(tallies: Iterable[Tally]) -> Tally:
    out = Tally()
    for t in tallies:
        out.add(t)
    return out


def _ratio(n: int, d: int) -> float | None:
    return n / d if d else None
