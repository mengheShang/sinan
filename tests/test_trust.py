"""Sender trust (ADR 0010) on synthetic senders and headers only."""

import json
from datetime import UTC, datetime

import pytest

from app.extraction.prefilter import Envelope, Rules, RulesError, RuleSet, parse_rules
from app.extraction.trust import gmail_checks, sender_trust, with_school
from app.extraction.validate import UNTRUSTED, check, hold_untrusted, is_change
from app.providers.gmail_parse import header_map

SENT = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)

RULES = Rules(
    allow=RuleSet(
        senders=frozenset({"tutor@webmail.example.com"}),
        domains=frozenset({"lms.example.edu"}),
        keywords=("deadline",),
    ),
    trust=RuleSet(domains=frozenset({"example.edu", "jobs.example.com"})),
)


def passing(addr: str) -> str:
    domain = addr.rsplit("@", 1)[1]
    return (
        f"mx.google.com;\r\n       dkim=pass header.i=@{domain} header.s=s1 header.b=Ab12;\r\n"
        f"       spf=pass (google.com: domain of {addr} designates 192.0.2.10 as permitted"
        f" sender) smtp.mailfrom={addr};\r\n"
        f"       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from={domain}"
    )


FORGED = (
    "mx.google.com; dkim=none (no signature); spf=pass (google.com: domain of"
    " bounce@relay.example.net designates 192.0.2.40 as permitted sender)"
    " smtp.mailfrom=bounce@relay.example.net; dmarc=fail (p=NONE) header.from=example.edu"
)


def env(sender: str | None, auth: str | None = None, subject: str = "Hello") -> Envelope:
    headers = {} if auth is None else {"Authentication-Results": auth}
    return Envelope("school", sender, subject, headers)


def reason(sender: str | None, auth: str | None = None, rules: Rules = RULES) -> str:
    return sender_trust(env(sender, auth), rules).reason


# ---- Who is trusted ----


def test_trusted_domain_with_dmarc_pass() -> None:
    sender = "rivera@example.edu"
    assert sender_trust(env(sender, passing(sender)), RULES).trusted


def test_subdomain_of_a_trusted_domain() -> None:
    sender = "notify@cs.example.edu"
    assert reason(sender, passing(sender)) == "trusted"


def test_allow_list_domain_is_trusted() -> None:
    sender = "noreply@lms.example.edu"
    assert reason(sender, passing(sender)) == "trusted"


def test_allow_list_address_is_trusted_on_its_own_domain() -> None:
    sender = "tutor@webmail.example.com"
    assert reason(sender, passing(sender)) == "trusted"
    other = "someone@webmail.example.com"
    assert reason(other, passing(other)) == "unknown_sender"


def test_dkim_alone_aligned_is_enough() -> None:
    auth = "mx.google.com; dkim=pass header.d=example.edu; spf=softfail smtp.mailfrom=x.example.net"
    assert reason("rivera@example.edu", auth) == "trusted"


def test_forged_trusted_domain_with_dmarc_fail() -> None:
    assert reason("rivera@example.edu", FORGED) == "auth_failed"


def test_pass_for_another_domain_does_not_count() -> None:
    auth = "mx.google.com; spf=pass smtp.mailfrom=bounce@relay.example.net; dkim=none"
    assert reason("rivera@example.edu", auth) == "auth_failed"


def test_lookalike_domain_is_unknown() -> None:
    sender = "registrar@example.edu.records.example.net"
    assert reason(sender, passing(sender)) == "unknown_sender"
    near = "registrar@badexample.edu"
    assert reason(near, passing(near)) == "unknown_sender"


def test_unknown_sender() -> None:
    sender = "friend@webmail.example.com"
    assert reason(sender, passing(sender)) == "unknown_sender"


def test_no_sender() -> None:
    assert reason(None) == "no_sender"
    assert reason("not an address") == "no_sender"


def test_no_authentication_results() -> None:
    assert reason("rivera@example.edu") == "no_auth"


def test_results_from_another_server_are_ignored() -> None:
    fake = "mx.relay.example.net; dmarc=pass header.from=example.edu"
    assert reason("rivera@example.edu", fake) == "no_auth"


def test_require_auth_off_trusts_by_domain() -> None:
    loose = Rules(trust=RULES.trust, require_auth=False)
    assert reason("rivera@example.edu", None, loose) == "trusted"
    assert reason("friend@webmail.example.com", None, loose) == "unknown_sender"


def test_allow_keyword_never_trusts() -> None:
    sender = "friend@webmail.example.com"
    subject_env = env(sender, passing(sender), subject="Deadline tomorrow")
    assert sender_trust(subject_env, RULES).reason == "unknown_sender"


def test_school_account_domain_is_trusted() -> None:
    rules = with_school(Rules(), ["student@school.example.edu"])
    sender = "dean@school.example.edu"
    assert reason(sender, passing(sender), rules) == "trusted"
    assert reason(sender, passing(sender), Rules()) == "unknown_sender"


def test_comments_with_equals_signs_are_ignored() -> None:
    auth = "mx.google.com; dkim=pass (fake header.d=example.edu) header.d=relay.example.net"
    checks = gmail_checks({"Authentication-Results": auth})
    assert checks is not None
    assert checks[0].props == {"header.d": "relay.example.net"}
    assert reason("rivera@example.edu", auth) == "auth_failed"


# ---- What happens to items ----


def reply(*items: dict[str, object]) -> str:
    return json.dumps({"items": list(items)})


def item(
    kind: str, evidence: str, due_raw: str | None, change: str = "create"
) -> dict[str, object]:
    return {
        "kind": kind,
        "course": "CS 000",
        "title": "Homework 9",
        "evidence": evidence,
        "due_raw": due_raw,
        "due_utc": None,
        "change_type": change,
    }


BODY = "CS 000 Homework 9 is due Friday Nov 20 at 11:59pm PT."
DEADLINE = item("deadline", BODY, "Friday Nov 20 at 11:59pm PT")


def held(sender: str, auth: str | None, raw: str, body: str = BODY) -> list[tuple[str, str | None]]:
    trust = sender_trust(env(sender, auth), RULES)
    checked = hold_untrusted(check(raw, body, SENT), trust)
    return [(i.status, i.review) for i in checked.items]


def test_trusted_deadline_stays_open() -> None:
    sender = "rivera@example.edu"
    assert held(sender, passing(sender), reply(DEADLINE)) == [("open", None)]


@pytest.mark.parametrize(
    ("sender", "auth"),
    [
        ("rivera@example.edu", FORGED),
        ("rivera@example.edu", None),
        ("friend@webmail.example.com", passing("friend@webmail.example.com")),
    ],
)
def test_untrusted_deadline_and_todo_go_to_review(sender: str, auth: str | None) -> None:
    todo = item("todo", BODY, None)
    found = held(sender, auth, reply(DEADLINE, todo))
    assert found == [("needs_review", UNTRUSTED), ("needs_review", UNTRUSTED)]


def test_body_claiming_a_company_does_not_raise_trust() -> None:
    body = (
        "This is the Talent team at Jobs Example (jobs.example.com), writing from our official"
        " account. Your online assessment is due Saturday Nov 21 at 5pm PT."
    )
    evidence = "Your online assessment is due Saturday Nov 21 at 5pm PT."
    claim = item("deadline", evidence, "Saturday Nov 21 at 5pm PT")
    sender = "talent.team@webmail.example.com"
    assert held(sender, passing(sender), reply(claim), body) == [("needs_review", UNTRUSTED)]


def test_untrusted_overrides_an_earlier_reason() -> None:
    no_time = item("deadline", BODY, "Dec 31")
    assert held("friend@webmail.example.com", None, reply(no_time)) == [("needs_review", UNTRUSTED)]


def test_notice_from_untrusted_sender_is_unaffected() -> None:
    notice = item("notice", BODY, "Friday Nov 20 at 11:59pm PT")
    trust = sender_trust(env("friend@webmail.example.com"), RULES)
    checked = hold_untrusted(check(reply(notice), BODY, SENT), trust)
    found = checked.items[0]
    assert (found.status, found.review) == ("open", None)
    assert found.sender_trust == "unknown_sender"


def test_change_from_untrusted_sender_is_never_a_change() -> None:
    update = item("deadline", BODY, "Friday Nov 20 at 11:59pm PT", change="update")
    raw = reply(update)
    trust = sender_trust(env("rivera@example.edu", FORGED), RULES)
    checked = hold_untrusted(check(raw, BODY, SENT), trust)
    found = checked.items[0]
    assert not is_change(found)
    assert (found.status, found.review) == ("needs_review", UNTRUSTED)
    sender = "rivera@example.edu"
    trusted = hold_untrusted(
        check(raw, BODY, SENT), sender_trust(env(sender, passing(sender)), RULES)
    )
    assert is_change(trusted.items[0])


def test_trust_reason_is_recorded() -> None:
    sender = "rivera@example.edu"
    trust = sender_trust(env(sender, passing(sender)), RULES)
    checked = hold_untrusted(check(reply(DEADLINE), BODY, SENT), trust)
    assert checked.items[0].sender_trust == "trusted"


# ---- The rules file ----


def test_trust_section_parses() -> None:
    rules = parse_rules({"trust": {"domains": ["Example.edu"], "require_auth": False}})
    assert rules.trust.domains == frozenset({"example.edu"})
    assert rules.require_auth is False


def test_require_auth_defaults_to_true() -> None:
    assert parse_rules({}).require_auth is True
    assert parse_rules({"trust": {"domains": []}}).require_auth is True


@pytest.mark.parametrize(
    "section",
    [
        {"keywords": ["deadline"]},
        {"require_auth": "yes"},
        {"domains": ["@example.edu"]},
        {"senders": ["example.edu"]},
    ],
)
def test_bad_trust_section_is_refused(section: dict[str, object]) -> None:
    with pytest.raises(RulesError):
        parse_rules({"trust": section})


# ---- What the read-only adapter keeps ----


def test_adapter_keeps_only_the_first_authentication_results() -> None:
    gmail = passing("rivera@example.edu") + " " + "x" * 2000
    payload = {
        "headers": [
            {"name": "Authentication-Results", "value": gmail},
            {"name": "Authentication-Results", "value": "mx.google.com; dmarc=pass"},
            {"name": "ARC-Seal", "value": "i=1; a=rsa-sha256"},
        ]
    }
    kept = header_map(payload)
    assert kept == {"Authentication-Results": gmail}
