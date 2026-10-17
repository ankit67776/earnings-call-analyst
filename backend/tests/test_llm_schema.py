from enum import StrEnum

import pytest
from pydantic import BaseModel, Field

from app.llm.schema import strict_schema


class Kind(StrEnum):
    guidance = "guidance"
    risk = "risk"


class Quote(BaseModel):
    text: str
    paragraph_id: str


class Claim(BaseModel):
    # Field names that collide with schema keywords must survive.
    title: str
    default: int | None = None
    kind: Kind
    quotes: list[Quote] = Field(description="Verbatim supporting quotes")


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _objects(item)


def test_every_object_is_closed_and_fully_required() -> None:
    schema = strict_schema(Claim)
    objects = list(_objects(schema))
    assert len(objects) == 2  # Claim and Quote
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])


def test_keeps_field_names_and_descriptions_drops_titles_and_defaults() -> None:
    schema = strict_schema(Claim)
    props = schema["properties"]
    assert list(props) == ["title", "default", "kind", "quotes"]
    assert props["default"] == {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert props["quotes"]["description"] == "Verbatim supporting quotes"
    assert "title" not in schema
    assert "title" not in schema["$defs"]["Quote"]
    assert schema["$defs"]["Kind"] == {"enum": ["guidance", "risk"], "type": "string"}


def test_rejects_free_form_dicts() -> None:
    class Loose(BaseModel):
        extra: dict[str, int]

    with pytest.raises(ValueError, match="free-form"):
        strict_schema(Loose)
