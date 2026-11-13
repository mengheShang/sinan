# ADR 0016: Assume the owner's zone and end of day when a deadline omits them

- Status: Proposed
- Date: 2026-11-13
- Decider: Menghe

## Context

In live use, 23 of the 31 items that went to `needs_review` did so only because the email gave no time zone (`no_zone`). Course mail almost never states one. Those items show the raw due text and have no `due_at`, so the digest cannot tell when they have passed. Date-only deadlines and phrases like "the first Friday after this email" also ended in review (Issue #65).

ADR 0005 required a zone and a time, and ADR 0011 kept that rule. The owner decided on 2026-11-13 to relax it. This ADR changes the "Time zone required" and "Time required" rules of ADR 0005; the rest of ADR 0005 and ADR 0011 stays. ADR 0010 is reserved for sender trust (#51), and 0015 is used by #66.

## Decisions (owner, 2026-11-13)

### 1. A missing zone is the owner's zone

When `due_raw` names no zone, the parser uses `SINAN_TIMEZONE` (default `America/Los_Angeles`) for both mailboxes. The item stays `open` and records `zone_assumed`. The Dashboard shows "zone assumed".

Standard and daylight checks and the DST gap and overlap rules still apply in the assumed zone: "Nov 1 1:30am" needs review (`ambiguous_time`) and "Mar 14 2027 2:30am" needs review (`nonexistent_time`). Relative days ("today", "Friday") count in the assumed zone, as they count in a stated one.

### 2. A missing time is 23:59

When `due_raw` gives a date but no time, the parser uses 23:59 in the stated or assumed zone, as "end of day" does (ADR 0011, decision 1). The item records `time_assumed`. The Dashboard shows "time not stated".

### 3. "the first Friday after this email"

"first Friday after this email", "the Friday after today" and close variants (`following` for `after`; `e-mail`, `message`, `mail`, `announcement` or `post` for `email`; with or without "the" and "first") mean the first such weekday **strictly after** the email's local send date. A Friday email saying "first Friday after this email" means the following Friday. Rules 1 and 2 apply to the time and zone.

### 4. Still ambiguous

These still need review: numeric dates (`numeric_date`), "midnight" (`midnight`), "next Friday" (`relative_weekday`), weekday and date conflicts (`weekday_mismatch`), phrases tied to another event such as "the Friday after the midterm" (`relative_event`), and spans such as "in two weeks" (`no_date`) or "Friday in two weeks" (`relative_span`). The long tail is #54.

## Implementation choices (for the owner's review)

These follow from rules 1 to 4 but were not spelled out in #65.

### 5. A named zone that is not understood is never replaced

Rule 1 covers a **missing** zone. A zone that is named but not understood ("JST", "BST", "London time", "local time", "Beijing" alone) still needs review (`unknown_zone`), because assuming the owner's zone would silently give a wrong time. Next to an understood zone it is `multiple_zones`. "China time" and "Beijing" alone were `no_zone` before; they are now `unknown_zone`.

### 6. A time tied to something else is not 23:59

Rule 2 covers a **missing** time. When `due_raw` has no clock time but ties the time to something else ("before class", "in lecture", "Friday morning", "close of business", "end of week", "before Friday"), 23:59 could be later than the real deadline, so the item needs review (`vague_time`).

### 7. Nothing is assumed that the evidence states

The model sometimes copies only part of the phrase, such as "Nov 18 at 10am" out of "Nov 18 at 10am ET". Assuming the owner's zone then gives a wrong time that looks right (synthetic sample `syn-024`). So, after parsing, if the zone was assumed but the evidence names any zone, or the time was assumed but the evidence gives any clock time, the item needs review (`zone_outside_due` or `time_outside_due`).

### 8. Where the flags are stored

`zone_assumed` and `time_assumed` are stored in the `after` data of the `item_versions` row that sets the due time: the create version, or the version an approved proposal appends. Both sides of an `item_update` proposal's payload carry them too, so the hash covers what the owner sees. No migration is needed. Readers take the flags from the newest version whose `after` has `due_at`; data saved before this ADR has no flags, which reads as nothing assumed.

### 9. Proposals compare due times as before

Change matching (ADR 0014) still compares the parsed `due_at`. A change whose time or zone was assumed and that differs from the item's time becomes a proposal as usual, and the proposal shows the assumed parts on both sides. Nothing is applied without the owner's approval.

## Consequences

- Items whose only problem was a missing zone or time now get a `due_at` and stay `open`, so the digest can place them and tell when they have passed.
- An assumed zone is wrong for mail from another zone that does not say so. The Dashboard label is the owner's cue to check.
- Already-extracted items are not rewritten. The rules apply to new extractions. Re-running extraction on recent mail is a separate, deliberate step.
- The synthetic evaluation's labels follow these rules (see the PR for each changed label).
