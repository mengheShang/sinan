# ADR 0020: Group morning notices by category

- Status: Proposed
- Date: 2026-11-16
- Decider: Menghe
- Amends: ADR 0017 (notices)

## Context

The morning Notices section shows one line per notice, which in live use is many lines a day. Issue #76 records the owner's decisions of 2026-11-14: group notices by a category the model picks during extraction, with no extra model call. Prompt v4 already gives one notice per email with a summary title.

## Decisions

1. **Category list.** Prompt v6 asks for a `category` on each notice: `course`, `safety`, `event`, `health`, `career`, `orders` or `other`, each with a one-line meaning. v5's other rules are unchanged. Required items get none.
2. **Validation.** The JSON Schema lists the values, but the Pydantic check accepts any string up to 50 characters or none, and `app/items/category.py` maps anything unknown to `other`. A bad category never refuses a reply, and a deadline or to-do drops whatever category the model gave.
3. **Storage.** The category is stored in the create version's `item_versions.after`, as the ADR 0016 flags are. No migration. Readers take it from the newest version that has one; data without it reads as `other`. "Make it a to-do" keeps the category.
4. **Display.** `GET /digest` and `GET /items/past` return `notice_groups` (one entry per category that has notices, in the fixed order above) instead of a flat `notices` list. The Dashboard shows one collapsible row per group with its count and titles. Opening it shows the notice lines with "Make it a to-do" and Dismiss as before, and a Dismiss all that sends only that group's shown IDs (ADR 0017, decision 7). The "N new notices since this morning" line stays.
5. **Not scored in M2.** The category is not labeled or measured. Tests check it.

The model gains no power: a category only picks the group a notice is shown in.

## Consequences

- A wrong category puts a notice in the wrong group. It is still shown, with the same actions.
- Notices saved before v6 all show under "Other".
- The section no longer has one Dismiss all for every notice; the owner clicks it once per group.
