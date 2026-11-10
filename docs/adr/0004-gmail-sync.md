# ADR 0004: Gmail sync scope, schedule and demo data

- Status: Accepted
- Date: 2026-11-10
- Decider: Menghe

## Context

Issue #10 syncs each connected mailbox into `messages`. Three choices were left open: which mail to keep, how often to sync, and how to keep demo data apart from real mail now that the demo writes messages too.

## Decisions

### 1. Sync all mail except spam, trash, sent and drafts

Course notifications are often labeled and archived by Gmail filters, so they never reach the inbox. Syncing only the inbox would miss deadlines in that mail.

What is excluded:
- Spam, trash and drafts.
- Mail you sent, unless you sent it to yourself and it also sits in the inbox.

The rule is applied twice:
- On Gmail's side, in the time-window query.
- In `gmail_parse.wanted` for every fetched message, so the incremental path and the fake follow the same rule.

### 2. Sync every 15 minutes

That is fast enough for deadlines and far inside Gmail's quota. The interval is `SINAN_SYNC_INTERVAL_MINUTES`. The Worker also syncs once at startup, never runs two syncs at once, and skips any account that needs re-authorization.

### 3. Demo and live use separate databases

Demo mode uses `<POSTGRES_DB>_demo`, and live mode uses `<POSTGRES_DB>`. `Settings.db_url()` applies the suffix, and `python -m app.db.ensure` creates the database before migrations run.

Switching modes therefore never mixes fake mail with real mail. Switching back to demo does not touch real data. Nothing has to be cleaned up by hand.

## Consequences

- The cursor is read before mail is listed, and saved only in the final commit. A crash leaves it unchanged, and the next run fetches again, with no duplicates because inserts ignore existing rows.
- The Worker now receives `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, so it can trade the read refresh token for an access token. It still only decrypts the `read` credential.
- The demo mailbox has fixed dates in early November 2026. After about 30 days, a new demo first sync will find none of it. Dating it relative to today would break the fixed deadlines in its text, so this is left as is for now.
