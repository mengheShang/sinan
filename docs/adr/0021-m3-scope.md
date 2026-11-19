# ADR 0021: M3 scope decisions

- Status: Accepted (2026-11-19)
- Date: 2026-11-18
- Decider: Menghe

## Context

M3 ("action layer") adds the approval UI, grants, the Executor, calendar and mailbox actions, and unsubscribe. Its exit criterion is that acceptance criteria 4 to 8 of the technical design pass:

4. No prompt injection case bypasses approval or a grant, or causes an unauthorized write.
5. Repeating an action takes effect once.
6. Every external action traces back to its email, its approval or grant, and its result.
7. Automatic mark-read and labels happen only within a valid grant, can be undone, and appear in the daily digest.
8. Unsubscribe runs only after approval, and only as a validated one-click request.

Today the Executor only runs a heartbeat, there is no write token, `grants` and `actions` do not exist (ADR 0002), and `proposals` has one kind, `item_update`, applied by the API (ADR 0009, decision 2). Breaking M3 into Issues raised the questions below. The owner decided them on 2026-11-18.

## Decisions

### 1. Only the Executor can decrypt the write token

Today one `SINAN_ENCRYPTION_KEY` reaches the API, the Worker and the Executor, so a write token encrypted like the read token could be decrypted by the Worker.

- Write tokens are sealed with an asymmetric scheme: X25519 key agreement with a fresh ephemeral key, HKDF, then AES-GCM, all from the existing `cryptography` package.
- The API holds only the Executor's public key (`SINAN_EXECUTOR_PUBLIC_KEY`). It seals the refresh token once, in the OAuth callback, and cannot open it again.
- Only the Executor holds the private key (`SINAN_EXECUTOR_PRIVATE_KEY`) and the OAuth client secret it needs to refresh access tokens.
- The Executor no longer receives `SINAN_ENCRYPTION_KEY`, so it cannot decrypt the read token either. The Compose `backend` anchor is split accordingly.
- Needs line-by-line review (token encryption).

### 2. A separate "Enable actions" OAuth flow, for both mailboxes

- Each account gets its own write flow, separate from the read-only flow of ADR 0003, with `credentials.purpose = 'write'`.
- Every connected mailbox can enable it, with `gmail.modify` only. Today that is three: the school mailbox, the main personal mailbox, and a second personal mailbox connected on 2026-11-18.
- The calendar gets its own "Connect calendar" flow with `calendar.events` only (decision 3), stored with a new `credentials.purpose`, `calendar`. It may use any Google account. Its token is sealed for the Executor in the same way. Keeping it apart from the mailbox write token means the calendar works whether or not that mailbox enables actions, and each token can be revoked alone.
- The flow stores the token only when Google granted exactly the requested scopes, as ADR 0003 does.
- Write tokens expire after 7 days in Testing mode like read tokens, and use the same "expires soon / expired / needs reconnect" states.
- Needs line-by-line review (OAuth, read and write permissions).

### 3. Calendar: `calendar.events`, one dedicated calendar

- The scope stays `calendar.events`, as in the technical design. The limit "write only the Sinan calendar" is enforced in code.
- `calendar.events` cannot create calendars, so the owner creates a calendar named Sinan in Google Calendar and enters its ID in the Dashboard. The calendar provider refuses any other calendar ID.
- The calendar lives on the second personal account, the one the owner's browser is signed in to and the one they read calendars on. Event reminders apply only to the user whose token wrote them, so a calendar shared from another account would not show Sinan's reminders there.
- That account may hold up to three tokens: read, write and calendar, each with its own purpose and its own 7-day expiry.

### 4. The Executor polls the database

- Approving only changes rows in the database. The Executor polls every 10 seconds for approved proposals and grant actions that have not run, and claims each with `SELECT ... FOR UPDATE SKIP LOCKED`.
- The Executor opens no port. Nothing can command it directly; it acts only on rows whose state passes its own checks.
- An approval made while the Executor is down runs when it comes back.

### 5. Grants

- **Scope:** one account, a list of exact sender addresses (no domains), and the action kinds `mark_read`, `label` and `archive`.
- **Created and revoked only by the signed-in owner in the Dashboard.** No model output and no Worker code path creates or changes a grant (CLAUDE.md rule 5).
- **Expiry:** at most 90 days. Revoking takes effect before the next action.
- **Cap:** at most 20 actions per grant per sync cycle. Going over pauses the grant and puts it in the digest; the owner resumes it.
- **Sender check:** the email must pass authentication for its From domain (DMARC pass, or a DKIM pass aligned with it), the same check ADR 0010 uses. It does **not** need to be on the ADR 0010 trust list, which covers course senders, not the newsletters most grants are for. The exact-address list is the owner's explicit trust.
- **Exclusions:** no action when the email produced any item, of any kind or status, notices included, or when it is still waiting for extraction. The Executor checks the grant and these conditions again right before acting.

### 6. Notice read state stays in step with Gmail

The Dashboard has no separate "read" state for a notice. Dismissing it (ADR 0017, decisions 6 and 7) is the owner saying "seen". So:

- **Dashboard to Gmail:** dismissing a notice, alone or with Dismiss all, also marks its email read in Gmail. The click is the one-time approval. It goes through the Executor as a `mark_read` action, with an idempotency key and an audit event. It is skipped when the same email still has an open deadline or to-do, so the unread mark keeps pointing at work.
- **Gmail to Dashboard:** when sync sees that an email has lost `UNREAD`, its open notices are dismissed, with actor `system` and the reason in the version row. Deadlines and to-dos are never changed this way. This direction needs only the read token.
- Grants never mark a notice's email read (decision 5).

### 7. Calendar events: one click, with reminders

- An item has an "Add to calendar" button. It opens a dialog showing exactly what will be written: title, course, start, end and reminders. Confirming is the one-time approval; the proposal's `payload_hash` is the hash of what the dialog showed.
- Items in `needs_review`, without `due_at`, and notices cannot be added.
- **Reminder templates.** A keyword rule on the title (exam, midterm, final, quiz) preselects one; the owner can switch it or change the times in the dialog. The model plays no part.
  - Exam: the event starts at the exam time and lasts 2 hours. Pop-up reminders 1 day and 2 hours before.
  - Deadline: the event starts at the due time. Pop-up reminders 2 days and 3 hours before.
  - The values come from configuration. Only pop-up reminders are used, never email reminders.
- When an `item_update` proposal is approved and the item already has an event, a `calendar_update` proposal is created automatically.
- The event ID is derived from the idempotency key (hex, which is valid base32hex), so a retry cannot create a second event.
- The event holds the title, course and due text only, never evidence or other email content.

### 8. Unsubscribe trusts Gmail's DKIM result and checks coverage itself

- Sync additionally keeps `List-Unsubscribe-Post` and every `DKIM-Signature` header (today only the first of each kept header is stored).
- A request is valid only when: both `List-Unsubscribe` and `List-Unsubscribe-Post: List-Unsubscribe=One-Click` are present; Gmail's `Authentication-Results` shows `dkim=pass` for a domain aligned with From; the `DKIM-Signature` with that `d=` lists both headers in `h=`; and the URL is HTTPS, has no user info, and resolves only to public addresses.
- Sinan does not verify DKIM signatures itself.
- Execution follows the technical design: one POST without cookies, no redirects, the `actions` row written before sending, and no automatic resend after a crash.
- Unsubscribe is built last. If M3 runs late, it moves after M3, as the technical design allows.

### 9. Undo, and proposal lifetimes

- Undo is an action of its own, run by the Executor, with its own idempotency key (from the original action) and audit event. Nothing reverts an action outside the Executor.
- `mark_read`, `label` and `archive` undo from `undo_payload`. An approved `calendar_update` restores the old values; a `calendar_create` is undone by deleting that event, only on the Sinan calendar.
- Unsubscribe has no undo.
- Lifetimes: `calendar_*` proposals expire after 7 days or at the due time, whichever is first, like `item_update`. `unsubscribe` proposals expire after 3 days.

## Consequences

Proposed Issue order (marked items need line-by-line review):

1. Migration: `grants`, `actions` (unique `idempotency_key`, `undo_payload`, source must be a proposal or a grant), new proposal kinds, and the `calendar` credential purpose. **Review.**
2. Executor key pair, the mailbox write OAuth flow and the calendar-only flow (decisions 1 to 3). **Review.**
3. Executor core: polling, rechecks of status, expiry, `payload_hash` and grant, action row before the call, read-back, audit. Fake providers only. **Review.**
4. Gmail write provider (only `UNREAD`, `INBOX` and Sinan labels) with a fake, and the CI forbidden-API check narrowed to allow `modify` only there. **Review.**
5. Notice read sync in both directions (decision 6).
6. Grants: Dashboard pages, the policy check, the Worker creating grant actions, caps and pausing. **Review.**
7. Digest: list every automatic action with one-click undo.
8. Calendar provider, "Add to calendar" dialog with reminder templates, and `calendar_update` after an approved change. **Review** for the provider.
9. Unsubscribe: header capture and the validator, with a malicious-header test set.
10. Unsubscribe execution: SSRF-safe single POST, no resend, sender observation in the digest. **Review.**
11. Acceptance gates: action injection cases (fake grant requests, bait to mark read, archive or unsubscribe), the "important mail mishandled" metric, and the unsubscribe validation error count, all in the CI gate.

After 1 to 3, the mailbox work (4 to 7), the calendar (8) and unsubscribe (9 and 10) can proceed in parallel. Issue 9's validator is pure code and can start at any time.

Other consequences:

- **Measuring mishandled mail.** The metric runs the grant policy on the labeled snapshots as if one grant covered every sender, and counts emails labeled `safe_to_auto_read: false` that it would act on. The target is 0, and any value above 0 fails CI.
- `.env.example` gains `SINAN_EXECUTOR_PUBLIC_KEY`, `SINAN_EXECUTOR_PRIVATE_KEY`, the reminder template values and the unsubscribe lifetime.
- The out-of-scope item in ADR 0017 ("Mark read" that also marks Gmail) is covered by decision 6.
- The technical design's "write only the Sinan calendar" stays a code-level limit, not a Google-level one.
