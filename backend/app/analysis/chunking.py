from dataclasses import dataclass

from app.filings.tenk import Paragraph, Section

# ~1.5K tokens of filing text per call keeps each request well under the free tier's
# 8K tokens/minute with room for the prompt and the JSON output.
DEFAULT_MAX_CHARS = 6_000


@dataclass(frozen=True)
class Chunk:
    section: str
    section_title: str
    paragraphs: list[Paragraph]
    # The heading the first paragraph sits under, when the chunk doesn't open with it.
    context_heading: str | None = None

    @property
    def chars(self) -> int:
        return sum(len(p.text) for p in self.paragraphs)


def chunk_section(
    section: Section, *, max_chars: int = DEFAULT_MAX_CHARS, include_tables: bool = False
) -> list[Chunk]:
    """Group a section's paragraphs into chunks of whole paragraphs.

    Chunks prefer to start at a heading, and a heading is never left dangling at the
    end of a chunk without its text. Tables are skipped by default: MD&A tables repeat
    numbers the text already states and cost many tokens.
    """
    chunks: list[Chunk] = []
    current: list[Paragraph] = []
    heading: str | None = None  # latest heading seen
    context: str | None = None  # heading in force when `current` started

    def close() -> None:
        nonlocal current, context
        # Trailing headings move to the next chunk, with the text they introduce.
        carry: list[Paragraph] = []
        while current and current[-1].kind == "heading":
            carry.insert(0, current.pop())
        if current:
            chunks.append(Chunk(section.id, section.title, current, context))
        current = carry
        context = None

    for p in section.paragraphs:
        if p.kind == "table" and not include_tables:
            continue
        size = sum(len(q.text) for q in current)
        if current and (
            size + len(p.text) > max_chars or (p.kind == "heading" and size > max_chars // 2)
        ):
            close()
        if not current:
            context = None if p.kind == "heading" else heading
        current.append(p)
        if p.kind == "heading":
            heading = p.text
    close()
    return chunks
