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

## Follow-up decisions (2026-11-13)

Made by the owner after reviewing PR #52. Decision 7 updates the "Time zone required" rule of ADR 0005.

### 4. Day-first "May" needs its capital

After a number, lowercase "may" is the verb ("2 may be late, Nov 14 5pm PT"), so it is not read as a date and that example parses as Nov 14. Day-first "May" or "MAY" is a date ("14 May 2027"). "14 may 5pm PT" has no date and needs review (`no_date`).

The rule covers "May" only, not every day-first month name. "May" is the only month name that is also a common word after a number. Requiring a capital for all months would send lowercase dates such as "14 nov" to review for no gain. Month-first "may 14" keeps the ADR 0005 behavior and ignores case.

### 5. A month followed by a clock time has no day

"Nov 11:59pm PT" used to parse as Nov 11 at 11:59pm. It now needs review (`no_date`), because the number is a clock time, not a day. Accepted.

### 6. Forms left in review

"close of business" / "COB", "the 14th of November", and "China time" or "Beijing" alone stay unrecognized and need review. Accepted.

### 7. Strict GMT/UTC offsets

Before this decision, "5pm GMT+8" ignored the `+8` and parsed as plain UTC, which silently gave a wrong time.

- `GMT` or `UTC` **immediately** followed by `+` or `-` and an hour is a fixed offset: `GMT+8`, `UTC-5`, `UTC+05:30`, `UTC+8:00`, `GMT+0800`. The total offset runs from -12:00 to +14:00, and minutes, if present, are 00, 30 or 45.
- The offset keeps its literal sign: `GMT+8` is UTC+08:00. It is never mapped to `Etc/GMT+8`, whose sign is inverted (UTC-8). A fixed offset has no DST, so the standard and daylight checks do not apply.
- Any other offset-like text needs review with reason `bad_offset`: a space between the zone and the sign (`GMT +8`), an hour out of range (`UTC+15`, `UTC-13`), odd minutes (`GMT+8:20`), or an offset after a regional zone (`PT+1`, `EST-5`).
- A bare `GMT` or `UTC` still means UTC. An offset next to a different zone ("GMT+8 PT") is `multiple_zones`.
- The offset text is removed before times and dates are read, so the "8:00" in `UTC+8:00` is not a time.

## Rules kept from ADR 0005

Numeric dates, a missing time zone, "midnight", weekday conflicts, two dates, times or zones, and DST gaps or overlaps still need review. Standard or daylight names must still fit the date, so "Nov 1 EOD PDT" needs review because 23:59 that night is PST.

## Consequences

- To read day-first dates safely, a number is not a day when it is a clock hour or minute: the day in "Nov 11:59pm" or "Nov 5 pm" is no longer read as Nov 11 or Nov 5 (decision 5), and the "59" in "23:59 November 14" is not a day. This fixes "14 Nov 11:59pm" being read as two dates.
- Day-first dates are matched on text that keeps its case, so decision 4 can tell "May" from "may". All other rules still match lowercased text.
- Whitespace runs in `due_raw` collapse to one space before parsing, so "Beijing  time" still matches.
