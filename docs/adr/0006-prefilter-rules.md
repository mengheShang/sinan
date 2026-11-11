# ADR 0006: Prefilter rules

- Status: Accepted
- Date: 2026-11-10
- Decider: Menghe

## Context

Issue #11 drops clearly irrelevant mail before it reaches the LLM, to cut cost and privacy exposure. M0 item 3 requires, for USC mail, preferring course notices and deadlines and excluding mail likely to contain other students' personal information. It first said grades too; decision 4 narrows that. Accounts carry a `school` or `personal` role, chosen when they are connected, so the prefilter can treat the two differently.

The prefilter sees only the sender, the subject and a few headers. It never reads the body.

## Decisions

### 1. Unmatched mail passes, for both roles

A message that no rule matches goes on to extraction. A missed deadline costs more than an extra LLM call, and an allowlist for school mail would need every course sender listed up front. The sensitive rule below still applies to school mail.

### 2. Allow rules win over skip rules

This lets a broad skip rule, such as a whole bulk domain, coexist with an allow rule for one sender on it. The trade-off is that a broad allow keyword can let through mail a skip rule would have caught, so allow keywords should stay specific.

### 3. Bulk headers skip, unless an allow rule matches

`List-Unsubscribe` or `Precedence: bulk | list | junk` marks mailing lists and marketing mail. Course sites often send notifications with these headers too, so their senders or domains must be on the allow list.

### 4. Sensitive keywords cover other students' information, apply to school accounts only, and beat allow rules

They come from the USC policy mitigation, which does not cover personal mail. Within school mail they are checked first, so even an allowed sender's roster mail never reaches the LLM.

Your own grades are not sensitive here. Instructors send each student only their own grades, so grade mail holds no one else's information, and it is your own record to process. Skipping it would also hide feedback and resubmission deadlines. The example list therefore holds terms like "roster" and "class list", not grade terms. This narrows the M0 item 3 mitigation, which first excluded grades as well.

## Order

1. School account and a sensitive subject keyword: skip (`sensitive`).
2. Allow rule by sender, domain or subject keyword: pass (`allow_sender`, `allow_domain`, `allow_keyword`).
3. Skip rule, same kinds: skip (`skip_sender`, `skip_domain`, `skip_keyword`).
4. Bulk headers: skip (`bulk`).
5. Otherwise: pass (`default`).

## Other rules (no decision needed, recorded for review)

- Senders match exactly, ignoring case. A domain covers its subdomains, so `example.edu` matches `mail.example.edu`, but not `notexample.edu`.
- Keywords match whole words or phrases in the subject, ignoring case and extra spaces, so "due" does not match "overdue".
- A decision carries only a fixed reason code, never the sender or subject, so it is safe to store and log.
- The repo ships fictional rules in `app/extraction/prefilter.example.toml`. Real rules live in `config/prefilter.toml`, which git ignores. A malformed file is refused with an error that names the key, not the value.

## Consequences

- #13 wires the prefilter into the Worker: it loads the local rules (falling back to the example), runs `decide` on each new message, and records skips as an `extraction_runs` row with status `skipped` and the reason code.
- Because unmatched mail passes, cost and exposure depend on keeping skip rules current. After the first weeks of live use, the skip counts per reason show which rules to add.
