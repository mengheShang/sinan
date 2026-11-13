# ADR 0013: M3.5 course sources

- Status: Proposed
- Date: 2026-11-12
- Decider: Menghe

## Context

Some course deadlines appear only on a course website and never in an email. An instructor's own site may post an assignment page and send no notice. Brightspace keeps due dates in its calendar. A missed deadline is the failure Sinan exists to prevent (ADR 0009), and the mailbox alone cannot catch these.

The technical design defers course platforms until after M4, and its risk table says no new connectors or domain packs before M4, so that scope does not grow again before the main chain is done. This ADR makes a bounded exception to that rule.

The owner checked one real case: a course site that only students with an instructor-issued shared account can open. It uses HTTP Basic Auth over HTTPS with a self-signed certificate issued for a different host name.

## Decisions

### 1. Add milestone M3.5 "Course sources" between M3 and M4

M3.5 adds exactly three source kinds:

1. **Public course pages**: pages that need no login.
2. **HTTP Basic Auth course pages**: pages behind a browser login box, using an account an instructor issues to the class.
3. **Brightspace calendar feeds**: the iCal subscription link Brightspace exports.

This is the only exception to "no new connectors before M4". Every other connector (163, alumni mail, Apple Calendar, other course platforms) and every domain pack still waits until after M4. M3.5 is done when a change to a configured page or feed shows up as a traceable item within one fetch cycle, and its evaluation and injection cases pass.

Cost: the M4 release moves back by about one to two weeks.

### 2. Sources are read-only and their content is untrusted

- Sources are only read. The fetcher sends GET requests only. It never submits forms, posts data or downloads anything other than the configured pages.
- It fetches only the page URLs the owner configured, and it never follows links to crawl further pages.
- Fetching is deterministic code in `app/providers/`, with a fake implementation for tests and the demo. The LLM never sees credentials, cookies or headers, and it gets no tools, as everywhere else (CLAUDE.md rule 3).
- Page text is untrusted data, like email (CLAUDE.md, "Security boundaries"). It goes through the same extraction, schema validation and evidence check, and its evidence must appear in the fetched text.
- Fetch frequency: by default at most twice a day per source, with a minimum interval the owner cannot set below one hour. This keeps load on instructors' servers low.
- Limits: a request timeout, a maximum response size, HTML and plain text only, and no redirect that leaves the source's origin. PDFs are deferred: a deadline that appears only inside a linked PDF is not seen in M3.5. If one is needed later, a single PDF URL the owner adds could become its own source kind, with text-only extraction and size limits.

### 3. Only changes are extracted, and every item traces back to a fetch

- Each fetch stores a snapshot of the page's extracted text and its content hash. If the hash has not changed since the last fetch, nothing more happens.
- When the hash changes, only the changed text is sent for extraction, together with the source name and the fetch time, which takes the role of the email date.
- An item from a source links to its source and the snapshot it came from, instead of an email, so acceptance criterion 2 (every item shows its origin and evidence) still holds.
- Changed deadlines follow the same matching and `item_update` proposals as email (ADR 0009, decision 2).

### 4. Credentials: HTTP Basic Auth only, entered by the owner, bound to one origin

- The only login M3.5 supports is HTTP Basic Auth. Form logins, single sign-on (such as the university login) and any two-factor step are out of scope. A CAPTCHA, or a site that switches to one of these, stops the source and flags it in the digest; the fetcher never tries to get past it.
- The owner enters the user name and password in the Dashboard. The agent never handles, reads or enters them (CLAUDE.md, "Roles").
- They are encrypted at rest with the same scheme as the OAuth tokens, and never logged, put in audit events or shown again.
- A credential is bound to its source's origin (scheme, host and port). It is sent only to that origin, never on a redirect, and never over plain HTTP.
- Deleting the source deletes its credential.
- These changes need the owner's line-by-line review (CLAUDE.md: token encryption).

### 5. TLS: verify normally, or pin an untrusted certificate the owner confirmed

- By default the certificate is verified as usual.
- If the certificate is not trusted (self-signed, or issued for another name), the Dashboard shows its SHA-256 fingerprint and validity dates when the source is added, and the owner can choose to pin it. With a pin, a connection is accepted only when the server's certificate has exactly that fingerprint, and only then are credentials sent.
- A changed fingerprint stops the source, sends no credentials, and asks the owner to confirm again in the digest. Certificate verification is never simply turned off.

### 6. A Brightspace feed link is a secret

- Anyone holding the link can read the calendar, so it is stored and protected like a credential (decision 4).
- Each calendar event becomes a candidate item. Its evidence is the event's own title, date and description fields. The event's stable ID is used to recognize later changes to the same event.

### 7. Real addresses stay out of the repository

Real course URLs, names, fingerprints and credentials live only in the live database (configured in the Dashboard) and never in Git (CLAUDE.md rule 2). Tests and the demo use fictional sites on `example.edu`.

### 8. Evaluation covers sources too

- A synthetic page set (fictional course pages, before and after a change), labeled in the #33 format with the page snapshot as the input, joins the evaluation runner.
- Injection cases for pages (hidden text, fake instructions, pages that try to move the deadline) join the injection set, and the CI gate (Issue #42) applies to them.

## Consequences

Proposed Issue order for M3.5:

1. Source tables migration: sources, fetch snapshots, and an item's link to a snapshot (decision 3). Line-by-line review.
2. Public page fetcher with change detection and extraction (decisions 2 and 3).
3. HTTP Basic Auth credentials and certificate pinning (decisions 4 and 5). Line-by-line review.
4. Brightspace calendar feed (decision 6).
5. Sources page in the Dashboard: add, pin, check status, delete.
6. Synthetic page set and page injection cases (decision 8).

Other consequences:

- The technical design's milestone table and the "no new connectors before M4" rule are amended by this ADR.
- M3.5 depends on M2's item matching and proposals, but not on M3's Executor, because it writes nothing to external services.
