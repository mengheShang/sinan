# ADR 0002: M1 scope decisions

- Status: Accepted
- Date: 2026-11-05
- Decider: Menghe

## Context

M1 is the first milestone that handles real mail. Breaking it into Issues (#5 to #14) raised four questions that the technical design did not settle, or settled inconsistently.

## Decisions

### 1. Create only the M1 tables now

M1 creates 8 of the 11 tables in the design: `accounts`, `credentials`, `sync_states`, `messages`, `extraction_runs`, `items`, `item_versions` and `audit_events`. The tables `proposals`, `grants` and `actions` are created in M3.

Their constraints depend on the approval and execution design, which is M3 work. Creating them now would likely mean another migration later, and every migration needs line-by-line review.

### 2. Sync fallback starts from the last successful sync

When the Gmail `historyId` cursor is too old (HTTP 404), the Worker re-syncs from the last successful sync time, capped at 30 days. This replaces the 7-day fallback in the technical design.

Testing-mode tokens expire every 7 days, and the machine may be off for days. A fixed 7-day window could silently miss mail after a longer gap. Duplicates are not a risk, because `messages` is unique on `(account_id, provider_msg_id)`.

### 3. Basic deadline parsing moves into M1

M1 parses explicit deadline text such as "Nov 14 11:59pm PT" into `due_at` in deterministic code. Anything with a missing or ambiguous date, time zone or year becomes `needs_review`. Full parsing coverage stays in M2.

Without this, every item would be `needs_review`, and the digest could not show what is due in the next 7 days.

### 4. Dashboard login lands in M1

The API and the Dashboard require a single-user password login in M1, before real mail is stored.

Binding to `127.0.0.1` keeps other machines out but not other local programs or browser pages. Once real mail is in the database, that exposure matters.

## Consequences

- M1 has ten Issues instead of the eight in the first draft: login (#8) and deadline parsing (#12) were added.
- Four M1 Issues need the owner's line-by-line review: the migration (#5), token encryption (#7), login (#8) and OAuth (#9).
- M2 still owns the evaluation set, full time parsing and deadline-change matching.
