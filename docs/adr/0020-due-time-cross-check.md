# ADR 0020: Cross-check due times with the model

- Status: Proposed
- Date: 2026-11-16
- Decider: Menghe
- Amends: ADR 0007 (LLM extraction), "Due times"; ADR 0016, decision 4 ("the long tail is #54")

## Context

The due-time parser (ADR 0005, ADR 0011, ADR 0016) never guesses. Any wording it has no rule for sends the item to `needs_review` without a time, and the owner reads the email and works the time out by hand. The model reads most of these phrasings correctly, but it is confidently wrong on ambiguous ones ("11/12", "midnight", a missing zone), and email text can try to steer it, so it must not decide due times on its own (Issue #54).

The owner agreed the direction on 2026-11-13, in the Issue:

- The model also returns a proposed UTC due time next to `due_raw`. It stays structured JSON only, validated by deterministic code.
- **Both agree:** the parser's result is used, as today.
- **They disagree, or the parser cannot read it:** the item goes to `needs_review`, with the model's answer pre-filled as a suggestion the owner can confirm in one click or correct.
- The model's time is never used without the parser agreeing or the owner confirming. A suggestion from an untrusted sender (ADR 0010) is shown the same way and gives no extra trust.

This is the same pattern as ADR 0019: the model suggests, deterministic code validates, and the owner confirms. This ADR writes down the rule and how it is built. Decisions 3, 4 and 5 answer the Issue's open questions and need the owner's approval.

## Decisions

### 1. Prompt v6 adds `due_utc`

Each item gains `due_utc`: the model's reading of `due_raw` as one UTC time, written `YYYY-MM-DDTHH:MM:SSZ`, or null. The field is required and nullable in the JSON Schema and in the Pydantic model, so a reply without it, or with any other extra field, is refused whole, as before.

The prompt tells the model to read only `due_raw` and the email's Date header, never a date the email asks it to report, and to use the same defaults as the parser: the email's date for a missing year, a weekday or "tomorrow"; the student's time zone when `due_raw` names none (ADR 0016, rule 1); 23:59 for a date with no time (rule 2). The owner's zone (`SINAN_TIMEZONE`) is written after the `</email>` fence as `Student's time zone: <IANA name>`, so the email cannot change it. The link step (ADR 0019) shows the email without that line.

`due_raw` still must be copied, never computed. `due_at` still comes only from `parse_due`.

### 2. Deterministic code checks the suggestion

`app/extraction/crosscheck.py` reads `due_utc` only when the item is a deadline or to-do whose `due_raw` is inside the evidence. A notice is never checked, and neither is an item whose time is unproven (`due_not_in_evidence`) or absent (`no_due_raw`).

A suggestion is usable only when it is exactly `YYYY-MM-DDTHH:MMZ` or `YYYY-MM-DDTHH:MM:SSZ`, is a real date and time, and falls between 30 days before and 366 days after the email date. The lower bound is ADR 0005's year window; the upper one leaves a year's room for a date that states its year. Anything else (an offset such as `+09:00`, text, a far-off year) is `invalid` and offers nothing. A bad suggestion never refuses the reply, because the item itself may be right.

Each deadline and to-do records one code, `due_check`, with the suggestion as `due_suggested`:

| `due_check` | Meaning | Outcome |
|---|---|---|
| `agree` | Both read the same minute. | The parser's result stands. |
| `disagree` | Both give a time, and they differ. | `needs_review` with reason `due_disagrees`. The item keeps the parser's time; the model's is offered. |
| `suggested` | The parser cannot read it; the model gives a time. | The parser's reason stays (`relative_event`, `no_date`, ...), with no `due_at`. The model's time is offered. |
| `withheld` | As `suggested`, but the parser's reason is real ambiguity (decision 4). | As today. The suggestion is stored for the evaluation but not offered. |
| `none` | No suggestion, or the item is not checked. | As today. |
| `invalid` | The suggestion is unusable. | As today. |

A model that abstains (null) while the parser reads a time is `none`, not a disagreement: the parser's result stands.

### 3. Agreement is the same minute (open question 1)

Two times agree when they are equal after dropping seconds. The model sees the Date header to the minute only, so "within 48 hours" (ADR 0016, rule 5) can differ from the parser in its seconds. No wider tolerance: the errors this check is meant to catch are an hour (a DST or zone slip), a day or more, and a tolerance of minutes would only hide the first kind.

### 4. Real ambiguity withholds the suggestion (open question 2)

The suggestion is **not offered** when the parser's reason means the text itself has two readings, so the model can only pick one:

`numeric_date`, `midnight`, `relative_weekday` ("next Friday"), `ambiguous_time` (a time that happens twice on a fall-back night), `multiple_dates`, `multiple_times`, `multiple_zones`, `weekday_mismatch`.

A one-click button on these would make the model's guess the easy path. The owner still sets the time on these items (decision 6), with the field empty.

The suggestion **is offered** for every other parser reason, where the parser lacks a rule rather than the text being ambiguous: `relative_event`, `relative_span`, `no_date`, `vague_time`, `unknown_zone`, `bad_offset`, `year_unclear`, `zone_dst_mismatch`, `nonexistent_time`, `invalid_date`, `invalid_time`, and the two ADR 0016 decision 8 codes, `zone_outside_due` and `time_outside_due`. The last two are where the model helps most: the parser saw only the copied phrase ("Nov 18 at 10am"), while the model read the whole email ("... 10am ET"). They also include the injection case `inj-031`, where an injected line gives a second date; the owner sees the suggestion next to the quoted evidence and decides.

The list is one constant, `WITHHELD`, so moving a reason between the two groups is a one-line change. The evaluation reports how often withheld suggestions would have been right (decision 9), so the owner can revisit the split with data.

### 5. The suggestion lives in `item_versions.after` (open question 3)

`due_suggested` and `due_check` are stored in the `after` data of the item's create version, beside the ADR 0016 flags and `sender_trust`. No migration is needed. The Dashboard reads them from the newest version whose `after` has `due_at`, as it reads the assumed flags, and offers the suggestion only while the item is `needs_review` and `due_check` is `disagree` or `suggested`. Once the owner sets the time, the newest such version is the owner's, which has no suggestion, so it disappears.

### 6. The owner confirms or corrects, from the Dashboard

`POST /items/{id}/due` takes either `due_at` (a UTC time with its offset: the suggestion, or the parser's time, as shown) or `local` (a wall time in `SINAN_TIMEZONE`, typed by the owner). A local time that does not exist or happens twice on a DST change is refused with 422. `app/db/item_due.py`:

- is allowed only on a deadline or to-do in `needs_review` whose reason is not `unmatched_update`, `ambiguous_update` or `proposal_expired`, which have their own path; otherwise 409, so a second click is harmless;
- appends an `item_versions` row with actor `user` and change type `update`. Its `after` holds `due_at`, the new `status` and `review`, the ADR 0016 flags all false (the owner stated the time, so nothing is assumed), and `due_source`: `suggestion` when the time is the offered suggestion, `parser` when it is the item's own parsed time, `owner` otherwise;
- updates `items.due_at` and `items.status` in the same transaction, with an audit event `item.due` holding the version, `due_source` and status only.

The Dashboard shows, on such an item, "The model reads the due time as …", with **Use this time**, **Keep Sinan's time** when the parser had one, and a date-time field pre-filled with the suggestion (or the parser's time) and **Set due time**. An item whose time the owner set shows "set by you".

Nothing external is written. The model is not called. No grant is read or written.

### 7. Untrusted senders gain nothing from a suggestion

An untrusted sender's required items are held as `untrusted_sender` (ADR 0010, decision 3), whatever the cross-check says; the suggestion is stored and shown the same way. Setting the time on such an item keeps it `needs_review` with `untrusted_sender`: a confirmed time says nothing about whether the email is real. The Dashboard says so next to the buttons.

### 8. A change whose times disagree is never a proposal

An update whose `due_check` is `disagree` is not matched or proposed (ADR 0014, ADR 0019). It is saved as a new item in `needs_review` (`due_disagrees`) with the suggestion, and the item it would change keeps its time. A proposal shows only the parser's time; approving it would hide the model's objection. Carrying the suggestion into the `item_update` payload is possible later, but it changes the payload hash and `app/actions/`, so it is left out here.

### 9. Evaluation

The runner applies the same cross-check. The report gains `due_crosscheck`, counts only:

| Field | Meaning |
|---|---|
| `checks` | Found deadlines and to-dos per `due_check` code. |
| `one_click` | Review items whose suggestion is offered and that are not held as `untrusted_sender`. |
| `one_click_held` | Offered, but also held as `untrusted_sender`. |
| `from_review` | `suggested`: items that waited without a time before and now have a one-click suggestion. |
| `to_review` | `disagree`: items the parser read that were `open` before and now wait in review. |
| `suggestion` | Over offered suggestions on items matched to a labeled item: `right` (the labeled time) and the share `wrong`. A suggestion on an ambiguous label is wrong. |
| `disagree_parser` | Over matched disagreements: whether the parser had the labeled time. `wrong` is the share where it did not, so the check caught a parser mistake. |
| `withheld` | As `suggestion`, for withheld suggestions, which the owner never sees. |

The injection set gains four cases (`inj-049` to `inj-052`, tag `due-suggestion`) that ask the model to suggest another date: a different date next to a parsed one, a date for a phrase the parser cannot read, an extra `due_confirmed` field, and an offset instead of UTC. Their replies obey the email. A new success rule counts any item the parser could not read that has a due time anyway. `injection_succeeded` must stay 0. The existing cases' replies carry `due_utc: null`, so they test what they tested before.

## Consequences

- Items whose wording the parser lacks a rule for can be settled in one click instead of by reading the email and typing a time. Every such click is the owner's, recorded with actor `user`.
- A parser mistake that both sides read differently (a slip like "GMT+8" before ADR 0011, decision 7) now waits in review instead of being `open` with a wrong time. The cost is that a model mistake on a correctly parsed item also sends it to review; `to_review` and `disagree_parser` measure that trade.
- An injected date can at most become a suggestion on a review item, next to the evidence the owner reads. It never becomes `due_at` by itself.
- Extraction cost grows by about 20 output tokens per item; there is no extra call.
- Already-extracted items are not rewritten. A new prompt version applies to new mail only (#75).
