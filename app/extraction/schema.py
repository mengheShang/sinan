"""The only shape the model may return, as Pydantic models and as JSON Schema.

The JSON Schema goes to the API as a structured output format. The Pydantic
models check the reply again in deterministic code, so a reply that breaks
the schema, adds a field or asks for an action is refused either way.

`kind` is the only field that ranks an item: "notice" makes it less
prominent (ADR 0017). No value makes an item more prominent or lets the
model act.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_ITEMS = 20
MAX_TITLE = 200
MAX_COURSE = 50
MAX_DUE_RAW = 200
MAX_EVIDENCE = 1000
KINDS = ("deadline", "todo", "notice")


class ExtractedItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["deadline", "todo", "notice"]
    course: str | None = Field(max_length=MAX_COURSE)
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    due_raw: str | None = Field(max_length=MAX_DUE_RAW)
    evidence: str = Field(max_length=MAX_EVIDENCE)
    change_type: Literal["create", "update"]


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[ExtractedItem] = Field(max_length=MAX_ITEMS)


def nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "course": nullable("string"),
        "title": {"type": "string"},
        "due_raw": nullable("string"),
        "evidence": {"type": "string"},
        "change_type": {"type": "string", "enum": ["create", "update"]},
    },
    "required": ["kind", "course", "title", "due_raw", "evidence", "change_type"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": ITEM_SCHEMA}},
    "required": ["items"],
    "additionalProperties": False,
}
