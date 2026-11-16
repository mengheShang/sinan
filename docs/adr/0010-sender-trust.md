# ADR 0010: Hold items from untrusted senders for review

- Status: Accepted (2026-11-15)
- Date: 2026-11-15
- Decider: Menghe
- Amends: ADR 0007 (LLM extraction), ADR 0014 and ADR 0019 (deadline changes and links)

## Context

Evidence validation (ADR 0007) checks that an item quotes the email, not that the email is real. A complete, explicit deadline in the body of any email becomes an `open` item, whoever sent it (#35, PR #49, "Decisions for the owner", item 2). In M2 the harm is small: nothing external is written, and a false item can be dismissed. In M3, grants and automatic actions may act on an `open` item, so this must be settled first (Issue #51).

The owner agreed the direction on 2026-11-12, in the Issue:

- Deterministic code decides sender trust from things the email body cannot control: the sender domain, and the authentication results Gmail recorded.
- Items from trusted senders stay `open`. Items from other senders go to `needs_review` with reason `untrusted_sender`. They are never dropped, because a missed deadline is worse than an extra prompt (ADR 0009).
- Company or organization names in the body never raise trust, since the sender writes them. A model hint could only lower trust.

This ADR writes down the rule and how it is built. On PR #86 (2026-11-15) the owner accepted all seven recommended decisions: the trust sources, requiring Gmail authentication for listed domains, the notice rule, the change rule, `untrusted_sender` replacing an earlier review reason, the Dashboard text, and the measured false-positive rate (0 of 3 school and 0 of 1 personal labeled required items held on dev set v1).

## Decisions

### 1. Trust sources

A sender is trusted when **both** hold:

1. **The sender is listed.** Its address or domain matches one of:
   - the prefilter's `[allow]` senders and domains (ADR 0006), which already name the course sites and platforms the owner reads;
   - the new `[trust]` section of the same local file (`config/prefilter.toml`, git-ignored), for the school domain and for recruiting, billing and rent platforms that are not on the allow list;
   - the domain of each connected **school** account, so the school domain is trusted without being typed in. A personal account's domain is never added: for a webmail provider it would trust everyone who uses it.

   A domain also covers its subdomains, as in the prefilter. Subject keywords never trust a sender, since the sender picks the subject. The `[skip]` lists play no part.
2. **Gmail authenticated it** (required, see decision 2). The first `Authentication-Results` header must name `mx.google.com` as its server, and show a pass aligned with the listed rule: `dmarc=pass` for the From domain, or `dkim=pass` (`header.d`) or `spf=pass` (`smtp.mailfrom`) for a domain the rule covers. An explicit `dmarc=fail` is never trusted, whatever else passed.

Everything else is untrusted. Each email gets one fixed reason code, stored with every item it produced as `sender_trust` in the version's `after` data:

| Code | Meaning |
|---|---|
| `trusted` | Listed and authenticated. |
| `no_sender` | No usable From address. |
| `unknown_sender` | The address and domain are on no list. |
| `no_auth` | Listed, but Gmail's results are missing, or the first results header is not Gmail's. |
| `auth_failed` | Listed, but no aligned pass, or DMARC failed. |

Later, possibly: people the owner has written to, and mail the owner sent (the `SENT` label is set by Gmail, not by the sender). Neither is built here.

### 2. DMARC (or an aligned DKIM or SPF pass) is required for listed domains

Without it, anyone can write `From: registrar@<school domain>`, and the From header is the only thing the domain check reads. `[trust] require_auth = false` turns the check off for a mailbox where Gmail's results turn out to be unreliable; the default is `true`. Alignment is checked against the listed rule, not against a public suffix list: a DKIM pass for `mail.example.edu` counts for a rule on `example.edu`, and a pass for any other domain does not.

Gmail adds its `Authentication-Results` above every header the sender wrote, and the adapter keeps only the first header of each name. A results header the sender wrote is therefore never the one read, and one that names another server is ignored.

### 3. Required items from untrusted senders wait for review

A `deadline` or `todo` from an untrusted sender is saved with status `needs_review` and review `untrusted_sender`. This reason replaces any earlier one (such as a due time outside the evidence): whether the email is real comes first, and the item keeps its own fields. Nothing is dropped. Trusted senders keep today's behavior.

### 4. Notices are unaffected

A notice keeps its status, `open`, and records its `sender_trust` code. Notices never act, never remind, never enter to-do lists or review, and never expire (ADR 0017, decision 2); they appear once in the morning digest. Holding them would only move the same line into review. "Make it a to-do" is the owner's own action (actor `user`), so a notice the owner promotes is trusted by that choice. If M3 ever lets a notice act or remind, this decision must be revisited.

### 5. A change from an untrusted sender is never a proposal

An update from an untrusted sender is never matched and never asked of the link step (ADR 0014, ADR 0019). It is saved as a new item in `needs_review` with review `untrusted_sender`, so the owner sees it, and the item it would change keeps its time. A proposal still needs the owner's approval, but it shows as "the course moved this deadline" next to a trusted item, which is exactly what a forged email wants. A change that matches nothing and one that would have been "unchanged" are both saved this way, so even a repeated reminder from an untrusted sender is visible.

A trusted change may still update an item that came from an untrusted sender; that item is already in review.

### 6. The body and the model can only lower trust

Trust is decided before and apart from the model's reply. No field of the extraction schema can raise it, and the body is never read for it. A future model hint such as "personal sender, no organization" may only turn a trusted email untrusted; none is added here.

### 7. What the read-only adapter keeps

`Authentication-Results` joins the explicit header allow-list in `app/providers/gmail_parse.py`, with a 4000-character limit instead of 1000, because Gmail's results are long. The adapter makes the same GET requests as before; no new scope, endpoint or method. The value names domains and server addresses, not mail content, and it is never logged. Mail stored before this change has no such header; since extraction reads only never-extracted mail (#75), only messages synced before the deploy and not yet extracted are affected (they are held as `no_auth`).

Evaluation snapshots may keep the header too (`app/db/label_format.py`), so new labels carry it. The existing dev set's snapshots predate it, so the dev run reads the header again from Gmail with the Worker's read-only reader, in memory only (`evals/real.py`).

### 8. Evaluation

The runner applies the same trust step as the Worker. Its report gains `sender_trust`: emails per reason code, the number of found items held, and per account role the share of **labeled** required items whose email is untrusted (`held`). On a labeled set the labels are real deadlines and to-dos, so `held` is the false-positive rate of the rule. The injection set gains six forged-sender cases (a failed DMARC on the school domain, a lookalike domain, missing results, results from another server, a body claiming to be a company, and a forged change) and a new expectation, `held`, for the change: nothing open and nothing linked. Every synthetic and injection snapshot carries a fictional Gmail pass for its own domain, so the older cases still test validation on trusted senders.

## Consequences

- Mail from senders the owner has not listed waits in review until the owner lists them. The dev-set rate in the PR measures how often that happens; listing a few platforms in `[trust]` is the expected fix, not loosening the rule.
- M3 grants and automatic actions can rely on `open` meaning "from a listed, authenticated sender", and should also check `sender_trust` directly rather than infer it from the status.
- The real trusted domains stay in the owner's local, git-ignored file. The repo holds only fictional examples (`app/extraction/prefilter.example.toml`).
- No database migration: the header lives in the existing `messages.headers` JSON, and the code in the existing `item_versions.after` JSON.
