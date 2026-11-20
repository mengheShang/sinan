import { numericLabel } from "./numeric";

// Display helpers for the digest. The API sends UTC; the digest shows times in
// the owner's zone (SINAN_TIMEZONE), with the zone name so nothing is guessed.

export function localTime(value: string | null, timeZone: string): string {
  if (!value) {
    return "–";
  }
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
  return format.format(new Date(value));
}

export function localDate(value: string): string {
  // A plain date like 2026-11-11; read it at noon UTC so no zone shifts the day
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC",
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  });
  return format.format(new Date(`${value}T12:00:00Z`));
}

// Short codes from the parser and validator (ADR 0005, ADR 0007, ADR 0016)
const REVIEW_TEXT: Record<string, string> = {
  no_zone: "No time zone given",
  unknown_zone: "A time zone Sinan does not know",
  zone_outside_due: "The email names a time zone the due time left out",
  multiple_zones: "Two different time zones",
  midnight: "“Midnight” could mean either end of the day",
  invalid_time: "The time does not exist",
  no_time: "No time given",
  vague_time: "The time is tied to a class or a part of the day",
  time_outside_due: "The quoted text gives a time the due time left out",
  multiple_times: "Two different times",
  multiple_dates: "Two different dates",
  invalid_date: "The date does not exist",
  year_unclear: "The year is unclear",
  relative_weekday: "“Next Friday” is ambiguous",
  relative_event: "The date is counted from another event",
  relative_span: "The date is a span, such as “in two weeks”",
  weekday_mismatch: "The weekday does not match the date",
  no_date: "No date given",
  zone_dst_mismatch: "The time zone does not fit the date",
  nonexistent_time: "The time is skipped by a clock change",
  ambiguous_time: "The time happens twice on a clock change",
  due_not_in_evidence: "The due time is not in the quoted text",
  no_due_raw: "A deadline without a due time",
  unmatched_update: "Changes an earlier item, but none matched: check which one",
  ambiguous_update: "Changes an earlier item, but several matched: check which one",
  proposal_expired: "A proposed new due time expired unanswered: check the due time",
  untrusted_sender:
    "From a sender Sinan cannot verify: check that it is real before relying on it",
};

export function reviewText(code: string | null, dueRaw?: string | null): string {
  if (!code) {
    return "Needs a look";
  }
  if (code === "numeric_date") {
    return numericLabel(dueRaw);
  }
  return REVIEW_TEXT[code] ?? code;
}
