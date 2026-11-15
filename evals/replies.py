"""Recorded model replies: the CI mode, with no API key and no cost.

A replies file sits next to its set and maps each sample id to the reply the
model gave (or, for the synthetic set, would give). A reply is a JSON object,
stored as is, or a string, used as raw text so broken replies can be tested
too. The file names the prompt version it was recorded with; a different
version is refused, because the replies would no longer match the prompt.

Link replies (ADR 0019) sit in their own file next to the set, recorded for
the link prompt's version. Each maps a found item's uid ("<sample id>/<n>",
the change the model was asked about) to the reply. Only changes the
deterministic matcher could not place are asked, so only those have one.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.extraction.link_prompt import LINK_PROMPT_VERSION
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


def parse(text: str, version: str = PROMPT_VERSION) -> dict[str, str]:
    try:
        data = RepliesFile.model_validate_json(text)
    except ValidationError:
        raise SetError("the replies file is malformed") from None
    if data.prompt_version != version:
        found = data.prompt_version
        raise SetError(f"replies were recorded for prompt {found}, not {version}")
    return {sample_id: raw_text(reply) for sample_id, reply in data.replies.items()}


def load(path: Path, version: str = PROMPT_VERSION) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return parse(text, version)


def load_links(path: Path) -> dict[str, str]:
    return load(path, LINK_PROMPT_VERSION)


class Recorded:
    """Looks up the reply for a sample by id. A missing reply fails loudly."""

    def __init__(self, replies: Mapping[str, str]) -> None:
        self._replies = dict(replies)

    def __call__(self, sample: Sample) -> str:
        reply = self._replies.get(sample.id)
        if reply is None:
            raise SetError(f"{sample.id}: no recorded reply")
        return reply


class RecordedLinks:
    """Looks up the link reply for a change by its uid. A missing one fails loudly."""

    def __init__(self, replies: Mapping[str, str]) -> None:
        self._replies = dict(replies)

    def __call__(self, uid: str, prompt: str, text: str) -> str:
        reply = self._replies.get(uid)
        if reply is None:
            raise SetError(f"{uid}: no recorded link reply")
        return reply


def dump(replies: Mapping[str, str], version: str = PROMPT_VERSION) -> str:
    """A replies file for live replies, sorted by id so diffs stay small."""
    parsed: dict[str, Any] = {}
    for sample_id in sorted(replies):
        raw = replies[sample_id]
        try:
            parsed[sample_id] = json.loads(raw)
        except ValueError:
            parsed[sample_id] = raw
    data = {"prompt_version": version, "replies": parsed}
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
