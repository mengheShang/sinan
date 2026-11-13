# ADR 0014: Deadline-change matching and item_update proposals

- Status: Accepted
- Date: 2026-11-13
- Decider: Menghe

## Context

Issue #38 matches an email that changes a deadline to the item it changes, and proposes the update instead of saving a duplicate (ADR 0009, decisions 2 and 6; technical design, "DDL changes"). The Issue and ADR 0009 left several rules open. The owner decided them on PR #57.

This ADR replaces ADR 0007 decision 2: an update is no longer always saved as a new item with review code `unmatched_update`.

## Decisions

### 1. Matching rule

Only an item the model marks as an update, whose due time parsed, is matched. An update with an ambiguous time stays a `needs_review` item with its parser code, so a proposal never clears a due time.

- **Course:** Unicode NFKC, case folded, letters and digits only, so "CS 000", "cs000" and "CS-000" are equal. An item with no course matches only items with no course.
- **Title:** NFKC, case folded, split into runs of letters and digits (any script), minus the words of its course, written apart or together.
- **Similar titles:** both keep at least one word, their number tokens are equal ("Problem Set 4" never matches "Problem Set 5"), and they share at least half of all their words (Jaccard overlap of 0.5 or more, the evaluation's threshold).
- **Candidates:** `open` and `needs_review` items, except items first found in the same email. The kind (deadline or to-do) is not compared.
- **High confidence means exactly one candidate.** Exactly one makes a pending proposal. Several save the change as a new `needs_review` item (`ambiguous_update`); none saves it as `unmatched_update`. An exact title match among several fuzzy ones is not preferred.

The evaluation runner uses the same matcher, so change accuracy measures this rule.

### 2. What a proposal may change

An `item_update` proposal changes only the due time: `due_raw` and `due_at`. Its payload holds `before`, `after` and the evidence, all covered by `payload_hash`. A different title in the change email is not proposed.

### 3. Expiry and superseding

- A proposal expires at the earlier of `SINAN_PROPOSAL_TTL_DAYS` after creation and the **proposed (new)** due time. An extension can therefore still be approved after the old deadline has passed, until the new one.
- A proposal is superseded when its item has a newer version than `base_version`.
- When both apply, **superseded wins**: the item already changed, so the stale proposal does not flag it for review.
- When an expiry is recorded, an `open` item moves to `needs_review` with review code `proposal_expired`.

### 4. An older email never overrides a newer one

- If the item's due time came from a newer email, the older change is dropped.
- If a newer email already has a pending proposal for the item, the older change is dropped.
- A change from a newer email supersedes the item's pending proposals from older emails.
- A change that repeats the item's current time proposes nothing.

Dropped and unchanged changes save nothing; the extraction cycle's audit event counts them.

### 5. Approval

Approving recomputes `payload_hash` from the stored payload **and** compares it with the hash the Dashboard showed, and refuses either mismatch. So the owner approves exactly what they saw. In one transaction it then:

- appends an `item_versions` row with `change_type = 'update'`;
- updates the item, moving `needs_review` to `open`;
- marks the proposal `approved`, then `executed`.

The API applies it. Nothing external is written.

### 6. The `system` actor

Migration 0006 allows `system` in `item_versions.actor`, for versions that neither the owner nor extraction made. The expiry-driven move to `needs_review` uses it. The version still points at the email whose change went unanswered, with `after.review = 'proposal_expired'`. Superseding writes no version row.

Downgrading below 0006 refuses while any `system` row exists, because `item_versions` is append-only and those rows cannot be rewritten. Restore a backup taken before 0006 instead.

### 7. Validity is computed on read, recorded only by the sweep and decisions

One function, `app.proposals.validity`, decides whether a proposal is valid, expired or superseded.

- `GET /proposals` computes each proposal's current status with it and returns that status. It writes nothing.
- The Worker's sweep, which runs every cycle before the digest, records expiries and supersedes.
- Approve and reject re-check validity inside their own transaction. They record an expiry or supersede they find and refuse the decision.

The digest counts proposals with status `expired` whose expiry falls after the previous digest and no later than this one.

## Consequences

- An item can show an expired proposal as gone from the Dashboard list before the next Worker cycle flags the item for review (at most one sync interval).
- `ItemView` shows the review code and evidence of an item's newest version, so an approved item quotes the change email.
- M3 can reuse the validity function and the shown-hash check for external actions.
