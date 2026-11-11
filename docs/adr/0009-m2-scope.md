# ADR 0009: M2 scope decisions

- Status: Accepted
- Date: 2026-11-11
- Decider: Menghe

## Context

M2 ("evaluation and changes") makes the extraction pipeline measurable and adds deadline-change matching. Its exit criterion is that the test-set metrics in the technical design can be reproduced. Breaking it into Issues raised six questions the design left open.

## Decisions

### 1. Label the auto-read field now

Each labeled email records: whether it holds an item, course, title, due time, whether it changes an existing item (and which one), and whether it is safe to mark read automatically. The last field is used only in M3, but labeling it now avoids a second pass over the same mail.

### 2. Create `proposals` in M2

A detected deadline change becomes a row in `proposals`, the table the design reserves for actions awaiting a decision, instead of a temporary flag on the item. M3 reuses the table for external actions.

- M2 creates `proposals` only. `grants` and `actions` stay in M3.
- M2 has one proposal kind, `item_update`: a local change to an item's due time or title. Approving it appends an `item_versions` row with `change_type = 'update'`, in one transaction, and touches no external service. So it is applied by the API, not the Executor.
- Columns follow the design: kind, payload, `payload_hash` (SHA-256 of the canonical payload), status (`pending`, `approved`, `rejected`, `expired`, `superseded`, `executed`), source message and extraction run, expiry time, and decision time. Decision 6 covers `expired` and `superseded`. The hash is recomputed before applying, and a mismatch is refused, as for M3 actions.
- The migration needs the owner's line-by-line review.

This replaces ADR 0007 decision 2 (`unmatched_update`) once matching lands.

### 3. Label in a Dashboard page, stored in the live database with a frozen snapshot

A labeling page shows one email at a time with a form for the fields in decision 1. Labeling in the Dashboard is faster than editing files by hand.

Labels are stored in a new table in the live database (`sinan`), which lives in the Docker volume and never in the repository. Each labeled row keeps a frozen input snapshot: a copy, taken when the email is labeled, of exactly the fields the extractor sees (sender, subject, body text, email date and account role), together with the labels, the set it belongs to (`dev` or `test`) and a set version.

The evaluation reads only these snapshots, never `messages`. A later re-sync or a change to body parsing therefore cannot change the inputs, and the same snapshot with the same prompt and model gives the same metrics. Without the snapshot, a changed score could come from changed inputs rather than from the prompt or model.

A labeled snapshot is not edited in place: correcting a label writes a new row and retires the old one, so earlier evaluation runs stay explainable. The table needs a migration, which needs the owner's line-by-line review.

An export of a set to a file is optional and not part of M2. If one is made, it goes outside the working directory.

### 5. Real evaluation data stays out of Git

This keeps the existing rule (technical design, "real evaluation data never goes into Git") and CLAUDE.md rules 2 and 6 unchanged. Snapshots and labels hold real mail, mostly other people's information (instructors, classmates, recruiters), and Git history is close to impossible to clean once pushed. Labeling speed does not depend on it, because the data lives in the database.

Evaluation reports in the repository hold summary metrics and sample counts only, never an email, a subject, a sender or a per-email result.

### 6. Pending proposals expire after 7 days or at the due time, whichever comes first

- A proposal gets an expiry time when it is created: 7 days later, or the relevant due time if that is sooner. Approving a deadline change after the deadline has passed has no use. The 7 days come from `SINAN_PROPOSAL_TTL_DAYS` (default 7): long enough to cover a weekend or a busy stretch with a daily digest, short enough not to pile up.
- A proposal becomes `superseded` as soon as its target item gets a newer version, from another email or from the owner, because the state it was based on is gone. This is the same idea as recomputing `payload_hash` before applying.
- Expired and superseded proposals are kept, never deleted. The next digest shows how many expired unanswered, and the target item moves to `needs_review`, because a missed deadline is worse than an extra prompt.
- Deterministic code checks validity when a proposal is read and again before it is applied, with the same function.
- The 7 days are revisited after 14 days of real use, from how long proposals actually wait. M3 may set shorter limits per action kind.

### 4. Sample real data after the school mailbox has synced

The development set (about 100 emails) and test set (about 50) are sampled once the school account's first sync is done, from both mailboxes, so course mail is represented. The test set is used only for reported metrics. Until then, M2 work that needs no real mail goes first.

## Consequences

Proposed Issue order:

1. Synthetic sample set (about 50 fictional emails) and label format.
2. Evaluation runner: recall, precision, due-time accuracy, change-match accuracy; recorded responses in CI, summary-only reports.
3. Injection test set (at least 30 cases) as security regression tests.
4. Full due-time parsing (the ADR 0005 leftovers: "14 November", "end of day", other zones).
5. `proposals` table migration, with expiry (decision 6). Line-by-line review.
6. Deadline-change matching and `item_update` proposals, with approve and reject in the Dashboard, and expiry and superseding (decision 6).
7. Label table migration with frozen snapshots (decision 3). Line-by-line review.
8. Labeling page in the Dashboard.
9. Sampling the real development and test sets (after the school sync, which finished on 2026-11-11).
10. CI regression gate: fail on a drop over 2 points or any successful injection.
11. Model comparison on the development set (Haiku 5.5 against Sonnet 5.5), after checking each model's retention terms.

Other consequences:

- M2 has two migrations that need line-by-line review: `proposals` (Issue 5) and the label table (Issue 7).
- The evaluation runner (Issue 2) reads synthetic sets from the repository and real sets from the label table, and writes summary-only reports.
- `.env.example` gains `SINAN_PROPOSAL_TTL_DAYS=7`.
