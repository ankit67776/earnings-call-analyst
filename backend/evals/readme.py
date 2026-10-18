"""Write eval results into the README between <!-- eval:NAME --> markers."""

from pathlib import Path

README = Path(__file__).resolve().parent.parent.parent / "README.md"


def replace_block(name: str, body: str, readme: Path = README) -> None:
    start, end = f"<!-- eval:{name} -->", f"<!-- /eval:{name} -->"
    text = readme.read_text(encoding="utf-8")
    if start not in text or end not in text:
        raise SystemExit(f"README.md has no {start} ... {end} block")
    before, rest = text.split(start, 1)
    after = rest.split(end, 1)[1]
    readme.write_text(f"{before}{start}\n{body}\n{end}{after}", encoding="utf-8")
