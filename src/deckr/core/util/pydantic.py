from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue

JsonObject = dict[str, JsonValue]


def to_camel(s: str) -> str:
    parts = s.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def to_pascal(s: str) -> str:
    """PascalCase: first letter uppercase (for Elgato manifest JSON)."""
    c = to_camel(s)
    return c[0].upper() + c[1:] if c else c


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )
