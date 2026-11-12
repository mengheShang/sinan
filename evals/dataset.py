"""Load and check an evaluation set: emails plus the labels a correct extractor gives.

The format is described in evals/README.md. A sample that breaks it fails
loudly. Error messages hold a line number and a field path only, never the
email text, so a bad real sample can be reported safely.
"""

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
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

SYNTHETIC = Path(__file__).with_name("synthetic.jsonl")

# The only headers a snapshot keeps: the ones the prefilter's bulk check reads
HEADER_NAMES = frozenset({"List-Unsubscribe", "Precedence"})

MAX_ITEMS = 20
KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class SetError(ValueError):
    """A sample breaks the format. The message names the line and field only."""


def utc_text(value: object) -> object:
    if isinstance(value, str) and value.endswith("Z"):
        return value
    raise ValueError("must be a UTC time ending in Z")


# Lax parsing only here: the text is already known to be a string ending in Z
UtcTime = Annotated[datetime, Field(strict=False), BeforeValidator(utc_text)]
Key = Annotated[str, Field(pattern=KEY_PATTERN)]


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
            raise ValueError("only List-Unsubscribe and Precedence are kept")
        return value


class ItemLabel(Strict):
    key: Key
    kind: Literal["deadline", "todo"]
    course: str | None = Field(max_length=50)
    title: str = Field(min_length=1, max_length=200)
    due_raw: str | None = Field(min_length=1, max_length=200)
    due_at: UtcTime | None
    due_ambiguous: bool
    changes: Key | None

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


class Sample(Strict):
    id: Key
    input: Snapshot
    labels: Labels
    tags: list[Key] = []

    @model_validator(mode="after")
    def due_raw_in_email(self) -> "Sample":
        text = squash(self.input.subject + "\n" + self.input.body_text)
        for item in self.labels.items:
            if item.due_raw is not None and squash(item.due_raw) not in text:
                raise ValueError(f"{item.key}: due_raw must appear in the subject or body")
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


def parse_line(number: int, line: str) -> Sample:
    try:
        return Sample.model_validate_json(line)
    except ValidationError as err:
        raise SetError(f"line {number}: {describe(err)}") from None


def check_links(samples: list[Sample], lines: list[int]) -> None:
    """Ids and keys are unique, and `changes` points to an item from an earlier email."""
    ids: set[str] = set()
    sent: dict[str, datetime] = {}
    for sample, number in zip(samples, lines, strict=True):
        if sample.id in ids:
            raise SetError(f"line {number}: id: duplicate")
        ids.add(sample.id)
        for item in sample.labels.items:
            if item.key in sent:
                raise SetError(f"line {number}: labels.items.{item.key}: duplicate key")
            sent[item.key] = sample.input.sent_at
    for sample, number in zip(samples, lines, strict=True):
        for item in sample.labels.items:
            if item.changes is None:
                continue
            target = sent.get(item.changes)
            if target is None:
                raise SetError(f"line {number}: {item.key}.changes: unknown key")
            if target >= sample.input.sent_at:
                raise SetError(f"line {number}: {item.key}.changes: must be an earlier email")


def load_lines(lines: Iterable[str]) -> list[Sample]:
    samples = []
    numbers = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        samples.append(parse_line(number, line))
        numbers.append(number)
    if not samples:
        raise SetError("the set is empty")
    check_links(samples, numbers)
    return samples


def load(path: Path = SYNTHETIC) -> list[Sample]:
    text = path.read_text(encoding="utf-8")
    return load_lines(text.splitlines())
