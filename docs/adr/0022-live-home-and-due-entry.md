# ADR 0022: A live home page, entering due times, and model-suggested due times

- Status: Accepted (2026-11-19)
- Date: 2026-11-19
- Decider: Menghe
- Supersedes: ADR 0008 (daily digest), except where noted below
- Amends: ADR 0005 (numeric dates), ADR 0021 (where automatic actions are listed)

## Context

Using the Dashboard on real mail showed four problems:

1. **The home page is a daily snapshot.** The Worker freezes which items go into which section at 7 a.m. (ADR 0008). The "new since the last digest" section mixes deadlines and to-dos and repeats items shown in the other sections, so the same item can appear twice. The owner never uses the digest as a digest.
2. **A due time that does not parse cannot be fixed.** An item whose due phrase needs review, or that has no due phrase at all, stays that way. There is no way to enter a due time by hand.
3. **Phrases the model leaves out.** In one real case the email said the owner would "have 5 days" to finish an assessment, and the model returned no due phrase. The parser already handles "within N days", but it never saw this phrase.
4. **Numeric dates always need review.** "Tue, 12/1/2026" was marked for review, and its label quoted a fixed example ("11/12 could mean two days") instead of this email's date. That made it look as if Sinan had misread the date.

The owner decided the following on 2026-11-19.

## Decisions

### 1. The home page is live, and each item appears once

The home page is computed when it is read. Nothing is stored for it. It has three lists:

- **Due:** every active deadline or to-do that has a due time, soonest first.
  - Overdue items (ADR 0015's two-day window) sort to the top with an "Overdue" tag.
  - Items that need review come at the end of the list, with their review reason as a tag. Each has a **Confirm** button: it moves the item to open (a version with actor `user`), unless the item has no due time, in which case it opens the due form (decision 3). Dismiss stays as it is.
- **To-do:** active deadlines and to-dos with no due time, newest first. Each has an **Add due** button (decision 3). Once a due time is set, the item moves to Due.
- **Notices:** as today (ADR 0017, ADR 0020), grouped by category.

Items first found since the owner's previous visit carry a **New** tag in whichever list they are in. This keeps the digest's "what changed" view without listing an item twice.
- The previous visit's time is kept in a browser cookie, so no table is needed.
- A visit moves that time forward only when it comes 30 minutes or more after the last one. Reloading the page does not clear the tags.
- Without the cookie (first visit, another browser), nothing is tagged.

There is no 7-day window, no separate needs-review section and no "new since" section. The Past view (ADR 0015) and the closed-items page are unchanged.

### 2. The daily digest stops

- The Worker no longer makes digests, and the Dashboard no longer reads them.
- The `digests` and `digest_items` tables stay, with their rows, until the live home page has proven itself. A later migration drops them.
- The count of proposals that expired unanswered moves to the home page, computed on read like everything else.
- ADR 0021 said automatic actions "appear in the daily digest" (acceptance criterion 7) and that paused grants and unsubscribe observations go "in the digest". They go instead in a "Sinan did this" list on the home page, with the same undo buttons. Issue #103 changes accordingly.

### 3. The owner can set or change any due time

- Every active deadline or to-do has a due form, reached from Add due, from Confirm on an item with no due time, and from an Edit link next to an existing due time.
- The form takes a local date and an optional time in SINAN_TIMEZONE. With no time it uses 23:59, as ADR 0016 does, and shows that it did.
- Saving appends an `item_versions` row: `change_type = 'update'`, `actor = 'user'`, the new `due_at`, `due_raw` set to the text the owner entered, review cleared, status `open`, and the suggestion's ID when the form was prefilled (decision 4).
- It is a local database write, made only by the API in answer to the owner, like approving an item_update proposal. The model never sets a due time.

### 4. The Worker asks the model for a suggested due time

- On each cycle, after extraction, the Worker picks active deadlines and to-dos that have no due time and no suggestion yet, up to a per-cycle cap. For each one it asks the model once, with the fenced email and the item's title, as ADR 0019's link step does.
- The model returns JSON only: a date, an optional time, and a short quote from the email that supports them, or null when the email gives no due time.
- Deterministic code accepts a suggestion only when:
  - the quote appears in the email;
  - the date exists;
  - the date falls between 30 days before and 365 days after the email.

  Anything else is stored as "no suggestion", so the item is not asked about again.
- Suggestions go in a new append-only table, `due_suggestions`: the item, the message, the suggested time or none, the quote, the model, and the prompt version and hash. One row per item. Migration; needs line-by-line review.
- A suggestion only prefills the due form, labelled as the model's suggestion with its quote. Nothing changes until the owner saves the form. The saved version records which suggestion it came from.
- Live mode without an API key makes no suggestions; demo mode uses a fake model.

### 5. Numeric dates: the weekday can decide, and the label shows the readings

- ADR 0005 marks every numeric date for review, because 11/12 has two readings. From now on, a numeric date next to a weekday is accepted when exactly one of its two readings (month/day, day/month) is a real date that falls on that weekday.
  - "Tue, 12/1/2026" is accepted as December 1, because January 12, 2026 is a Monday.
  - A date with no weekday, or one where both readings or neither fit the weekday, still needs review.
- The `numeric_date` label names this email's two readings ("12/1 could be Dec 1 or Jan 12") instead of a fixed example.

## Consequences

- One list per kind of item, and no duplicates. A new item appears as soon as it is extracted, not the next morning.
- Every item can reach a due time. When the parser fails, the owner can fix it, and the model's suggestion makes that a click when the suggestion is right.
- The model still never writes a due time. Its suggestion is shown with its quote, and only the owner's save applies it. That save is traceable to the suggestion, the model and the prompt.
- ADR 0008's snapshot and its "rows never change" property go away, along with the morning digest hour. `SINAN_DIGEST_HOUR` becomes unused; it is removed with the tables.
- One more model call per item without a due time, at most once per item.

## Order

1. Numeric dates with a weekday, and the label (decision 5).
2. Set and change due times, and Confirm (decision 3).
3. The live home page, and stopping the digest (decisions 1 and 2).
4. Suggested due times (decision 4). Needs 2.
5. Later: drop the digest tables and `SINAN_DIGEST_HOUR`.
