// The numeric date label (ADR 0022, decision 5). Run with `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { NUMERIC_FALLBACK, numericLabel, readings } from "./numeric.ts";

test("both readings are named", () => {
  assert.equal(numericLabel("Tue, 12/1/2026"), "Numeric date: 12/1 could be Dec 1 or Jan 12");
  assert.equal(numericLabel("11/12 5pm PT"), "Numeric date: 11/12 could be Nov 12 or Dec 11");
});

test("a reading that is not a date is left out", () => {
  assert.equal(numericLabel("13/11/2026"), "Numeric date: 13/11 reads as Nov 13, check it");
  assert.deepEqual(readings("2/30/2026"), []);
});

test("a date that reads the same both ways is named once", () => {
  assert.equal(numericLabel("Wed 5/5/2026"), "Numeric date: 5/5 reads as May 5, check it");
});

test("Feb 29 counts only in a leap year, or with no year", () => {
  assert.deepEqual(readings("2/29/2027"), []);
  assert.deepEqual(readings("2/29/28"), ["Feb 29"]);
  assert.deepEqual(readings("29/2"), ["Feb 29"]);
});

test("anything else falls back to the general text", () => {
  assert.equal(numericLabel(null), NUMERIC_FALLBACK);
  assert.equal(numericLabel(""), NUMERIC_FALLBACK);
  assert.equal(numericLabel("12/1 or 12/2"), NUMERIC_FALLBACK);
  assert.equal(numericLabel("13/13/2026"), NUMERIC_FALLBACK);
  assert.equal(numericLabel("12/1/202"), NUMERIC_FALLBACK);
});
