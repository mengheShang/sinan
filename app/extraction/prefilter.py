"""Decide which messages may reach the LLM. Pure functions only.

The prefilter looks at the sender, the subject and a few headers, never the
body. The rules (ADR 0006), checked in this order:

1. School accounts: a sensitive subject keyword (grades, rosters and the like)
   always skips, even when an allow rule matches. This is the M0 item 3
   mitigation for USC mail.
2. An allow rule (sender, domain or subject keyword) passes.
3. A skip rule (sender, domain or subject keyword) skips.
4. Bulk-mail headers (List-Unsubscribe, Precedence: bulk) skip.
5. Anything else passes.

A decision carries a fixed reason code, never the sender or the subject, so
it is safe to store and log.
"""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# Fictional rules shipped with the repo. Real sender lists stay in local,
# untracked config (see the file's header).
EXAMPLE_RULES = Path(__file__).with_name("prefilter.example.toml")

BULK_PRECEDENCE = frozenset({"bulk", "list", "junk"})

REASONS = frozenset(
    {
        "sensitive",
        "allow_sender",
        "allow_domain",
        "allow_keyword",
        "skip_sender",
        "skip_domain",
        "skip_keyword",
        "bulk",
        "default",
    }
)

LIST_KEYS = ("senders", "domains", "keywords")
SECTION_KEYS = {
    "allow": LIST_KEYS,
    "skip": LIST_KEYS,
    "school": ("sensitive_keywords",),
}


class RulesError(ValueError):
    """The rules file is malformed. The message names the key, never mail data."""


@dataclass(frozen=True)
class RuleSet:
    senders: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rules:
    allow: RuleSet = RuleSet()
    skip: RuleSet = RuleSet()
    # Applied to school accounts only, before any allow rule
    sensitive_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Envelope:
    """What the prefilter may see of a message."""

    role: str
    from_addr: str | None
    subject: str | None
    headers: Mapping[str, str]


@dataclass(frozen=True)
class Decision:
    action: Literal["pass", "skip"]
    reason: str


def domain_of(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].lower()


def domain_matches(domain: str | None, rules: frozenset[str]) -> bool:
    """A rule for "example.edu" also covers "mail.example.edu"."""
    if domain is None:
        return False
    return any(domain == rule or domain.endswith("." + rule) for rule in rules)


def keyword_hit(subject: str, keywords: tuple[str, ...]) -> bool:
    """Whole-word, case-insensitive match, so "grade" does not hit "upgrade"."""
    for word in keywords:
        pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
        if re.search(pattern, subject):
            return True
    return False


def match(env: Envelope, rules: RuleSet) -> str | None:
    """Which kind of rule matched first: sender, domain, keyword, or None."""
    sender = (env.from_addr or "").lower()
    if sender and sender in rules.senders:
        return "sender"
    if domain_matches(domain_of(sender), rules.domains):
        return "domain"
    subject = normalize(env.subject)
    if keyword_hit(subject, rules.keywords):
        return "keyword"
    return None


def normalize(text: str | None) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed.lower()


def header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def is_bulk(headers: Mapping[str, str]) -> bool:
    if header(headers, "List-Unsubscribe"):
        return True
    precedence = normalize(header(headers, "Precedence"))
    return precedence in BULK_PRECEDENCE


def decide(env: Envelope, rules: Rules) -> Decision:
    subject = normalize(env.subject)
    if env.role == "school" and keyword_hit(subject, rules.sensitive_keywords):
        return Decision("skip", "sensitive")
    allowed = match(env, rules.allow)
    if allowed is not None:
        return Decision("pass", "allow_" + allowed)
    skipped = match(env, rules.skip)
    if skipped is not None:
        return Decision("skip", "skip_" + skipped)
    if is_bulk(env.headers):
        return Decision("skip", "bulk")
    return Decision("pass", "default")


# ---- Loading the rules file ----


def clean_list(section: str, key: str, value: Any) -> list[str]:
    where = f"{section}.{key}"
    if not isinstance(value, list):
        raise RulesError(f"{where} must be a list of strings")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RulesError(f"{where} must contain non-empty strings only")
        out.append(normalize(item))
    return out


def check_senders(values: list[str]) -> None:
    for value in values:
        if value.count("@") != 1 or " " in value:
            raise RulesError("senders must be full addresses like name@example.edu")


def check_domains(values: list[str]) -> None:
    for value in values:
        if "@" in value or " " in value or value.startswith("."):
            raise RulesError("domains must look like example.edu, without @ or a leading dot")


def section_of(data: Mapping[str, Any], name: str) -> dict[str, list[str]]:
    raw = data.get(name, {})
    if not isinstance(raw, dict):
        raise RulesError(f"[{name}] must be a table")
    allowed = SECTION_KEYS[name]
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise RulesError(f"unknown keys in [{name}]: {', '.join(unknown)}")
    return {key: clean_list(name, key, raw.get(key, [])) for key in allowed}


def rule_set(values: dict[str, list[str]]) -> RuleSet:
    check_senders(values["senders"])
    check_domains(values["domains"])
    return RuleSet(
        senders=frozenset(values["senders"]),
        domains=frozenset(values["domains"]),
        keywords=tuple(values["keywords"]),
    )


def parse_rules(data: Mapping[str, Any]) -> Rules:
    unknown = sorted(set(data) - set(SECTION_KEYS))
    if unknown:
        raise RulesError(f"unknown sections: {', '.join(unknown)}")
    allow = rule_set(section_of(data, "allow"))
    skip = rule_set(section_of(data, "skip"))
    school = section_of(data, "school")
    return Rules(allow, skip, tuple(school["sensitive_keywords"]))


def load_rules(path: Path) -> Rules:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return parse_rules(data)
