# ADR 0023: Match new items and unparsed changes against existing items

- Status: Accepted (2026-11-26)
- Date: 2026-11-26
- Decider: Menghe
- Amends: ADR 0014 (deadline-change matching), ADR 0019 (the model picks a target)

## Context

Using the live home page (ADR 0022) on real mail showed the same task listed two or three times. There were two causes.

1. **A repeated task is saved again.** The model marks each extracted item as a `create` or an `update`. Only an `update` is matched to existing items (ADR 0014); a `create` is always saved as a new item. A reminder, a forum digest quoting the instructor, or a second notice from the school is often marked `create`, so it becomes a second item. In one case two items had the same course, title and due time, word for word.
2. **An unparsed change is saved again (Issue #119).** An `update` is matched only when its due time parsed. An instructor moved a lab to "Thursday, 12/3/2026", which could not be parsed at the time. The change was saved as a new item that needed review, and the old item kept its old due time.

The owner decided on 2026-11-25 (2) and 2026-11-26 (1) that both should reach the existing item instead.

## Decisions

### 1. A new item is matched first

A `create` deadline or to-do goes through the same deterministic matcher as an `update` (ADR 0014): equal course, the same numbers in the title, and half the title words shared, against active items from other emails.

- **No match, or several:** saved as a new item, as today.
- **One match:** no new item is saved. What happens depends on the due times:
  - The new item has no due time, or the same due time as the match: it is a **repeat**. Nothing is written except the cycle's count, as for an unchanged `update` (ADR 0014).
  - A different due time: a **pending item_update proposal** on the match, exactly as if the model had said `update`. The owner approves or rejects it on the home page.
  - The match has no due time and the new item has one: also a proposal, which gives the match its due time.
- The stale rule holds: an older email never changes an item whose due time came from a newer one (ADR 0014).

### 2. An unparsed change marks its target (Issue #119)

An `update` whose due time does not parse is matched like a parsed one, deterministic matcher first, then the link picker (ADR 0019).

- **One target:** no new item and no proposal. The target gets a version with actor `extraction`, the new email as message and evidence, status `needs_review` and review code `update_unparsed`. Its due time stays as it is. The owner sets the new one with Edit due (ADR 0022, decision 3).
- **No target, or several:** a new item that needs review, as today.
- The stale rule holds.

### 3. What stays the same

- Notices are never matched (ADR 0017), and an untrusted sender's items are never matched or proposed (ADR 0010).
- The model still only classifies; deterministic code decides every match. A proposal still needs the owner's approval before any due time changes.

### 4. The same title needs no model; a similar one asks the model

The deterministic matcher only finds candidates. Then:

- One candidate with the same title words, after the course is removed ("PA5" and "pa5"), is the match, with no model call.
- Otherwise, when there are candidates (similar titles, or several), the model is asked once whether the new item is one of them, with a separate, versioned prompt (`repeat-v1`). Like the link step of ADR 0019, it sees the fenced email and a numbered list built by deterministic code, returns only a number or null, and its answer is checked against the list. The prompt says that a draft and its final version, a part of an assignment, or a quiz and its retake are different tasks, and that null is better than a wrong pick.
- No pick, a refused reply, a failed call, or no model in live mode without an API key: the item is saved as new. Nothing is lost when the model is unsure.

The first draft of this decision (2026-11-26) used the deterministic matcher alone. A test showed that "Problem set 4 draft" would then count as a repeat of "Problem set 4" with the same due time and be dropped, so the owner moved similar titles to the model the same day. Items with no similar title are never sent to the model, so most new items cost no extra call.

An item whose due phrase did not parse and that matches one existing item is flagged like an unparsed change (decision 2).

### 5. Rejecting a proposal that came from a create saves the new item

A wrong match ("Quiz 6 retake" against "Quiz 6") must not lose a task. A proposal made from a `create` records that in its payload, together with the new item as it would have been saved. Rejecting it saves that item, as if no match had been found. The stored payload is covered by `payload_hash`, so the item saved is exactly the one that was proposed. Approving it works like any other proposal. This touches `app/actions/` and needs line-by-line review. Decided by the owner on 2026-11-26.

## Consequences

- Reminders, forum digests and repeated notices no longer add items. A repeat that moves the date becomes a proposal, so a wrong match is never applied without the owner.
- A change the parser cannot read lands on the item it changes, so the owner fixes one item instead of dismissing a duplicate.
- The evaluation's change accuracy measures the matcher; creates that now match change the item counts, so the regression gate's baseline may need an update, with every changed case listed.
- Items already saved are not merged; existing duplicates are dismissed by hand.
