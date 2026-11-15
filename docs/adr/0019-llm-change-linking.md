# ADR 0019: The model picks a deadline change's target when matching fails

- Status: Accepted (2026-11-15)
- Date: 2026-11-15
- Decider: Menghe
- Amends: ADR 0014 (deadline-change matching), decision 1

## Context

ADR 0014 links a deadline change (an extension, a new exam time) to the item it changes only when the deterministic matcher finds exactly one candidate: an equal course, the same numbers in the title, and half the title words shared. On the synthetic set with live Haiku 5.5 replies (prompt v4, PR #82), the model marked 7 of the 8 labeled changes as updates, but only 4 were linked. The other 3 failed on wording ("Midterm exam covering chapters 1 to 6" against "Midterm exam moved to Monday"). An unlinked change becomes a second item instead of a proposal to update the first one.

Prompt v5 (PR #84) asks for the task's name only as the title, so the matcher succeeds more often. Wording can still differ ("supply curves problem set" against "supply curves homework"), and two similar items ("Lab report draft", "Final lab report") give several candidates.

Issue #83 records the owner's choice of 2026-11-15: Option A, hybrid matching. On PR #85 (2026-11-15) the owner accepted the window and cap, sending "several" to the model, the model and prompt version, and a new ADR as recommended, and decided that the Dashboard badge and the link in the payload belong in the same change (decisions 5 and 8). This ADR writes down the rule and how it is built. Issue #54 (cross-checking due times with the model) follows the same pattern: the model suggests, deterministic code validates, and the owner confirms.

## Decisions

### 1. The deterministic matcher runs first

- **Exactly one candidate:** nothing changes from ADR 0014. The model is not asked.
- **None or several:** one extra model call (the link step) picks the item the change refers to from a list, or answers that none fits.
- Only a change that ADR 0014 would match is asked: an update with a parsed due time, never a notice.

### 2. The list comes from deterministic code, never from the model

`app.items.matching.choices` builds it from the same pool as the matcher (active items, not notices, not first found in the same email):

- **Course:** equal after normalizing, or both without a course.
- **Window:** the item's newest email (the one behind its created or last approved time) is at most **60 days** older than the change email, **or** its due time is at or after the change email's date. An exam announced at the start of term and moved near the end still qualifies, because it is still ahead.
- **Order and cap:** newest email first, at most **15** items.
- **Content:** each item shows only its number, course, title and current due time (UTC). Never another email's body or evidence.

When the list is empty, the model is not asked.

### 3. The link prompt is versioned and hashed on its own

- `app/extraction/link_prompt.py`, version `link-v1`, with the SHA-256 of the template in `LINK_PROMPT_HASH`.
- The email is fenced as in the extraction prompt (`prepare`) and is untrusted data. The change (course, title, `due_raw`, evidence) and the list are given as one JSON object per line inside `<change>` and `<choices>` tags. `<`, `>` and `&` are JSON-escaped, so a title cannot close or open a tag. Titles were written by the model from other emails, so they are fenced too.
- The prompt asks for null when no item is clearly the same task, or when more than one could be: a wrong pick is worse than none.
- The same model as extraction (`SINAN_LLM_MODEL`, Haiku 5.5 by default), through the `LinkPicker` provider interface with no tools. A structured output schema allows only `{"target": <integer> | null}`, with a 100-token limit. The demo uses `FakeLinkPicker`, which always answers null.

### 4. The reply is validated, and anything else means no link

`app/extraction/link_reply.py` accepts exactly `{"target": <integer>}` or `{"target": null}`. Bad JSON, another shape, an extra field, a boolean, text or a float instead of an integer, and a number outside 1 to the list's length are refused. A refused reply, a null, and a call that fails for this change alone (a refusal, a cut-off reply) all leave the change unlinked, saved as ADR 0014 says: `unmatched_update`, or `ambiguous_update` when the matcher had found several. A service error (network, rate limit) ends the cycle like an extraction error, so the email is read again later.

### 5. A pick is never applied directly

A chosen item goes down the same path as a deterministic match (ADR 0014, decisions 2 to 5): the stale and unchanged checks, superseding, the carried time of day, and a **pending** `item_update` proposal. The owner approves every change, and approval recomputes `payload_hash` as before. A wrong pick costs one rejected proposal, never a silent edit. The model never creates or modifies grants, and the link step writes nothing outside the proposal path.

The `item_update` payload records how the item was chosen, as `link: "deterministic"` or `link: "llm"` (owner, PR #85). The link is covered by `payload_hash`, so the owner approves the link path together with the times and the evidence, and approval still checks the hash the Dashboard showed. A payload from before this ADR has no `link` and reads as `deterministic` (`app.proposals.link_of`); its stored hash is unchanged, so it approves as before. No migration is needed: `payload` is a JSON object.

### 6. Audit, with IDs and counts only

- `proposal.created` adds `link` (`deterministic` or `llm`) and `candidates`: 1 for a deterministic match, else the length of the list the model saw. An `llm` link also records `matched` (the matcher's candidates, 0 or several), `link_model`, `link_prompt_version` and `link_prompt_hash`.
- The extraction cycle's event adds `link_prompt_version`, `link_asked`, `link_picked`, `link_refused`, `link_failed` and `llm_proposals`.

No title, course, email text or reply text is recorded.

### 8. The Dashboard marks a proposal the model linked (owner, PR #85)

`GET /proposals` returns `link` for each proposal, read from the payload. A proposal with `link: "llm"` shows the badge **"Linked by the model, check it is the right item"** next to the target item's title, in a colour distinct from the "New due time proposed" flag. A deterministic link shows no badge.

### 7. Evaluation

- The runner links changes twice over the same extraction replies: by the matcher alone and with the link step. The report's `linking` block gives `change_accuracy_without_llm`, `change_accuracy_with_llm`, `spurious_links_*` (found items linked although their label is no change), and the link step's counts. The headline `change_accuracy`, which the gate checks, is the Worker's behavior: with the link step.
- Link replies are recorded like extraction replies, in `evals/<set>.links.json`, keyed by the change's found uid, for prompt `link-v1`. CI needs no key; a missing recorded reply fails the run.
- The injection set has cases that try to steer the link. Each ends with no link or a link inside the deterministic list, which is a pending proposal. A link outside the list counts as a successful injection.

## Consequences

- At most one extra model call per unlinked change, and none for changes the matcher places, for mail with no candidates, or for notices.
- The owner sees which proposals the model linked, on the proposal itself and in the audit, and can reject a wrong pick before anything changes.
- The pick depends on titles the model wrote earlier. A poor title on the original item can still leave a change unlinked; it then stays a review item, as before.
- A change can still be linked to the wrong item of the same course. The owner rejects it, and the next change can be linked again.
