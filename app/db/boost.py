"""Which stored mail likely holds a deadline, for the supplementary dev draw (Issue #78).

Deterministic and model-free (ADR 0018): an email is a boost candidate when
its frozen subject or body contains one of the keywords below, or when its
sender is on the course-platform list. Extraction results play no part,
so recall is not measured only on mail the model already handles.

Keywords match whole words, ignoring case, with plural and verb endings:
"exams" and "submitted" count, "finally" and "projector" do not.

The sender list holds real addresses, so it lives in a local file that git
ignores (config/deadline_boost.toml), like the prefilter rules. When that
file is absent, the fictional example list next to this module is used and
the command says so.

Pure except for `load_senders`, which reads the file. Nothing here logs.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.db.label_format import Snapshot
from app.extraction.prefilter import (
    RulesError,
    check_domains,
    check_senders,
    domain_matches,
    domain_of,
    normalize,
)

STRATUM = "deadline_boost"

EXAMPLE_SENDERS = Path(__file__).with_name("deadline_boost.example.toml")

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "mon",
    "tue",
    "tues",
    "wed",
    "thu",
    "thur",
    "thurs",
    "fri",
    "sat",
    "sun",
)

# The Issue #78 list. Each entry is a regular expression for one keyword
# and its endings; all of them are matched as whole words.
KEYWORDS = (
    r"due",
    r"deadlines?",
    r"submit(?:s|ted|ting)?",
    r"exams?",
    r"quiz(?:zes)?",
    r"midterms?",
    r"finals?",
    r"homeworks?",
    r"assignments?",
    r"projects?",
    r"register\s+by",
    r"by\s+(?:" + "|".join(WEEKDAYS) + r")",
    r"no\s+later\s+than",
)

PATTERN = re.compile(r"(?<!\w)(?:" + "|".join(KEYWORDS) + r")(?!\w)", re.IGNORECASE)


@dataclass(frozen=True)
class Senders:
    """Course-platform senders: full addresses, and domains with their subdomains."""

    addresses: frozenset[str] = field(default_factory=frozenset)
    domains: frozenset[str] = field(default_factory=frozenset)


def keyword_hit(text: str) -> bool:
    return PATTERN.search(text) is not None


def sender_hit(from_addr: str | None, senders: Senders) -> bool:
    sender = (from_addr or "").lower()
    if sender and sender in senders.addresses:
        return True
    return domain_matches(domain_of(sender), senders.domains)


def likely_deadline(snapshot: Snapshot, senders: Senders) -> bool:
    """True when the frozen email is a boost candidate."""
    if keyword_hit(snapshot.subject):
        return True
    if keyword_hit(snapshot.body_text):
        return True
    return sender_hit(snapshot.from_addr, senders)


# ---- Loading the sender list ----


def clean(key: str, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise RulesError(f"senders.{key} must be a list of strings")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RulesError(f"senders.{key} must contain non-empty strings only")
        out.append(normalize(item))
    return out


def parse_senders(data: dict[str, Any]) -> Senders:
    """Errors name the key, never a value."""
    unknown = sorted(set(data) - {"senders"})
    if unknown:
        raise RulesError(f"unknown sections: {', '.join(unknown)}")
    raw = data.get("senders", {})
    if not isinstance(raw, dict):
        raise RulesError("[senders] must be a table")
    extra = sorted(set(raw) - {"addresses", "domains"})
    if extra:
        raise RulesError(f"unknown keys in [senders]: {', '.join(extra)}")
    addresses = clean("addresses", raw.get("addresses", []))
    domains = clean("domains", raw.get("domains", []))
    check_senders(addresses)
    check_domains(domains)
    return Senders(frozenset(addresses), frozenset(domains))


def senders_path(local: Path) -> Path:
    """The local list when it exists, else the fictional example list."""
    if local.is_file():
        return local
    return EXAMPLE_SENDERS


def load_senders(path: Path) -> Senders:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return parse_senders(data)
