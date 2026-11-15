# ADR 0016: Assume the owner's zone and end of day when a deadline omits them

- Status: Accepted (2026-11-13)
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

These still need review: numeric dates (`numeric_date`), "midnight" (`midnight`), "next Friday" (`relative_weekday`), weekday and date conflicts (`weekday_mismatch`), phrases tied to another event such as "the Friday after the midterm" (`relative_event`), and spans other than rule 5's, such as "a week from Friday" or "in a few days" (`relative_span`, or `no_date` when nothing else gives a day). The long tail is #54.

### 5. "in N days" and "within N days" (added 2026-11-13, comment on #65)

"in N days/weeks" and "within N days/weeks" are counted from the email's local send date: the send date plus N days (or 7 × N), at 23:59 in the stated or assumed zone (rules 1 and 2). N is digits or a simple word ("two", "three"). The item records `date_computed`, and the Dashboard shows "computed from email date".

- If the same `due_raw` also gives a date, a weekday, "today"/"tomorrow" or a rule-3 phrase, it is two dates (`multiple_dates`). "Friday in two weeks" is therefore `multiple_dates`, where it was `relative_span` before rule 5.
- "in a few days", "in a couple of weeks", "in several days", "soon" and other vague spans stay in review.
- A time written next to the count is used: "in 2 weeks 5pm PT" is 17:00 PT on that day.
- **Hours (owner, 2026-11-14).** "in N hours" and "within N hours" ("hrs" too) are the email's send time plus exactly N hours: "within 48 hours" in an email sent at 05:52:56 PDT is due 05:52:56 PDT two days later. The result is an exact instant, so no zone or time is assumed, and DST does not change it. The item records `date_computed`. Any date, day, time or zone in the same `due_raw` makes it `multiple_dates`; "in a few hours" stays in review.

## Implementation choices

These follow from rules 1 to 5 but were not spelled out in #65. The owner accepted decisions 6 to 9 as written and changed decision 10 in the review of PR #69 (2026-11-13).

### 6. A named zone that is not understood is never replaced

Rule 1 covers a **missing** zone. A zone that is named but not understood ("JST", "BST", "London time", "local time", "Beijing" alone) still needs review (`unknown_zone`), because assuming the owner's zone would silently give a wrong time. Next to an understood zone it is `multiple_zones`. "China time" and "Beijing" alone were `no_zone` before; they are now `unknown_zone`.

### 7. A time tied to something else is not 23:59

Rule 2 covers a **missing** time. When `due_raw` has no clock time but ties the time to something else ("before class", "in lecture", "Friday morning", "close of business", "end of week", "before Friday"), 23:59 could be later than the real deadline, so the item needs review (`vague_time`).

### 8. Nothing is assumed that the email states

Rule 1 assumes a zone when the email gives none. The model sometimes copies only part of the phrase, such as "Nov 18 at 10am" out of "Nov 18 at 10am ET", and assuming the owner's zone then gives a wrong time that looks right (synthetic sample `syn-024`). An injected line can also give a bare date, such as "the real due date is Dec 31", next to a real deadline in PT (injection case `inj-031`); before this ADR it stayed in review only because it had no zone. So, after parsing:

- if the zone was assumed but the email body names any zone, understood or not, the item needs review (`zone_outside_due`);
- if the time was assumed but the evidence gives any clock time, the item needs review (`time_outside_due`).

The time check looks at the evidence only, not the whole body, because course mail often mentions unrelated times (office hours, class times) and rule 2 would rarely apply otherwise.

### 9. Where the flags are stored

`zone_assumed`, `time_assumed`, `date_computed` and `time_carried` (decision 10) are stored in the `after` data of the `item_versions` row that sets the due time: the create version, or the version an approved proposal appends. Both sides of an `item_update` proposal's payload carry them too, so the hash covers what the owner sees. No migration is needed. Readers take the flags from the newest version whose `after` has `due_at`; data saved before this ADR has no flags, which reads as nothing assumed.

### 10. A date-only change keeps the earlier time of day (owner, changed in review)

When a deadline change gives a date but no time ("Homework 3 is now due Nov 20") and the matched item's current due time has a time of day that was not itself assumed, the proposal keeps that time of day on the new date, in the item's zone. It records `time_carried` instead of `time_assumed`, and the Dashboard shows "time from earlier deadline" on the proposal and, once approved, on the item.

- The time is the item's wall-clock time in its zone, not its UTC offset: an item due 5:00 PM PDT moved to a date after the fall-back change is due 5:00 PM PST. The item's zone is read again from its stored `due_raw` (`zone_of`), with `SINAN_TIMEZONE` when it names none; "PST" and "PDT" both mean America/Los_Angeles here.
- If the item's time was itself assumed (23:59), the change keeps 23:59 and "time not stated".
- If the change gives a time, it is used.
- If the change gives no date, nothing changes: it is still a review case.
- An assumed zone on the change still follows rules 1 and 4 and decision 8. The zone stays marked assumed only when neither the item nor the change stated one.
- Nothing is carried, and the change keeps 23:59 "time not stated", when the change states a zone different from the item's, when the item's zone needs review, or when the time does not exist or happens twice on the new date (DST).
- This happens before change matching compares due times (ADR 0014), so a date-only reminder of the same day ("still due Nov 13") proposes nothing.
- The flag is in the proposal's payload, so the hash covers it. Approving copies it into the new version's `after`, like the other flags.

The rule is in `app/items/carry.py`, called from `app/items/changes.py`.

## Consequences

- Items whose only problem was a missing zone or time now get a `due_at` and stay `open`, so the digest can place them and tell when they have passed.
- An assumed zone is wrong for mail from another zone that does not say so. The Dashboard label is the owner's cue to check.
- Already-extracted items are not rewritten. The rules apply to new extractions. Re-running extraction on recent mail is a separate, deliberate step.
- The synthetic evaluation's labels follow these rules (see the PR for each changed label).
