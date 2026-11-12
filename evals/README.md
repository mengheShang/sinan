# Evaluation sets

An evaluation set is a list of emails, each with the labels a correct extractor would produce. The evaluation runner (ADR 0009, Issue 2) feeds each email to the pipeline and compares the result with the labels.

- `synthetic.jsonl`: 51 fictional emails, safe to commit and used in CI and the demo. Every person, course, address and domain is made up, and senders use the reserved `example.*` domains.
- Real development and test sets live in the label table in the live database, never in this repository (ADR 0009, decisions 3 and 5). They use the same format.
- `dataset.py` loads a set and checks it. A bad sample fails with its line number and field path, never the email text.

The format was approved by the owner on 2026-11-11.

## Format

JSON Lines: one email per line, as one JSON object. Shown here across several lines for reading:

```json
{
  "id": "syn-005",
  "input": {
    "from_addr": "rivera@example.edu",
    "subject": "CS 000: Homework 3 extended",
    "body_text": "Homework 3 is now due Monday Nov 16 at 11:59pm PT.",
    "sent_at": "2026-11-06T17:00:00Z",
    "role": "school",
    "headers": {}
  },
  "labels": {
    "items": [
      {
        "ref": "cs000-hw3-v2",
        "kind": "deadline",
        "course": "CS 000",
        "title": "Homework 3",
        "due_raw": "Monday Nov 16 at 11:59pm PT",
        "due_at": "2026-11-17T07:59:00Z",
        "due_ambiguous": false,
        "changes": "cs000-hw3"
      }
    ],
    "safe_to_auto_read": false
  },
  "tags": ["course", "extension"]
}
```

### `input`: the frozen snapshot

Exactly what the prefilter and the extractor see of the email, and nothing else.

| Field | Meaning |
| --- | --- |
| `from_addr` | Sender address. |
| `subject` | Subject line. |
| `body_text` | Plain-text body, as the extractor receives it. |
| `sent_at` | The email date the extractor uses: the sent time, or the received time when that is missing. UTC, ending in `Z`. |
| `role` | `school` or `personal`. |
| `headers` | Only `List-Unsubscribe` and `Precedence`, when present. They drive the prefilter's bulk-mail check, so prefilter recall can be measured on the snapshot. Any other header is rejected. |

### `labels`: the correct answer

`items` lists every deadline and to-do in the email. An empty list means the email holds none; there is no separate "has an item" field.

| Field | Meaning |
| --- | --- |
| `ref` | A name for the item, unique in the set (lowercase letters, digits and `-`). Later items refer to it. |
| `kind` | `deadline` or `todo`. |
| `course` | Course code, or `null` when the item is not about a course. |
| `title` | Short title. |
| `due_raw` | The due-time phrase as written in the email, or `null` if there is none. It must appear in the subject or body. |
| `due_at` | The correct due time in UTC, ending in `Z`, or `null`. |
| `due_ambiguous` | `true` when the email gives a due time that a correct system must not guess and should mark `needs_review`. |
| `changes` | `null` for a new item. For an item that changes an earlier one (an extension, a moved exam), the `ref` of the item it changes. |

`safe_to_auto_read` (required for every email) is `true` when the email could be marked read automatically without the owner missing anything. It is used only in M3 and is labeled now to avoid a second pass (ADR 0009, decision 1).

`tags` is optional. Tags group results in reports (`course`, `job`, `bill`, `injection`, `bulk-header`, ...) and are never scored.

## Rules the loader enforces

- Unknown fields and missing fields are rejected. Booleans must be JSON booleans.
- Times are UTC and end in `Z`.
- The due fields agree:
  - no `due_raw` means `due_at` is `null` and `due_ambiguous` is `false`;
  - a `due_raw` with a `due_at` has `due_ambiguous` set to `false`;
  - a `due_raw` with `due_at` set to `null` has `due_ambiguous` set to `true`.
- Ids and item refs are unique in the set.
- `changes` names a ref from an email with an earlier `sent_at`.

## Labeling notes

- `due_at` follows the deterministic rules in ADR 0005, which say when a time counts as ambiguous. For example:
  - a numeric date such as `11/18`, "midnight", "next Friday", a missing time and a missing time zone are all ambiguous;
  - a missing year is not: it is inferred from the email date.
- Phrases whose meaning is still open, such as "end of day" (ADR 0009, Issue 4), are left out of the synthetic set until they are decided.
- An extension or a rescheduled event is a new labeled item whose `changes` points to the item it replaces. Chains are allowed: the third version points to the second.
- Instructions inside an email (prompt injection) are labeled as data. The labels describe the email's real deadlines and ignore what the email asks the system to do.
- Marketing deadlines ("sale ends Sunday") and receipts are not items.

## Changing the synthetic set

Edit `synthetic.jsonl` directly, keep every name and address fictional, and run `uv run pytest tests/test_eval_dataset.py`.
