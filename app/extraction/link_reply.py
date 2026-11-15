"""The only reply the link step accepts, and its check (ADR 0019). Pure.

The reply must be exactly `{"target": <integer>}` or `{"target": null}`.
Anything else is refused and counts as no link: bad JSON, another shape, an
extra field, a boolean or text in place of a number, or a number outside the
list of choices the model was shown.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

# "picked" and "none" are valid replies; the rest are refused
Code = Literal["picked", "none", "bad_json", "bad_schema", "out_of_list"]

LINK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"target": {"type": ["integer", "null"]}},
    "required": ["target"],
    "additionalProperties": False,
}


class LinkReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: int | None


@dataclass(frozen=True)
class Target:
    # The 1-based number of the chosen item, or None for no link
    number: int | None
    code: Code


def parse_target(raw: str, shown: int) -> Target:
    """The chosen number from a raw reply, given `shown` choices."""
    try:
        data = json.loads(raw)
    except ValueError:
        return Target(None, "bad_json")
    try:
        reply = LinkReply.model_validate(data)
    except ValidationError:
        return Target(None, "bad_schema")
    if reply.target is None:
        return Target(None, "none")
    if not 1 <= reply.target <= shown:
        return Target(None, "out_of_list")
    return Target(reply.target, "picked")
