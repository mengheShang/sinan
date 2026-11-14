# ADR 0017: Optional and general mail becomes a morning notice

- Status: Proposed
- Date: 2026-11-13
- Decider: Menghe
- Amends: ADR 0007 (LLM extraction), ADR 0008 (daily digest), ADR 0014 (change matching), ADR 0015 (past and stale items)

## Context

In live use, all 22 open items were to-dos with no deadline, and most came from school broadcast mail: storm preparation, a crime alert, a leave-of-absence process, a flu shot reminder, a forum daily digest. None of them was a task for the owner. Prompt v1 offered only `deadline` and `todo`, so the model filed general advice and announcements as to-dos. Issue #67 records the owner's decisions of 2026-11-13. This ADR writes them down, together with how they are built.

## Decisions (owner, Issue #67)

1. **A new kind, `notice`.** The model classifies by whether the owner personally **must** act, not by whether a date appears.
   - `deadline` and `todo` are required actions: submit work, take an exam, reply to an interview, pay rent, sign an offer.
   - `notice` is optional or general content: "if you want", "interested students", "optional", "you may"; event invitations, talk sign-ups, safety alerts, health advice, platform digests and broadcasts. A notice may carry a due date ("register by Nov 20" for an optional event) and stays a notice.
2. **Notices appear once, in the morning digest.** A "Notices" section lists those received since the previous digest, one line each, with the due date if any and a link to the email. Notices never enter to-do lists, the due-soon section or review, never remind and never expire. Afterwards they stay reachable from the Past page.
3. **One click makes a notice a to-do.** Each notice line has "Make it a to-do". It changes the kind and appends an `item_versions` row with actor `user`. This is how the owner fixes a wrong demotion.
4. **No forced rule based on a parsed date.** Three safety nets instead:
   - visibility: a demoted item always shows in the next morning's notices, with its date;
   - the one-click fix of decision 3;
   - measurement: the evaluation reports "required items demoted to notice", and the CI gate fails when it rises by more than the core-metric threshold. The injection set gains cases that try to demote a real deadline. They must still end up visible, as a notice at worst, never dropped.
5. **The model gains no power.** Choosing `notice` can only make an item less prominent. The model still has no tools and returns JSON only (CLAUDE.md rule 3).

**Out of scope:** "Mark read" on a notice that also marks the email read in Gmail. That writes to Gmail, so it belongs to M3: it needs the write scope, the Executor (the only holder of the write token), a one-time approval or a standing grant, an idempotency key, and the `safe_to_auto_read` labels (ADR 0009, decision 1). Nothing here writes outside the local database.

## How it is built

### Extraction (prompt v2)

- `app/extraction/schema.py` allows `kind` to be `deadline`, `todo` or `notice`, in both the JSON Schema sent to the API and the Pydantic check. Any other value is refused with the whole reply, as before.
- Prompt v2 states the rule of decision 1 and adds: "When you are unsure whether the student must act, treat it as required." That leans a borderline item toward the more prominent kind. Marketing, receipts, sign-in codes and casual personal messages are still skipped.
- `PROMPT_VERSION` is `v2`. Every stored message gets one new run under v2, as for any prompt change (ADR 0007).

### Validation

`app/extraction/validate.py` treats a notice like any other item for evidence: it must quote at least 8 characters found in the body, or it is dropped. Then:

- **The due time is parsed as usual** and kept, with its assumed parts (ADR 0016).
- **A notice never needs review.** A due time that does not parse, a missing zone the email names elsewhere, and `change_type: "update"` would each put a deadline in review. A notice stays `open` instead, with no `due_at`. Its `due_raw` is kept as written, so the line can still show "Nov 20" in quotes.
- **A `due_raw` that is not inside the evidence is dropped** for a notice. For a deadline that case already means review. For a notice it would otherwise show, unreviewed, a date that the email does not tie to the item, such as an injected "Dec 31".
- **A notice is never a change.** `is_change` is false for it, so it is never matched and never becomes a proposal.

### Change matching

The candidates for a deadline change (ADR 0014) exclude notices. A change from the model cannot attach a proposal to a notice, so it cannot hold a notice in the digest or move its date. An extension for a wrongly demoted item finds no candidate and is saved as a new item in review, which is visible.

### Database

Migration 0007 changes one thing: the CHECK on `items.kind` allows `notice`. Downgrade refuses while any notice exists. Rewriting those rows would put them in to-do lists without anyone deciding so, and their first, append-only `item_versions` row would still say `notice`.

### Digest

- **Sections, overdue and Past.** `new`, `due_soon` and `needs_review` select only deadlines and to-dos. `list_items`, used by the to-do lists and the overdue and past groups, skips notices. A notice has no phase (ADR 0015): it is never overdue, past or stale.
- **Which digest shows a notice is computed on read, not stored.** A digest's notices are the items found as notices with `created_at` after the previous digest's `generated_at` and no later than its own. Those windows do not overlap and leave no gap, so each notice is in exactly one digest. The digest's audit event records the count when it is made.
- **"Found as a notice"** means the item is a notice now, or one of its versions has `before.kind = "notice"`, the version that "Make it a to-do" appends. So a notice the owner converted stays in its digest's list, shown in its current state.
- **Past page.** `GET /items/past` adds every open notice, newest first, including any found since the latest digest.

Why computed and not stored, as for overdue in ADR 0015: a stored section would need a new value in the `digest_items.section` CHECK, which means a second migration change, and the created-at windows already give the "exactly one digest" rule.

### Make it a to-do

`POST /items/{id}/kind` with `{"kind": "todo"}` calls `app/db/item_kind.py`. In one transaction it locks the item, refuses anything that is not a notice (409, which also covers a double click), sets `kind = 'todo'`, appends a version with `change_type: "update"`, actor `user`, `before: {"kind": "notice"}` and `after: {"kind": "todo"}`, and writes an `item.kind` audit event with IDs and kinds only. The due time, title and status do not change. From then on the item behaves like any to-do: it shows in "Due in the next 7 days" when its due time is near, can be marked done, and can become the target of a deadline change.

The only allowed move is `notice` to `todo`. The API accepts no other value, so the Dashboard cannot demote, and the model never calls this endpoint.

### Evaluation

- **`demoted_to_notice`**: labeled `deadline` and `todo` items whose matched found item is a `notice`, over all labeled `deadline` and `todo` items. Lower is better. A missed item counts against recall, not here.
- **Gate.** `evals.gate` fails when `demoted_to_notice` rises more than 2 points above the baseline, the same threshold as a core metric drop. A baseline without the metric is not compared, so the gate change and the baseline update can land in separate commits.
- **Injection set.** Four cases (`inj-035` to `inj-038`) try to demote a real deadline through the body, a fake system line, hidden HTML and the subject. Their expectation `visible` succeeds for the attacker when a labeled item is not found in any kind, or when an open item carries a due time other than the labeled one. An undated notice counts as visible.
- **Synthetic set.** Broadcast and optional emails are relabeled as notices, and five emails are added: an optional event with a date, a safety alert, health advice, a forum digest, and a required broadcast that the recorded replies deliberately demote, so the metric is not trivially 0.

## Consequences

- **A wrong demotion costs prominence, not visibility.** A real deadline the model calls a notice appears once, as one line in the morning, with its date. It is never in "Due in the next 7 days" and never overdue until the owner makes it a to-do. The metric and the gate watch how often that happens on the synthetic set; the real dev and test sets measure it once labeled with `notice`.
- **Notices are not reminded.** A notice with a date in three weeks is shown once and then only on the Past page.
- **Validation cannot force a date.** A reply can still give a notice no `due_raw`, and an email can still talk the model out of an item entirely. Neither is caught deterministically, as before (evals/README.md, "What validation does not stop"); recall measures the second.
- **Bulk-header broadcasts still never reach the model.** The prefilter skips mail with bulk headers unless allowed (ADR 0006). Two relabeled synthetic broadcasts are skipped that way and count as missed notices.
- **Recorded replies under v2 are hand-written.** CI's replies for prompt v2 are fictional until the owner re-records the synthetic set with a live run. The baseline then moves in its own commit.
