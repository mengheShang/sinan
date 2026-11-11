"""Prefilter rules on synthetic messages. All senders and domains are fictional."""

from pathlib import Path

import pytest

from app.extraction.prefilter import (
    EXAMPLE_RULES,
    REASONS,
    Decision,
    Envelope,
    Rules,
    RulesError,
    RuleSet,
    decide,
    load_rules,
    parse_rules,
)

RULES = Rules(
    allow=RuleSet(
        senders=frozenset({"prof.rivera@example.edu"}),
        domains=frozenset({"lms.example.edu"}),
        keywords=("assignment", "due"),
    ),
    skip=RuleSet(
        senders=frozenset({"deals@shop.example.com"}),
        domains=frozenset({"news.example.org", "example.edu"}),
        keywords=("newsletter",),
    ),
    sensitive_keywords=("grades posted", "roster"),
)

BULK = {"List-Unsubscribe": "<mailto:unsubscribe@example.org>"}


def env(
    sender: str | None = "someone@example.net",
    subject: str | None = "Hello",
    role: str = "personal",
    headers: dict[str, str] | None = None,
) -> Envelope:
    return Envelope(role, sender, subject, headers or {})


CASES = [
    # Nothing matches: both roles pass by default
    (env(), Decision("pass", "default")),
    (env(role="school"), Decision("pass", "default")),
    (env(sender=None, subject=None), Decision("pass", "default")),
    # Allow rules, by sender, domain (and subdomain) and subject keyword
    (env(sender="Prof.Rivera@Example.edu"), Decision("pass", "allow_sender")),
    (env(sender="notify@lms.example.edu"), Decision("pass", "allow_domain")),
    (env(sender="bot@east.lms.example.edu"), Decision("pass", "allow_domain")),
    (env(subject="Assignment 3 posted"), Decision("pass", "allow_keyword")),
    # Skip rules
    (env(sender="deals@shop.example.com"), Decision("skip", "skip_sender")),
    (env(sender="team@news.example.org"), Decision("skip", "skip_domain")),
    (env(subject="The weekly NEWSLETTER"), Decision("skip", "skip_keyword")),
    # Allow wins over skip: example.edu is skipped, but this sender is allowed
    (env(sender="prof.rivera@example.edu"), Decision("pass", "allow_sender")),
    (env(sender="office@example.edu"), Decision("skip", "skip_domain")),
    (env(subject="Newsletter: assignment tips"), Decision("pass", "allow_keyword")),
    # Bulk headers skip unless an allow rule matches
    (env(headers=BULK), Decision("skip", "bulk")),
    (env(headers={"precedence": " Bulk "}), Decision("skip", "bulk")),
    (env(headers={"Precedence": "list"}), Decision("skip", "bulk")),
    (env(headers={"Precedence": "first-class"}), Decision("pass", "default")),
    (env(headers={"List-Unsubscribe": ""}), Decision("pass", "default")),
    (env(sender="notify@lms.example.edu", headers=BULK), Decision("pass", "allow_domain")),
    # Sensitive keywords: school only, and they beat allow rules
    (env(subject="Midterm grades posted", role="school"), Decision("skip", "sensitive")),
    (
        env(sender="prof.rivera@example.edu", subject="Updated roster", role="school"),
        Decision("skip", "sensitive"),
    ),
    (env(subject="Midterm grades posted"), Decision("pass", "default")),
]


@pytest.mark.parametrize(("message", "expected"), CASES)
def test_decide(message: Envelope, expected: Decision) -> None:
    assert decide(message, RULES) == expected


@pytest.mark.parametrize(
    "subject",
    ["Overdue library book", "Fast assignments", "Upgrade your rosters"],
)
def test_keywords_match_whole_words_only(subject: str) -> None:
    rules = Rules(skip=RuleSet(keywords=("due", "assignment", "roster")))
    assert decide(env(subject=subject), rules) == Decision("pass", "default")


def test_keyword_phrase_ignores_spacing_and_case() -> None:
    message = env(subject="MIDTERM  Grades\tPosted", role="school")
    assert decide(message, RULES) == Decision("skip", "sensitive")


def test_domain_rule_does_not_match_lookalike_domain() -> None:
    rules = Rules(skip=RuleSet(domains=frozenset({"example.org"})))
    message = env(sender="a@notexample.org")
    assert decide(message, rules) == Decision("pass", "default")


def test_reasons_never_carry_message_data() -> None:
    for message, _ in CASES:
        reason = decide(message, RULES).reason
        assert reason in REASONS


def test_example_rules_load() -> None:
    rules = load_rules(EXAMPLE_RULES)
    assert "lms.example.edu" in rules.allow.domains
    assert "roster" in rules.sensitive_keywords


def test_example_rules_are_fictional() -> None:
    rules = load_rules(EXAMPLE_RULES)
    addresses = rules.allow.senders | rules.skip.senders
    domains = rules.allow.domains | rules.skip.domains
    hosts = {a.split("@")[1] for a in addresses} | domains
    fictional = ("example.edu", "example.com", "example.org")
    for host in hosts:
        assert host.endswith(fictional), host


def test_load_rules_from_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.toml"
    path.write_text('[skip]\nkeywords = ["  Webinar "]\n', encoding="utf-8")
    rules = load_rules(path)
    assert rules.skip.keywords == ("webinar",)
    assert rules.allow == RuleSet()


def test_empty_rules_pass_everything() -> None:
    rules = parse_rules({})
    assert decide(env(subject="anything"), rules) == Decision("pass", "default")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"alow": {}}, "unknown sections: alow"),
        ({"allow": {"sender": []}}, r"unknown keys in \[allow\]: sender"),
        ({"allow": []}, r"\[allow\] must be a table"),
        ({"skip": {"keywords": "sale"}}, "skip.keywords must be a list"),
        ({"skip": {"keywords": [""]}}, "non-empty strings"),
        ({"skip": {"keywords": [3]}}, "non-empty strings"),
        ({"allow": {"senders": ["example.edu"]}}, "full addresses"),
        ({"allow": {"domains": ["a@example.edu"]}}, "without @"),
        ({"allow": {"domains": [".example.edu"]}}, "leading dot"),
        ({"school": {"keywords": []}}, r"unknown keys in \[school\]"),
    ],
)
def test_bad_rules_are_refused(data: dict[str, object], message: str) -> None:
    with pytest.raises(RulesError, match=message):
        parse_rules(data)
