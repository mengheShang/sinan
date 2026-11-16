"""Decide whether a message's sender is trusted (ADR 0010). Pure functions only.

Trust comes only from things the email body cannot control:

1. The sender's address or domain is on a trusted list: the prefilter's
   `[allow]` senders and domains, the `[trust]` section of the same file,
   and the domain of each connected school account (`with_school`).
   Subject keywords never trust a sender, since the sender picks the
   subject.
2. Gmail authenticated that domain. The first `Authentication-Results`
   header, which Gmail adds above every header the sender wrote, must come
   from `mx.google.com` and show a pass aligned with the trusted rule:
   `dmarc=pass` for the From domain, or `dkim=pass` or `spf=pass` for a
   domain the rule covers. An explicit `dmarc=fail` is never trusted.
   `[trust] require_auth = false` skips this step.

Nothing in the body, and nothing the model says, can raise trust. Every
result carries a fixed reason code, never an address, so it is safe to
store and log.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from email.utils import parseaddr

from app.extraction.prefilter import (
    Envelope,
    Rules,
    domain_matches,
    domain_of,
    header,
)

AUTH_HEADER = "Authentication-Results"
# The authserv-id Gmail writes on its own results
GMAIL_AUTHSERV = "mx.google.com"

TRUSTED = "trusted"
REASONS = frozenset({TRUSTED, "no_sender", "unknown_sender", "no_auth", "auth_failed"})

# A parenthesized comment, with no nested parentheses inside it
COMMENT = re.compile(r"\([^()]*\)")


@dataclass(frozen=True)
class Trust:
    trusted: bool
    # One of REASONS
    reason: str


@dataclass(frozen=True)
class Check:
    """One authentication result, such as dkim=pass header.d=example.edu."""

    method: str
    result: str
    props: Mapping[str, str]


def strip_comments(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = COMMENT.sub(" ", text)
    return text


def parse_check(part: str) -> Check | None:
    words = part.split()
    if not words or "=" not in words[0]:
        return None
    method, result = words[0].split("=", 1)
    props = {}
    for word in words[1:]:
        if "=" in word:
            key, value = word.split("=", 1)
            props[key.lower()] = value.lower()
    return Check(method.lower(), result.lower(), props)


def gmail_checks(headers: Mapping[str, str]) -> list[Check] | None:
    """Gmail's own results, or None when the first such header is not Gmail's."""
    value = header(headers, AUTH_HEADER)
    if value is None:
        return None
    parts = strip_comments(value).split(";")
    authserv = parts[0].split()
    if not authserv or authserv[0].lower() != GMAIL_AUTHSERV:
        return None
    checks = [parse_check(part) for part in parts[1:]]
    return [check for check in checks if check is not None]


def domain_part(value: str | None) -> str | None:
    """The domain of "user@example.edu", "@example.edu" or "example.edu"."""
    if not value:
        return None
    domain = value.rsplit("@", 1)[-1].strip().strip(".")
    return domain or None


def checked_domain(check: Check) -> str | None:
    """The domain a passing check authenticated, if the check passed."""
    if check.result != "pass":
        return None
    if check.method == "dmarc":
        return domain_part(check.props.get("header.from"))
    if check.method == "dkim":
        signed = check.props.get("header.d") or check.props.get("header.i")
        return domain_part(signed)
    if check.method == "spf":
        return domain_part(check.props.get("smtp.mailfrom"))
    return None


def authenticated(checks: list[Check], scope: frozenset[str]) -> bool:
    """Some check passed for a domain inside `scope`, and DMARC did not fail."""
    if any(c.method == "dmarc" and c.result == "fail" for c in checks):
        return False
    domains = [checked_domain(check) for check in checks]
    return any(domain_matches(domain, scope) for domain in domains)


def scope_of(sender: str, rules: Rules) -> frozenset[str]:
    """The trusted domains this sender falls under; empty when none.

    A trusted address is authenticated against its own domain.
    """
    domain = domain_of(sender)
    if domain is None:
        return frozenset()
    senders = rules.allow.senders | rules.trust.senders
    if sender in senders:
        return frozenset({domain})
    domains = rules.allow.domains | rules.trust.domains
    return frozenset(rule for rule in domains if domain_matches(domain, frozenset({rule})))


def sender_of(env: Envelope) -> str | None:
    address = parseaddr(env.from_addr or "")[1].lower()
    return address if "@" in address else None


def sender_trust(env: Envelope, rules: Rules) -> Trust:
    """Whether items from this message may stay open."""
    sender = sender_of(env)
    if sender is None:
        return Trust(False, "no_sender")
    scope = scope_of(sender, rules)
    if not scope:
        return Trust(False, "unknown_sender")
    if not rules.require_auth:
        return Trust(True, TRUSTED)
    checks = gmail_checks(env.headers)
    if checks is None:
        return Trust(False, "no_auth")
    if not authenticated(checks, scope):
        return Trust(False, "auth_failed")
    return Trust(True, TRUSTED)


def with_school(rules: Rules, addresses: Iterable[str]) -> Rules:
    """The rules plus the domain of each school account address.

    Only school accounts: a personal account's domain, such as a webmail
    provider's, would trust everyone who uses it.
    """
    domains = {domain_of(address) for address in addresses}
    school = frozenset(d for d in domains if d is not None)
    trust = replace(rules.trust, domains=rules.trust.domains | school)
    return replace(rules, trust=trust)
