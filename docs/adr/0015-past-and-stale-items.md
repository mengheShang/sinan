# ADR 0015: Past and stale items leave the digest

- Status: Accepted (2026-11-13, with the amendment below)
- Date: 2026-11-13
- Decider: Menghe
- Amends: ADR 0008 (daily digest)

## Context

In live use the Dashboard kept showing a midterm from a month ago and a quiz and homework from two months ago. Under ADR 0008, `new` and `needs_review` ignore whether a due time has passed, and review items with no due time never age out. Issue #66 records four decisions by the owner (2026-11-13). This ADR writes them down, together with how they are built.

## Decisions (owner, Issue #66)

1. **Overdue, with a 2-day grace period.** An `open` or `needs_review` item whose `due_at` has passed is shown in an "Overdue" group until 2 days after its due time, then leaves the digest.
2. **Stale review items.** A `needs_review` item with no `due_at`, whose source email is more than 14 days old, moves to a "Stale" list and leaves the digest.
3. **Already past when extracted.** A new item whose `due_at` was already more than 2 days past when it was extracted does not appear in `new`.
4. **Nothing is deleted or re-statused.** These are views computed on read. The items keep their status. They stay reachable from a new "Past items" page (`/past`), where the owner can still mark them done or dismiss them.

An item with a valid pending proposal (ADR 0014) stays visible while the proposal can still be decided.

## How it is built

### Phases, computed by a pure function with a clock

`app/items/timing.py` gives each active item a phase at a time `now`:

| Phase | Rule | Shown |
| --- | --- | --- |
| `current` | due in the future, or no due time and not stale | its digest sections |
| `overdue` | `now - due_at < 2 days` | "Overdue" on the Dashboard |
| `past` | `now - due_at >= 2 days` | Past items page |
| `stale` | `needs_review`, no `due_at`, `now - source email > 14 days` | Past items page |

- **Source email time.** The source email is the one behind the item's newest version with evidence (the same email the Dashboard links to). Its `received_at` is used, or the item's `created_at` if no email is known.
- **Pending proposals.** "Valid" uses the same test as `app.proposals.validity`: pending, `expires_at` after `now`, and based on the item's current version. An item with such a proposal is never `past` or `stale`. If its due time has passed, it stays `overdue` beyond the grace period. Once the proposal is superseded, the normal rules apply. An expired one keeps holding the item; see the amendment.
- **Status.** The phase never changes `items.status` and writes nothing.

### What is stored, and what is computed

`digest_items` stays a snapshot of the digest when it was made.

- **When made**, `make_digest` keeps only items that are `current` at `generated_at`. The stored sections and the audited counts therefore match.
- **Decision 3** follows from this: an item already past when extracted is `past` at every later time, so it is never stored in `new`. No separate check is needed. A test covers it.

Overdue, past and stale are computed on read with an injected clock (`apps/api/clock.py`, `get_clock`), and never stored:

- **`GET /digest`** reads the stored sections and drops any active item that is no longer `current` at read time. It adds `overdue`, computed from all active items at read time, including items that were never in that digest. Closed items stay in their sections, so the Dashboard can still say how many were closed since the digest was made.
- **`GET /items/past`** returns `past` (most recently due first) and `stale`.

Why computed and not stored:

- **A stored digest must not change after it is made.** Overdue and past depend on the time of reading. An item due tonight at 23:59 is "due soon" in this morning's digest and overdue by tomorrow. Storing that would mean rewriting rows or waiting for the next digest.
- **No migration is needed.** A stored "overdue" section would need a new value in the `digest_items.section` check constraint.
- **Past digests still read the same.** Their rows are unchanged. Only the latest digest is shown, and always as it stands now.

## Consequences

- **Overdue is live.** The "Overdue" group can include items that the stored digest never listed. For example, an item extracted at noon that was due yesterday shows at once, not the next morning.
- **Old digests read differently over time.** The same stored digest shows fewer items when read later, because some leave for Overdue or Past. Nothing in `digests` or `digest_items` changes. The audit event still records the counts at the time the digest was made.
- **Expired proposals.** When a proposal expires unanswered, ADR 0009 moves its item to `needs_review` (`proposal_expired`). The item stays in the digest; see the amendment. The digest also counts the expired proposal in its meta line.
- **Unparsed due times.** An `open` item with no `due_at` never ages out. Only `needs_review` items go stale, as decided. #65 gives more items a `due_at`, which makes this case rarer.
- **Boundaries.** At exactly 2 days past due an item is `past`. At exactly 14 days old an item is still `current`; it goes stale only when it is more than 14 days old.

## Amendment (2026-11-13)

As first merged, an item whose proposal expired unanswered followed the normal due-time rules. If its stored (old) due time was more than 2 days past, it went to the Past page. But the stored due time is the one the proposal tried to replace. The proposed later time may still be ahead, so a live deadline could leave the digest without the owner seeing it. That breaks ADR 0009's rule that a missed deadline is worse than an extra prompt.

The owner decided that an expired proposal holds its item in the digest the way a pending one does. `app.items.timing.is_held` is true for a `needs_review` item whose review code is `proposal_expired`, as well as for a pending proposal. Such an item is never `past` or `stale`. Once its due time passes, it shows in the Overdue group until the owner acts on it. If the owner marks it done or dismisses it, the hold ends, and it stays ended if the item is later reopened, because the hold only applies while the item is in `needs_review` with that review code.

The owner accepted the other choices made in PR #68: the Overdue group is computed live, the boundaries are as stated, open items with no due time never go stale, and this ADR is accepted.
