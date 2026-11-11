# ADR 0007: LLM extraction and evidence checks

- Status: Accepted
- Date: 2026-11-10
- Decider: Menghe

## Context

Issue #13 sends prefiltered mail to the Claude API and keeps only items backed by evidence. ADR 0002 says the model returns JSON only and deterministic code decides. ADR 0005 parses `due_at`, and ADR 0006 decides which mail reaches the model. M0 item 7 chose Anthropic and left the model to M2's evaluation, starting from Claude Haiku 5.5 and Claude Sonnet 5.5. Four choices needed a decision.

## Decisions

### 1. Default model: Claude Haiku 5.5

Extraction from one email is a simple task, and Haiku 5.5 costs about a twentieth of Sonnet 5.5. `SINAN_LLM_MODEL` changes it without a code change, and M2's evaluation may move it up. Haiku 5.5 has no server-side refusal fallback, so a refusal is recorded as an error and yields no item.

### 2. An "update" becomes a new item marked `needs_review`

Matching an extension to the item it changes is M2 work. Until then, an item the model marks as an update is saved as a new item with status `needs_review` and review code `unmatched_update`. This follows the design rule that no matching candidate means review. The digest shows both, with the newer one flagged.

### 3. `due_raw` outside the evidence keeps the item for review

When the evidence is in the body but `due_raw` is not inside the evidence, the task is real but its time is unproven. The item is kept with no `due_at`, status `needs_review` and code `due_not_in_evidence`.

### 4. At most 100 messages per cycle go to the model

`SINAN_EXTRACT_PER_CYCLE` (default 100) bounds cost and rate limits, especially on the first live run, which sees up to 30 days of mail. The rest wait for the next 15-minute cycle, oldest first. Prefilter skips cost nothing and are not counted.

## Other rules (no decision needed, recorded for review)

- **Request.** One Messages request per email: prompt v1 as the system prompt, the prepared email as the only user message, a JSON Schema structured output, and effort `low`. No tools, no tool choice. Errors keep only a short code, because SDK messages can echo the request.
- **What the model sees.** Subject, sender and date, then the body with quoted history cut (`>` lines, "On ... wrote:", forwarded and Outlook header blocks). The email sits inside `<email>` tags, and any `<email` or `</email` in the email is escaped so it cannot close the fence.
- **Prompt versioning.** `extraction_runs` stores `prompt_version` and the SHA-256 of the prompt template. Changing the prompt means a new version, and every message runs again under it.
- **Schema.** Fields `kind`, `course`, `title`, `due_raw`, `evidence`, `change_type`, with no others; at most 20 items. A reply that is not JSON, or breaks the schema in any way, including an extra field such as an action, is refused whole (`invalid`).
- **Evidence.** It must be at least 8 characters and appear in the body after Unicode NFKC and whitespace collapsing. Case must match. The subject does not count. Items without it are dropped and counted (`dropped=N`).
- **Due times.** `due_raw` must appear in the evidence, ignoring case and spacing, and `parse_due` turns it into `due_at`. A parser reason such as `numeric_date` becomes the review code. A deadline without `due_raw` needs review (`no_due_raw`).
- **Errors.**
  - A refusal, a reply cut off by `max_tokens`, or an email too large for the API is that email's fault. It is recorded once as `error` and not retried under this prompt version.
  - Anything else, including network errors, rate limits, a bad key and other 4xx responses, may affect every email. It records nothing, ends the cycle and is retried next time.
- **Storage.** One `extraction_runs` row per message and prompt version. Each item gets its first `item_versions` row with the evidence, the message and the run. The review code is kept in that row's `after` data.
- **No key.** Live mode without `ANTHROPIC_API_KEY` keeps syncing mail and skips extraction.

## Consequences

- A cycle's audit event holds counts and skip reasons only, never content.
- M2 adds item matching, which replaces decision 2, and the evaluation set that settles decision 1.
- Raising the prompt version reprocesses all stored mail, up to the per-cycle cap.
