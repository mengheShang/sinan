# ADR 0011: More deadline formats

- Status: Accepted
- Date: 2026-11-12
- Decider: Menghe

## Context

ADR 0005 left three deadline forms for M2: day-first month names ("14 November"), "end of day", and more time zones (ADR 0009, item 4). Issue #36 adds them. Two of them needed an owner decision before implementation: what "end of day" means, and which zones to add.

This ADR extends ADR 0005; it does not replace any of its rules. ADR 0010 is reserved for sender trust (#51).

## Decisions

### 1. "end of day" is 23:59 in the stated time zone

"end of day", "end of the day", "end-of-day" and "EOD" mean 23:59 in the time zone stated next to them. Without a stated zone the item still needs review (`no_zone`), as with any other time. Next to a different explicit time ("EOD 5pm") it needs review (`multiple_times`). Other "end of" phrases, such as "end of week", are not times.

### 2. China time by full name only (provisional)

"Beijing time" and "China Standard Time" mean Asia/Shanghai (UTC+8, no daylight saving time). The abbreviation `CST` keeps meaning US Central, so "Beijing time (CST)" names two zones and needs review (`multiple_zones`). "China time" or "Beijing" alone is not recognized. This is provisional and may be revisited once real mail shows how often China time appears.

### 3. Day-first month names

"14 November", "14th Nov.", and "14 Nov 2026" are parsed like "Nov 14". A missing year is inferred by the ADR 0005 rule.

## Rules kept from ADR 0005

Numeric dates, a missing time zone, "midnight", weekday conflicts, two dates, times or zones, and DST gaps or overlaps still need review. Standard or daylight names must still fit the date, so "Nov 1 EOD PDT" needs review because 23:59 that night is PST.

## Consequences

- To read day-first dates safely, a number is not a day when it is a clock hour or minute: the day in "Nov 11:59pm" or "Nov 5 pm" is no longer read as Nov 11 or Nov 5, and the "59" in "23:59 November 14" is not a day. This fixes "14 Nov 11:59pm" being read as two dates.
- A number right before "may" ("2 may be late, Nov 14") reads as a second date and needs review. This is the safe direction.
- Whitespace runs in `due_raw` collapse to one space before parsing, so "Beijing  time" still matches.
