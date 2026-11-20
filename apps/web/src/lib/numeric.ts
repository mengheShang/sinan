// The review label for a numeric date (ADR 0022, decision 5). It names this
// item's readings, worked out from due_raw, instead of a fixed example.
// Pure, so it runs under `node --test` without a bundler.

export const NUMERIC_FALLBACK = "Numeric date: the month and day could be either way round";

const NUMERIC = /\b(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?\b/g;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
// A leap year, so Feb 29 counts when no year is written
const ANY_YEAR = 2024;

function fullYear(written: string | undefined): number | null {
  if (!written) {
    return ANY_YEAR;
  }
  if (written.length === 2) {
    return 2000 + Number(written);
  }
  if (written.length === 4) {
    return Number(written);
  }
  return null;
}

function isDate(year: number, month: number, day: number): boolean {
  if (month < 1 || month > 12 || day < 1) {
    return false;
  }
  // Day 0 of the next month is the last day of this one
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return day <= last;
}

function name(month: number, day: number): string {
  return `${MONTHS[month - 1]} ${day}`;
}

// The readings of the one numeric date in due_raw that are real dates,
// month/day first, without repeats.
export function readings(dueRaw: string): string[] | null {
  const matches = [...dueRaw.matchAll(NUMERIC)];
  if (matches.length !== 1) {
    return null;
  }
  const [, firstText, secondText, yearText] = matches[0];
  const year = fullYear(yearText);
  if (year === null) {
    return null;
  }
  const first = Number(firstText);
  const second = Number(secondText);
  const found: string[] = [];
  if (isDate(year, first, second)) {
    found.push(name(first, second));
  }
  if (first !== second && isDate(year, second, first)) {
    found.push(name(second, first));
  }
  return found;
}

export function numericLabel(dueRaw: string | null | undefined): string {
  if (!dueRaw) {
    return NUMERIC_FALLBACK;
  }
  const found = readings(dueRaw);
  if (!found || found.length === 0) {
    return NUMERIC_FALLBACK;
  }
  const written = dueRaw.match(NUMERIC)?.[0] ?? "";
  // Drop the year, so the label reads "12/1 could be ..."
  const short = written.split("/").slice(0, 2).join("/");
  if (found.length === 2) {
    return `Numeric date: ${short} could be ${found[0]} or ${found[1]}`;
  }
  return `Numeric date: ${short} reads as ${found[0]}, check it`;
}
