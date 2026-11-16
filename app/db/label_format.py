"""The label format of evals/README.md: a frozen snapshot plus its labels.

These models live in `app/` because the API and `app/db/labels.py` use them
and the backend image does not ship `evals/`. `evals/dataset.py` re-exports
them and adds the set loader on top.

Error messages hold field paths only, never the email text, so a bad real
sample can be reported safely.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# The only headers a snapshot keeps: the ones the prefilter's bulk check
# reads, and Gmail's authentication results for sender trust (ADR 0010)
HEADER_NAMES = frozenset({"List-Unsubscribe", "Precedence", "Authentication-Results"})

MAX_ITEMS = 20
KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


def utc_text(value: object) -> object:
    if isinstance(value, str) and value.endswith("Z"):
        return value
    raise ValueError("must be a UTC time ending in Z")


# Lax parsing only here: the text is already known to be a string ending in Z
UtcTime = Annotated[datetime, Field(strict=False), BeforeValidator(utc_text)]
Ref = Annotated[str, Field(pattern=KEY_PATTERN)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Snapshot(Strict):
    """Exactly what the extractor and the prefilter see of one email."""

    from_addr: str = Field(max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    subject: str = Field(max_length=1000)
    body_text: str = Field(min_length=1, max_length=50_000)
    sent_at: UtcTime
    role: Literal["school", "personal"]
    headers: dict[str, str]

    @field_validator("headers", mode="before")
    @classmethod
    def known_headers(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("must be an object")
        if not set(value) <= HEADER_NAMES:
            raise ValueError("only the headers in HEADER_NAMES are kept")
        return value


class ItemLabel(Strict):
    ref: Ref
    kind: Literal["deadline", "todo", "notice"]
    course: str | None = Field(max_length=50)
    title: str = Field(min_length=1, max_length=200)
    due_raw: str | None = Field(min_length=1, max_length=200)
    due_at: UtcTime | None
    due_ambiguous: bool
    changes: Ref | None

    @model_validator(mode="after")
    def due_fields_agree(self) -> "ItemLabel":
        if self.due_raw is None:
            if self.due_at is not None or self.due_ambiguous:
                raise ValueError("due_at and due_ambiguous need a due_raw")
            return self
        if self.due_at is None and not self.due_ambiguous:
            raise ValueError("a due_raw without due_at must set due_ambiguous")
        if self.due_at is not None and self.due_ambiguous:
            raise ValueError("an ambiguous due time must have due_at null")
        return self


class Labels(Strict):
    items: list[ItemLabel] = Field(max_length=MAX_ITEMS)
    safe_to_auto_read: bool


def squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def missing_due_raw(snapshot: Snapshot, labels: Labels) -> list[str]:
    """Refs of items whose due_raw is not in the subject or body."""
    text = squash(snapshot.subject + "\n" + snapshot.body_text)
    missing = []
    for item in labels.items:
        if item.due_raw is not None and squash(item.due_raw) not in text:
            missing.append(item.ref)
    return missing


class Sample(Strict):
    id: Ref
    input: Snapshot
    labels: Labels
    tags: list[Ref] = []

    @model_validator(mode="after")
    def due_raw_in_email(self) -> "Sample":
        missing = missing_due_raw(self.input, self.labels)
        if missing:
            raise ValueError(f"{missing[0]}: due_raw must appear in the subject or body")
        return self

    @property
    def has_item(self) -> bool:
        return bool(self.labels.items)


def describe(err: ValidationError) -> str:
    """Field paths and messages only: the input values are left out."""
    parts = []
    for detail in err.errors(include_url=False, include_input=False, include_context=False):
        path = ".".join(str(p) for p in detail["loc"]) or "sample"
        parts.append(f"{path}: {detail['msg']}")
    return "; ".join(parts)
