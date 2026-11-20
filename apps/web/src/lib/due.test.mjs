// The due form helpers (ADR 0022, decision 3). Run with `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { dueBody, localParts } from "./due.ts";

const LA = "America/Los_Angeles";

test("a UTC time becomes the local date and time", () => {
  assert.deepEqual(localParts("2026-12-02T01:00:00Z", LA), { date: "2026-12-01", time: "17:00" });
  assert.deepEqual(localParts("2026-12-02T07:59:00Z", LA), { date: "2026-12-01", time: "23:59" });
});

test("midnight reads as 00:00, not 24:00", () => {
  assert.deepEqual(localParts("2026-12-01T08:00:00Z", LA), { date: "2026-12-01", time: "00:00" });
});

test("no due time prefills nothing", () => {
  assert.deepEqual(localParts(null, LA), { date: "", time: "" });
});

test("the body sends the time only when there is one", () => {
  assert.deepEqual(dueBody("2026-12-01", "17:00"), { date: "2026-12-01", time: "17:00" });
  assert.deepEqual(dueBody("2026-12-01", ""), { date: "2026-12-01" });
});

test("anything else is refused before it reaches the API", () => {
  assert.equal(dueBody("", ""), null);
  assert.equal(dueBody("Dec 1", ""), null);
  assert.equal(dueBody("2026-12-01", "5pm"), null);
});
