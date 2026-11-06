# M0 Validation

The technical design lists seven questions that must be answered before M1. They decide how Sinan connects to each mailbox, which scopes it requests, and which LLM API it uses.

- **Owner:** the project owner. Anything that needs a real account, OAuth client or policy reading is done by the owner.
- **Agent:** prepared the steps and filled in what public documentation already answers. Those findings are marked "From docs" and still need a hands-on check where noted.

## Rules for this document

- Never paste tokens, authorization codes, client secrets or real email content here.
- Record only error codes, screen titles, settings and yes/no outcomes.
- Use placeholders for addresses: `<usc-address>` and `<personal-gmail>`.
- Date every conclusion. Policies change.

## Summary

| # | Question | Status | Conclusion |
| --- | --- | --- | --- |
| 1 | Can the USC account authorize a self-built OAuth app? | Done | Yes |
| 2 | Does USC allow `gmail.modify`? | Done | Yes |
| 3 | Does USC policy allow sending email content to a third-party LLM? | Done | Owner decision: use a third-party LLM, with mitigations |
| 4 | Refresh token lifetime in Testing mode | From docs, needs check | 7 days |
| 5 | Verification requirements for a single-user app | From docs | Stay in Testing mode |
| 6 | `history.list` and custom Calendar event IDs | From docs, needs check | Usable, with fallbacks |
| 7 | LLM API retention, training and structured output | Partly from docs | Owner chooses provider |

Status values: Not started, In progress, From docs, From docs, needs check, Done.

## Shared setup for items 1, 2 and 4

The owner does all of this. No code in this repository is involved, and no token is stored.

1. Sign in to Google Cloud Console with `<personal-gmail>`. Do not use the USC account to create the project.
2. Create a project named `sinan-dev`.
3. Enable the Gmail API and the Google Calendar API.
4. Configure the OAuth consent screen:
   - User type: External.
   - Publishing status: Testing.
   - Test users: `<personal-gmail>` and `<usc-address>`.
5. Create an OAuth client of type "Web application". Add `https://developers.google.com/oauthplayground` as an authorized redirect URI.
6. Open the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground). In settings, choose "Use your own OAuth credentials" and enter the client from step 5.
7. Delete the Playground tokens and revoke access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) when finished.

## 1. Can the USC account authorize a self-built OAuth app?

**Why it matters:** A Workspace admin can block apps that are not on an allowlist. If USC blocks it, M1 cannot read the USC mailbox directly.

**Steps (owner):**

1. In the Playground, request only `https://www.googleapis.com/auth/gmail.readonly`.
2. Sign in as `<usc-address>`.
3. Record what happens:
   - The consent screen appears, possibly with an "unverified app" warning that can be bypassed. This is a **pass**.
   - The flow ends with "Access blocked", `admin_policy_enforced` or "This app is blocked". This is a **fail**.
4. On a pass, exchange the code and call `GET https://gmail.googleapis.com/gmail/v1/users/me/profile`. A 200 response confirms access. Record the status code only.
5. Check whether USC IT publishes a policy on third-party Google apps, and link it in the evidence.

**If it fails:** Auto-forward USC mail to `<personal-gmail>`. First confirm that USC allows auto-forwarding, which is covered by item 3.

**Result:**

- Date: 2026-11-05
- Outcome: Pass. Sign-in went through USC single sign-on with Duo. The consent screen showed the "unverified app" warning, which could be bypassed.
- Evidence: `GET /gmail/v1/users/me/profile` returned HTTP 200.
- Conclusion: M1 connects to the USC mailbox directly with OAuth. Auto-forwarding is not needed.
- Cleanup: Access was removed from the USC account, and the Playground OAuth client was deleted, which invalidates every token it issued.

## 2. Does USC allow `gmail.modify`?

**Why it matters:** Mark-as-read and archive need `gmail.modify`. Google lists it as a restricted scope, and admins sometimes allow read-only scopes but block restricted write scopes.

**Steps (owner), only if item 1 passes:**

1. In the Playground, request `https://www.googleapis.com/auth/gmail.modify` and sign in as `<usc-address>`.
2. Record whether consent succeeds or is blocked.
3. Do not call any modify endpoint. Seeing the consent result is enough for M0.

**If it fails:** The USC mailbox stays read-only. Mailbox actions apply only to `<personal-gmail>`. The design already allows postponing the write token.

**Result:**

- Date: 2026-11-05
- Outcome: Pass. The consent screen for `gmail.modify` appeared and was not blocked.
- Evidence: Consent result only. No modify endpoint was called.
- Conclusion: Mailbox actions (mark as read, archive, Sinan labels) are available on the USC mailbox as well as `<personal-gmail>`.

## 3. Does USC policy allow sending email content to a third-party LLM?

**Why it matters:** Course email can contain other people's information, such as classmates, staff and grades. This is a policy question, not a technical one.

**Steps (owner):**

1. Find the USC policies on acceptable use of IT resources, data classification and generative AI tools. Record each title and link.
2. Find where course email and student records fall in the data classification. Also check FERPA guidance that applies to students.
3. Check whether USC has approved specific AI vendors or has an agreement with one.
4. Check whether auto-forwarding USC mail to a personal account is allowed. This is the fallback for item 1.

**If it is not allowed:** Process only notification-type email, such as LMS announcements and system notices, or use a local model. The prefilter already limits what reaches the LLM.

**Result:**

- Date: 2026-11-05
- Policies found:
  - [Generative AI General Policy](https://policy.usc.edu/generative-ai-general-policy/), issued 2026-01-09, revised 2026-09-01. It applies to students. With AI tools that USC has not procured, Public data is pre-authorized, Internal Use Only and Confidential data need case-by-case approval, and Confidential-Controlled data is prohibited. Section 5.4.3 requires the system owner's approval to connect an AI tool to a University system. The policy does not address students processing their own mail, and does not mention APIs.
  - [Data Classification Standard ST06](https://policy.usc.edu/wp-content/uploads/2024/02/FINAL_ST06_USC_Data-Classification-Standard.pdf) and the [Data Protection Policy](https://policy.usc.edu/data-protection/). USC examples classify internal emails as Internal Use Only.
  - [Email policy](https://policy.usc.edu/?p=4460) bans auto-forwarding to non-USC accounts. The [ITS FAQ](https://itservices.usc.edu/?p=24167) says it covers employee email only. Not needed, because item 1 passed.
- Options considered:
  - A. Ask USC ITS for case-by-case approval.
  - B. Rules-only extraction for USC mail.
  - C. A USC enterprise AI tool.
  - D. Send prefiltered mail to a third-party LLM.
- Conclusion: The owner chose option D. The policy does not cover a student processing their own mail, and the owner judges the risk acceptable. This is a conservative reading of the policy, not legal advice. Asking ITS later stays possible and would not block development.
- Required mitigations for M1:
  - Send only prefiltered mail. Prefer course notices and deadlines. Exclude mail that likely contains grades or other students' personal information.
  - Strip quoted history and unneeded headers before sending.
  - Choose a provider with short retention and no training on API data (item 7).
  - Never put USC mail content in the repository, logs or reports.

## 4. Refresh token lifetime in Testing mode

**From docs:** Google's [OAuth 2.0 overview](https://developers.google.com/identity/protocols/oauth2) says an app with an External user type and Testing status receives refresh tokens that expire in 7 days. The exception is an app that requests only name, email and profile scopes. Gmail scopes do not qualify.

**Impact on design:**

- The owner must re-authorize each account about once a week.
- The Dashboard must show token age, and warn before expiry.
- The Worker must treat `invalid_grant` as "re-authorization needed", not as a crash.
- Sync must catch up after a gap. Item 6 covers this.

**Check (owner):** Note the date of the first authorization in the setup, and confirm that the token stops working after about 7 days.

**Result:**

- Date authorized:
- Date the token stopped working:
- Conclusion:

## 5. Verification requirements for a single-user app

**From docs:** The [Gmail scopes page](https://developers.google.com/workspace/gmail/api/auth/scopes) lists the sensitivity of each scope.

| Scope | Sensitivity |
| --- | --- |
| `gmail.readonly` | Restricted |
| `gmail.modify` | Restricted |
| `gmail.metadata` | Restricted |
| `gmail.labels` | Non-sensitive |

To publish an app with restricted scopes to production, Google requires app verification and a third-party security assessment (CASA). A project in Testing mode skips both, with these limits:

- At most 100 test users over the project's whole lifetime. The count cannot be reset.
- Test users see an "unverified app" warning.
- Refresh tokens expire after 7 days (item 4).

**Preliminary conclusion:** Keep the project in Testing mode with the owner's two addresses as the only test users. Do not apply for verification. Any public demo must run in demo mode with fake providers, and must never ask visitors to connect a real mailbox.

**Owner decision:** Accept a weekly re-authorization in exchange for no verification. Yes / No:

## 6. `history.list` and custom Calendar event IDs

### Gmail incremental sync

**From docs:** The [Gmail sync guide](https://developers.google.com/workspace/gmail/api/guides/sync) says history records are typically available for at least one week, but sometimes for much less. When `startHistoryId` is too old, `history.list` returns HTTP 404, and the client must run a full sync.

**Impact on design:**

- Store the last `historyId` for each account.
- On 404, fall back to a time-window sync, for example `messages.list` with `q=newer_than:14d`, then store a fresh `historyId`.
- Matching on Gmail message ID keeps a re-sync idempotent.

### Calendar event IDs

**From docs:** The [`events.insert` reference](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert) says a client-supplied event ID must:

- Use only base32hex characters: lowercase `a` to `v` and digits `0` to `9`.
- Be 5 to 1024 characters long.
- Be unique within the calendar.

A UUID written as 32 lowercase hex characters without dashes meets these rules, because hex is a subset of base32hex. Generating the event ID from the proposal ID therefore works.

Inserting an existing ID returns HTTP 409. The docs do not say whether a deleted event's ID stays reserved.

**Check (in M1, once the real Calendar provider exists; the owner runs it against a test calendar):**

1. Insert an event with a fixed ID into a dedicated test calendar. Expect 200.
2. Insert the same ID again. Expect 409. The Executor treats this as "already done" and reads the event back.
3. Delete the event, then insert the same ID again. Record whether the result is 409 or 200.

If step 3 returns 409, a deleted event can only be restored with `events.update`. The Executor needs that path, or else the item gets a new ID with an external mapping table.

**Result:**

- Date:
- Step 3 outcome:
- Conclusion:

## 7. LLM API retention, training and structured output

**Why it matters:** Email content leaves the machine. The provider's retention and training terms limit what Sinan may send, and structured output support shapes the extraction code.

**Requirements:**

- No training on API inputs or outputs by default.
- Retention as short as possible, ideally with zero data retention available.
- A JSON Schema structured output mode, or reliable tool-free JSON output.
- No tools are given to the model, whatever the provider supports.

**Comparison (owner fills in the rest from each provider's current terms):**

| Provider | Default retention | Training on API data | Zero data retention | Structured output | Source and date |
| --- | --- | --- | --- | --- | --- |
| Anthropic | Deleted within 30 days | Check commercial terms | By agreement | Check current docs | [Privacy center](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data), 2026-11-05 |
| Provider B | | | | | |
| Local model | None | None | Not applicable | Depends on runtime | |

Secondary reports say that some newer models require 30-day retention even under a zero data retention agreement. Confirm this against the provider's own documentation for the specific model chosen.

**Owner decision:**

- Provider and model:
- Date:
- Reason:

## Impact on M1

Fill this in once all seven items are done.

- USC mailbox access: direct OAuth
- USC mailbox actions: read and write
- Email types sent to the LLM: prefiltered mail only, with the item 3 mitigations
- LLM provider:
- Re-authorization reminder needed: yes / no
