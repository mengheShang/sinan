// The proposal link badge (ADR 0019). Run with `npm test` (node:test, no bundler).
import assert from "node:assert/strict";
import { test } from "node:test";

import { MODEL_LINK_BADGE, linkBadge } from "./link.ts";

test("a proposal linked by the model gets the badge", () => {
  assert.equal(linkBadge("llm"), "Linked by the model, check it is the right item");
  assert.equal(linkBadge("llm"), MODEL_LINK_BADGE);
});

test("a proposal linked by the matcher gets no badge", () => {
  assert.equal(linkBadge("deterministic"), null);
});

test("a proposal without a link reads as deterministic", () => {
  assert.equal(linkBadge(undefined), null);
});
