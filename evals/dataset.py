"""Load and check an evaluation set: emails plus the labels a correct extractor gives.

The format is described in evals/README.md; its models live in
app/db/label_format.py and are re-exported here. A sample that breaks it
fails loudly. Error messages hold a line number and a field path only,
never the email text, so a bad real sample can be reported safely.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from app.db.label_format import (
    HEADER_NAMES,
    KEY_PATTERN,
    MAX_ITEMS,
    ItemLabel,
    Labels,
    Ref,
    Sample,
    Snapshot,
    Strict,
    UtcTime,
    describe,
    squash,
    utc_text,
)

__all__ = [
    "HEADER_NAMES",
    "KEY_PATTERN",
    "MAX_ITEMS",
    "SYNTHETIC",
    "ItemLabel",
    "Labels",
    "Ref",
    "Sample",
    "SetError",
    "Snapshot",
    "Strict",
    "UtcTime",
    "check_links",
    "describe",
    "load",
    "load_as",
    "load_lines",
    "load_lines_as",
    "parse_line",
    "squash",
    "utc_text",
]

SYNTHETIC = Path(__file__).with_name("synthetic.jsonl")


class SetError(ValueError):
    """A sample breaks the format. The message names the line and field only."""


def parse_line[S: Sample](number: int, line: str, model: type[S]) -> S:
    try:
        return model.model_validate_json(line)
    except ValidationError as err:
        raise SetError(f"line {number}: {describe(err)}") from None


def check_links(samples: Sequence[Sample], lines: list[int]) -> None:
    """Ids and refs are unique, and `changes` points to an item from an earlier email."""
    ids: set[str] = set()
    sent: dict[str, datetime] = {}
    for sample, number in zip(samples, lines, strict=True):
        if sample.id in ids:
            raise SetError(f"line {number}: id: duplicate")
        ids.add(sample.id)
        for item in sample.labels.items:
            if item.ref in sent:
                raise SetError(f"line {number}: labels.items.{item.ref}: duplicate ref")
            sent[item.ref] = sample.input.sent_at
    for sample, number in zip(samples, lines, strict=True):
        for item in sample.labels.items:
            if item.changes is None:
                continue
            target = sent.get(item.changes)
            if target is None:
                raise SetError(f"line {number}: {item.ref}.changes: unknown ref")
            if target >= sample.input.sent_at:
                raise SetError(f"line {number}: {item.ref}.changes: must be an earlier email")


def load_lines_as[S: Sample](lines: Iterable[str], model: type[S]) -> list[S]:
    """Load lines as `model`, a Sample or a subclass that adds fields."""
    samples = []
    numbers = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        samples.append(parse_line(number, line, model))
        numbers.append(number)
    if not samples:
        raise SetError("the set is empty")
    check_links(samples, numbers)
    return samples


def load_lines(lines: Iterable[str]) -> list[Sample]:
    return load_lines_as(lines, Sample)


def load_as[S: Sample](path: Path, model: type[S]) -> list[S]:
    text = path.read_text(encoding="utf-8")
    return load_lines_as(text.splitlines(), model)


def load(path: Path = SYNTHETIC) -> list[Sample]:
    return load_as(path, Sample)
