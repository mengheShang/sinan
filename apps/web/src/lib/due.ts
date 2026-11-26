// Helpers for the due form (ADR 0022, decision 3). Pure, so they run under
// `node --test` without a bundler.

export type LocalParts = { date: string; time: string };

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TIME = /^\d{2}:\d{2}$/;

// A UTC time as the owner's local date and time, for prefilling the form:
// { date: "2026-12-01", time: "17:00" }. Empty strings when there is none.
export function localParts(value: string | null, timeZone: string): LocalParts {
  if (!value) {
    return { date: "", time: "" };
  }
  const format = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  const parts = format.formatToParts(new Date(value));
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  const date = `${get("year")}-${get("month")}-${get("day")}`;
  const time = `${get("hour")}:${get("minute")}`;
  return { date, time };
}

// The request body for POST /items/{id}/due, or null when the input is not
// a date (and an optional hour and minute) as the form sends them.
export type DueBody = { date: string; time?: string; suggestion_id?: string };

// `suggestion` is the model's suggestion the form was prefilled from, or "".
export function dueBody(date: string, time: string, suggestion = ""): DueBody | null {
  if (!DATE.test(date)) {
    return null;
  }
  if (suggestion !== "" && !UUID.test(suggestion)) {
    return null;
  }
  const body: DueBody = { date };
  if (time !== "") {
    if (!TIME.test(time)) {
      return null;
    }
    body.time = time;
  }
  if (suggestion !== "") {
    body.suggestion_id = suggestion;
  }
  return body;
}

// What a due form or Confirm button shows after its action
export type DueState = { message: string };
