# ADR 0008: Daily digest

- Status: Accepted
- Date: 2026-11-11
- Decider: Menghe
- Amended by: ADR 0015 (past and stale items leave the digest)

## Context

Issue #14 shows a daily digest in the Dashboard: new items, items due in the next 7 days, and items needing review. The Worker makes it each morning, and it is never sent anywhere. Four choices needed a decision.

## Decisions

### 1. A `digests` table and a `digest_items` join table

Migration 0003 adds two tables:

- **`digests`**: one row per local day (`digest_date` is unique), with `generated_at` and `since`. `since` is the previous digest's `generated_at`.
- **`digest_items`**: `(digest_id, section, item_id)`.
  - `section` is checked to be `new`, `due_soon` or `needs_review`.
  - `digest_id` cascades on delete.
  - `item_id` restricts deletes, like `item_versions` does.

The join table copies no content, so the Dashboard joins to `items` and always shows an item's current state.

A snapshot in `audit_events` would have avoided a migration. A real table gives foreign keys and a simple query for past digests.

### 2. 7:00 in America/Los_Angeles

`SINAN_TIMEZONE` (default `America/Los_Angeles`) sets which local day a digest belongs to and the zone the digest shows times in. `SINAN_DIGEST_HOUR` (default 7) sets when the Worker makes it. Storage stays UTC.

### 3. A missed morning is caught up

The Worker checks at the end of every cycle, after sync and extraction. If today's digest does not exist and the local hour is at or past `SINAN_DIGEST_HOUR`, it makes one. A laptop that was asleep at 7:00 gets its digest on the first cycle after it wakes. Demo mode ignores the hour, so the demo shows a digest at once. It still waits until the demo mailbox has produced items, because a digest made before the account is connected would stay empty all day.

### 4. Times in the local zone, and no item actions yet

The digest shows times like "Fri, Nov 13, 11:59 PM PST", using the zone the API reports. The accounts page keeps UTC. Marking items done or dismissing them is a separate Issue, so this PR stays read-only.

## Other rules (no decision needed, recorded for review)

- **Sections.** Only items that are `open` or `needs_review` are included.
  - `new`: created after `since`. The first digest counts every active item as new.
  - `due_soon`: `due_at` within 7 days of `generated_at`.
  - `needs_review`: status `needs_review`.

  An item can be in more than one section.
- **Order.** Soonest due first. Items without a due time come last.
- **Gmail link.** In live mode it is `https://mail.google.com/mail/?authuser=<mailbox>#all/<message id>`. In demo mode there is none, since the mail is fake.
- **API.**
  - `GET /digest` returns the latest digest, or `null`.
  - `GET /items?status=...` lists items; by default it returns `open` and `needs_review`.

  Both require the login, and both only read.
- **Audit.** Each digest adds an audit event with its date and section counts, never content.

## Consequences

- The digest page joins items to their first version for the evidence and source message. When M2 adds item matching and later versions, it should show the latest evidence too.
- Done and dismiss will append `item_versions` rows with actor `user`. A digest made after that drops the closed items, and past digests still list them.
