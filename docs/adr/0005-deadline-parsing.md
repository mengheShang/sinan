# ADR 0005: Explicit deadline parsing rules

- Status: Accepted
- Date: 2026-11-10
- Decider: Menghe
- Amended by: ADR 0022 (a weekday can settle a numeric date)

## Context

Issue #12 turns an explicit `due_raw` such as "Nov 14 11:59pm PT" into `due_at` in deterministic code (ADR 0002). CLAUDE.md says to mark an item `needs_review` when the date, time zone or year is missing, never to guess. Course email almost never states a year, though, and numeric dates like 11/12 have two readings. Both rules needed a decision.

## Decisions

### 1. A missing year is inferred from the email date

The parser picks the one year (the email's year, the year before or the year after) that puts the date between 30 days before and 335 days after the email date. Some examples:

- A December email saying "Jan 5" means January of next year.
- A January email saying "Dec 28" means the December just past.
- "Sep 1" in a November email means next September.

This is a fixed, tested rule rather than a guess. Without it, almost every deadline would be `needs_review` and the digest could not show what is due soon. CLAUDE.md now names this as the one exception.

### 2. Numeric dates always need review

"11/14" and "11/12/2026" are never parsed. USC mail is usually US-format, but mail from elsewhere is not, and the parser cannot tell which. Month names ("Nov 14", "November 14th") and ISO dates ("2026-11-14") are unambiguous and are parsed.

Amended by ADR 0022 (decision 5): a numeric date next to a weekday is parsed when exactly one of its readings, month/day or day/month, is a real date on that weekday. "Tue, 12/1/2026" is December 1, because January 12, 2026 is a Monday. Without a weekday, or when both readings or neither fit it, the date still needs review. A 2-digit year is read as 20xx ("12/1/26" is 2026), as the owner decided on 2026-11-20; a 3-digit year is never accepted.

## Other rules (no decision needed, recorded for review)

- **Time zone required.** The parser understands:
  - Pacific, Mountain, Central and Eastern, in the general form (PT) or the standard or daylight form (PST, PDT).
  - UTC and GMT.
  - AoE.

  A standard or daylight name must fit the date, so "PST" on a summer date needs review.
- **Time required.** "noon" is 12:00, and "midnight" needs review, because it could mean either end of the day.
- **Weekdays.**
  - A weekday alone means the first such day on or after the email date, in the deadline's time zone.
  - "next Friday" needs review.
  - A weekday next to a date must agree with it.
- **today, tonight and tomorrow** count from the email date in the deadline's time zone.
- **DST.** A wall time that does not exist on a spring-forward night, or happens twice on a fall-back night, needs review unless a standard or daylight name settles it.
- **Conflicts.** Two dates, two times or two zones need review.

## Consequences

- Every result is either a UTC `due_at` or a short reason code, never both. #13 stores the reason with the item.
- More forms, such as "14 November", "end of day" or other time zones, are M2 work. They return `needs_review` today, which is the safe default.
- `tzdata` is a runtime dependency, so DST rules do not depend on the operating system's time zone files.
