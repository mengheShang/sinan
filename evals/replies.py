"""Recorded model replies: the CI mode, with no API key and no cost.

A replies file sits next to its set and maps each sample id to the reply the
model gave (or, for the synthetic set, would give). A reply is a JSON object,
stored as is, or a string, used as raw text so broken replies can be tested
too. The file names the prompt version it was recorded with; a different
version is refused, because the replies would no longer match the prompt.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.extraction.prompt import PROMPT_VERSION
from evals.dataset import Sample, SetError


class RepliesFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    prompt_version: str
    replies: dict[str, dict[str, Any] | str]


def raw_text(reply: dict[str, Any] | str) -> str:
    if isinstance(reply, str):
        return reply
    return json.dumps(reply)


def parse(text: str) -> dict[str, str]:
    try:
        data = RepliesFile.model_validate_json(text)
    except ValidationError:
        raise SetError("the replies file is malformed") from None
    if data.prompt_version != PROMPT_VERSION:
        found = data.prompt_version
        raise SetError(f"replies were recorded for prompt {found}, not {PROMPT_VERSION}")
    return {sample_id: raw_text(reply) for sample_id, reply in data.replies.items()}


def load(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return parse(text)


class Recorded:
    """Looks up the reply for a sample by id. A missing reply fails loudly."""

    def __init__(self, replies: Mapping[str, str]) -> None:
        self._replies = dict(replies)

    def __call__(self, sample: Sample) -> str:
        reply = self._replies.get(sample.id)
        if reply is None:
            raise SetError(f"{sample.id}: no recorded reply")
        return reply


def dump(replies: Mapping[str, str]) -> str:
    """A replies file for live replies, sorted by id so diffs stay small."""
    parsed: dict[str, Any] = {}
    for sample_id in sorted(replies):
        raw = replies[sample_id]
        try:
            parsed[sample_id] = json.loads(raw)
        except ValueError:
            parsed[sample_id] = raw
    data = {"prompt_version": PROMPT_VERSION, "replies": parsed}
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
