from typing import Any

from pydantic import BaseModel

# Pydantic emits these but strict structured outputs don't need them.
_DROP_KEYS = {"title", "default"}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for `model` in the shape strict structured outputs require.

    Every object is closed (additionalProperties: false) and lists all of its
    properties as required. Optional fields stay nullable via anyOf [..., null].
    """
    return _strict(model.model_json_schema())


def _strict(node: Any) -> Any:
    if isinstance(node, list):
        return [_strict(item) for item in node]
    if not isinstance(node, dict):
        return node

    if isinstance(node.get("additionalProperties"), dict):
        raise ValueError("strict schemas need fixed fields; free-form dicts aren't supported")

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS:
            continue
        if key in ("properties", "$defs"):
            # Keys here are field/definition names, not schema keywords.
            out[key] = {name: _strict(sub) for name, sub in value.items()}
        else:
            out[key] = _strict(value)

    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out.get("properties", {}))
    return out
