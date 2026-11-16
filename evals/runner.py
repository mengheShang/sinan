"""Run each sample through the production pipeline and build a summary report.

The path is the one the Worker takes (app/db/extract.py): the prefilter, the
prepared text, the model, deterministic validation and sender trust (ADR
0010). Only where the reply
comes from changes: a recorded reply, or a live model call.

Pure except for the `reply` and `links` callables it is given. The report
holds counts and metrics only, never an email, a subject, a sender, an id or
a per-email result (ADR 0009, decision 5).

Changes are linked twice over the same extraction replies: once by the
deterministic matcher alone, and once with the link step (ADR 0019), which
asks the model when the matcher finds no candidate or several. The headline
`change_accuracy` is the second, the Worker's behavior; `linking` reports
both and the link step's counts.

A report can also split the set into named parts, such as the random dev draw
and its `deadline_boost` supplement (Issue #78). Each part gets the same counts
and metrics, scored over its own emails after one run over the whole set, so
a change in one part still links to an item in the other.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.extraction.link_prompt import LINK_PROMPT_VERSION
from app.extraction.prefilter import Envelope, Rules, decide
from app.extraction.prompt import PROMPT_VERSION, prepare
from app.extraction.trust import Trust, sender_trust
from app.extraction.validate import UNTRUSTED, Checked, ValidItem, check, hold_untrusted, is_change
from app.items.linking import FAILED, NO_CHOICES, ask
from app.items.matching import Known, choices, match
from evals.dataset import Sample
from evals.metrics import Email, Found, Ratio, Scores, pairs_of, score

# Gives the raw model reply for one sample that passed the prefilter
Reply = Callable[[Sample], str]

# Gives the raw link reply for one change: (found uid, link prompt, text)
Links = Callable[[str, str, str], str]

REFUSED = ("bad_json", "bad_schema", "out_of_list")


@dataclass(frozen=True)
class Outcome:
    """What the pipeline did with one sample. Kept in memory, never reported."""

    sample: Sample
    skipped: bool
    checked: Checked | None
    # Sender trust, decided for every email, skipped or not
    trust: Trust = Trust(True, "trusted")


def envelope(sample: Sample) -> Envelope:
    snap = sample.input
    return Envelope(snap.role, snap.from_addr, snap.subject, snap.headers)


def process(sample: Sample, rules: Rules, reply: Reply) -> Outcome:
    env = envelope(sample)
    trust = sender_trust(env, rules)
    decision = decide(env, rules)
    if decision.action == "skip":
        return Outcome(sample, True, None, trust)
    raw = reply(sample)
    snap = sample.input
    checked = check(raw, snap.body_text, snap.sent_at)
    checked = hold_untrusted(checked, trust)
    return Outcome(sample, False, checked, trust)


def in_order(samples: Sequence[Sample]) -> list[Sample]:
    """Oldest first, as the Worker sees mail, so a change comes after its target."""
    return sorted(samples, key=lambda s: (s.input.sent_at, s.id))


def run(samples: Sequence[Sample], rules: Rules, reply: Reply) -> list[Outcome]:
    ordered = in_order(samples)
    return [process(sample, rules, reply) for sample in ordered]


@dataclass
class LinkCounts:
    """The link step's outcomes over a run. Counts only."""

    asked: int = 0
    picked: int = 0
    refused: int = 0
    failed: int = 0


def email_text(sample: Sample) -> str:
    snap = sample.input
    return prepare(snap.subject, snap.from_addr, snap.sent_at, snap.body_text)


@dataclass
class Linker:
    """Links each change to the earlier item it changes, as extraction does.

    Uses the production matcher (app/items/matching.py) over the items found
    so far, except notices, as extraction does. With `links`, the link step
    (app/items/linking.py) asks for a pick when the matcher finds no
    candidate or several. An item stands for one tracked item; a change
    linked to it becomes that item's newest found item, with the change's
    due time and email date, so the next change in a chain links to it. An
    update left without a target is kept as a new item, as extraction saves
    it.
    """

    links: Links | None = None
    counts: LinkCounts = field(default_factory=LinkCounts)
    pool: dict[int, Known[int]] = field(default_factory=dict)
    # Newest found uid of each tracked item, and the sample it came from
    latest: list[str] = field(default_factory=list)
    origin: list[str] = field(default_factory=list)

    def add(self, uid: str, sample: Sample, item: ValidItem) -> None:
        key = len(self.latest)
        self.latest.append(uid)
        self.origin.append(sample.id)
        if item.kind == "notice":
            return
        sent = sample.input.sent_at
        self.pool[key] = Known(key, item.course, item.title, item.due_at, sent)

    def count(self, code: str) -> None:
        if code == NO_CHOICES:
            return
        self.counts.asked += 1
        if code == "picked":
            self.counts.picked += 1
        elif code == FAILED:
            self.counts.failed += 1
        elif code in REFUSED:
            self.counts.refused += 1

    def pick(
        self, uid: str, sample: Sample, item: ValidItem, earlier: list[Known[int]]
    ) -> int | None:
        """The link step's pick among `earlier`, or None without `links`."""
        if self.links is None:
            return None
        links = self.links
        sent = sample.input.sent_at
        options = choices(item.course, sent, earlier)

        def call(prompt: str, text: str) -> str:
            return links(uid, prompt, text)

        picked = ask(call, email_text(sample), item, options)
        self.count(picked.code)
        return picked.target

    def target_of(self, uid: str, sample: Sample, item: ValidItem) -> int | None:
        earlier = [k for k in self.pool.values() if self.origin[k.key] != sample.id]
        found = match(item.course, item.title, earlier)
        if found.outcome == "one":
            return found.target
        return self.pick(uid, sample, item, earlier)

    def link(self, uid: str, sample: Sample, item: ValidItem) -> str | None:
        """The uid this item changes, or None after adding it as a new item."""
        key = self.target_of(uid, sample, item) if is_change(item) else None
        if key is None:
            self.add(uid, sample, item)
            return None
        target = self.latest[key]
        self.latest[key] = uid
        old = self.pool[key]
        self.pool[key] = Known(key, old.course, old.title, item.due_at, sample.input.sent_at)
        return target


def found_of(outcome: Outcome, linker: Linker | None = None) -> list[Found]:
    """The found items of one email. Without a linker, nothing is linked."""
    if outcome.checked is None:
        return []
    if linker is None:
        linker = Linker()
    items: list[ValidItem] = outcome.checked.items
    sample = outcome.sample
    found = []
    for n, item in enumerate(items):
        uid = f"{sample.id}/{n}"
        target = linker.link(uid, sample, item)
        kept = Found(uid, item.course, item.title, item.due_at, item.status, target, item.kind)
        found.append(kept)
    return found


def to_emails(outcomes: Sequence[Outcome], linker: Linker | None = None) -> list[Email]:
    """Each email's labels and found items. Outcomes must be oldest first.

    Without a linker, changes are linked by the deterministic matcher only.
    """
    if linker is None:
        linker = Linker()
    return [Email(o.sample.labels.items, found_of(o, linker)) for o in outcomes]


def ratio_data(ratio: Ratio) -> dict[str, Any]:
    value = ratio.value
    rounded = None if value is None else round(value * 100, 2)
    return {"percent": rounded, "hits": ratio.hits, "total": ratio.total}


def scores_data(scores: Scores) -> dict[str, Any]:
    return {
        "item_recall": ratio_data(scores.recall),
        "item_precision": ratio_data(scores.precision),
        "due_accuracy": ratio_data(scores.due_accuracy),
        "change_accuracy": ratio_data(scores.change_accuracy),
        "demoted_to_notice": ratio_data(scores.demoted),
        "notice_escalated": ratio_data(scores.escalated),
    }


def groups_data(scores: Scores) -> dict[str, int]:
    """Emails by labeled group and found group, keyed "<labeled>_to_<found>"."""
    return {f"{a}_to_{b}": n for (a, b), n in scores.groups.items()}


def count_data(outcomes: Sequence[Outcome], scores: Scores) -> dict[str, int]:
    skipped = sum(o.skipped for o in outcomes)
    invalid = sum(1 for o in outcomes if o.checked and o.checked.status == "invalid")
    return {
        "emails": scores.emails,
        "prefilter_skipped": skipped,
        "invalid_replies": invalid,
        "labeled_items": scores.labeled,
        "found_items": scores.found,
        "matched_items": scores.matched,
    }


def parts_data(
    outcomes: Sequence[Outcome], emails: Sequence[Email], parts: Mapping[str, str]
) -> dict[str, Any]:
    """Counts and metrics per part. `parts` maps a sample id to its part name."""
    found = {}
    for name in sorted(set(parts.values())):
        keep = [i for i, o in enumerate(outcomes) if parts.get(o.sample.id) == name]
        part_outcomes = [outcomes[i] for i in keep]
        part_emails = [emails[i] for i in keep]
        scores = score(part_emails, context=emails)
        counts = count_data(part_outcomes, scores)
        found[name] = {
            "counts": counts,
            "metrics": scores_data(scores),
            "email_groups": groups_data(scores),
        }
    return found


@dataclass(frozen=True)
class Linked:
    """Each email's found items, linked with the link step and without it."""

    emails: list[Email]
    plain: list[Email]
    counts: LinkCounts
    # Whether the link step was used at all
    used: bool


def link_all(outcomes: Sequence[Outcome], links: Links | None) -> Linked:
    plain = to_emails(outcomes)
    if links is None:
        return Linked(plain, plain, LinkCounts(), False)
    linker = Linker(links)
    emails = to_emails(outcomes, linker)
    return Linked(emails, plain, linker.counts, True)


def spurious(emails: Sequence[Email]) -> int:
    """Found items linked to an earlier item although their label is no change."""
    pairs = pairs_of(emails)
    return sum(1 for label, item in pairs if label.changes is None and item.target is not None)


def linking_data(linked: Linked) -> dict[str, Any]:
    """Change accuracy with and without the link step, and its counts."""
    plain = score(linked.plain).change_accuracy
    full = score(linked.emails).change_accuracy
    counts = linked.counts
    used = linked.used
    return {
        "link_prompt_version": LINK_PROMPT_VERSION if used else None,
        "change_accuracy_without_llm": ratio_data(plain),
        "change_accuracy_with_llm": ratio_data(full) if used else None,
        "spurious_links_without_llm": spurious(linked.plain),
        "spurious_links_with_llm": spurious(linked.emails) if used else None,
        "asked": counts.asked,
        "picked": counts.picked,
        "refused": counts.refused,
        "failed": counts.failed,
    }


REQUIRED = ("deadline", "todo")


def trust_role(outcomes: Sequence[Outcome], role: str) -> dict[str, Any]:
    """Labeled required items of one account role, and how many are held."""
    mine = [o for o in outcomes if o.sample.input.role == role]
    required = 0
    held = 0
    for outcome in mine:
        labels = [i for i in outcome.sample.labels.items if i.kind in REQUIRED]
        required += len(labels)
        if not outcome.trust.trusted:
            held += len(labels)
    share = Ratio(held, required)
    return {"emails": len(mine), "required_items": required, "held": ratio_data(share)}


def trust_data(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    """Sender trust over a run (ADR 0010). Counts only, never a sender.

    `held` per role is the share of labeled required items whose email is
    not trusted, so they would wait in needs_review as `untrusted_sender`:
    the false-positive rate when the labels are real deadlines and to-dos.
    """
    reasons: dict[str, int] = {}
    for outcome in outcomes:
        code = outcome.trust.reason
        reasons[code] = reasons.get(code, 0) + 1
    found = [i for o in outcomes if o.checked for i in o.checked.items]
    held = sum(1 for item in found if item.review == UNTRUSTED)
    return {
        "email_reasons": dict(sorted(reasons.items())),
        "found_items_held": held,
        "roles": {role: trust_role(outcomes, role) for role in ("school", "personal")},
    }


def report(
    set_name: str,
    mode: str,
    model: str,
    outcomes: Sequence[Outcome],
    parts: Mapping[str, str] | None = None,
    links: Links | None = None,
    linked: Linked | None = None,
) -> dict[str, Any]:
    """Summary metrics and counts only, plus the same per part when given.

    `links` gives the link step's replies; without it changes are linked by
    the deterministic matcher only. `linked` is an earlier `link_all` of the
    same outcomes, so the link step is not asked twice.
    """
    if linked is None:
        linked = link_all(outcomes, links)
    emails = linked.emails
    scores = score(emails)
    result: dict[str, Any] = {
        "set": set_name,
        "mode": mode,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "counts": count_data(outcomes, scores),
        "metrics": scores_data(scores),
        "email_groups": groups_data(scores),
        "linking": linking_data(linked),
        "sender_trust": trust_data(outcomes),
    }
    if parts:
        result["parts"] = parts_data(outcomes, emails, parts)
    return result
