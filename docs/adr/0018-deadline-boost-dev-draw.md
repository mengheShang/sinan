# ADR 0018: A supplementary dev draw of mail likely to hold deadlines

- Status: Accepted (2026-11-15)
- Date: 2026-11-14
- Decider: Menghe
- Amends: ADR 0012 (sampling the real sets), decisions 4 and 5

## Context

The owner finished labeling the real sets (dev 100, test 50) on 2026-11-14. They hold 60 notices but only 5 required items (deadlines and to-dos): 3 in dev and 2 in test. With 3 required items in dev, one miss moves `item_recall` and `due_accuracy` for deadlines by 33 points, so #51, #54 and #43 cannot tell a real change from noise.

The owner decided on 2026-11-14 (Issue #78) to add a supplementary dev draw of mail likely to hold a deadline, chosen deterministically by keywords and never by extraction results; 40 emails with no school/personal split; an addition to the current dev set version, not a redraw; recorded with stratum `deadline_boost` and split out in reports. The test set stays unchanged (ADR 0012, decision 4). ADR 0010 is reserved for sender trust (#51), so this is a new ADR rather than a long amendment inside ADR 0012.

## Decisions

### 1. "Likely holds a deadline" is a keyword or sender match on the frozen snapshot

An email is a candidate when its frozen subject or body (`app/db/labels.freeze`, exactly what labeling stores) matches one of these, ignoring case, as whole words: `due`, `deadline`, `submit`, `exam`, `quiz`, `midterm`, `final`, `homework`, `assignment`, `project`, `register by`, `by <weekday>` and `no later than`. Plural and verb endings count (`exams`, `quizzes`, `submitted`); longer words do not (`finally`, `projector`, `overdue`). A weekday is its full name or a common short form (`fri`, `thurs`). Whitespace between the words of a phrase may be any run of spaces or line breaks.

An email from a course-platform sender is also a candidate, without a keyword. The sender list holds real addresses, so it lives in `config/deadline_boost.toml`, which git ignores, in the same pattern as the prefilter rules (ADR 0006). When the file is absent, the fictional example list `app/db/deadline_boost.example.toml` is used and the output says `boost senders: example`.

Rejected: "emails where extraction found a deadline". It is circular: recall would only be measured on what the model already finds.

The match uses no model and no extraction run. The prefilter outcome is not a filter here: skipped mail can be drawn, and each pick records its prefilter outcome.

### 2. The draw: 40 emails, no role split, outside every earlier draw

`python -m apps.worker.sample --seed <n> --boost-dev 40` draws from candidates that can be frozen and are in no earlier draw (dev or test, any version). The candidates are sorted by id and shuffled with a generator seeded from the seed and the dev set version, under a name of its own (`sinan-boost:...`), and the first 40 are taken. The same seed over the same mail gives the same draw.

Without `--write` it is a dry run; `--write` records it. The output holds counts only: candidates, unlabelable and already-drawn mail, likely candidates per role, and the picks per role.

A shortfall stops the draw, as in ADR 0012 decision 3: with fewer than 40 likely candidates nothing is recorded, and the output says how many it needs and has.

### 3. The supplement joins the current dev set version, once

The picks are `eval_sample.pick` events with set `dev`, the current dev set version, stratum `deadline_boost`, the role, and `prefilter` (`passed` or `skipped`). The `eval_sample.draw` event adds `stratum: deadline_boost` and the sender list source. No migration is needed.

A dev set version takes one supplement. A second `--boost-dev` on the same version is refused, with or without `--write`; a new supplement comes only with a new dev set (`--redraw-dev`), and is drawn for that version. `--boost-dev` before the first draw is refused. The test set gets no picks and none of its picks change.

This amends ADR 0012 decision 4: a dev set version is no longer exactly 100 emails once it has a supplement; it is 100 plus 40.

### 4. The labeling page lists the current dev set version, original draw and supplement together

ADR 0012 decision 5 said the page shows only the newest draw of each set. It now reads: the page shows the newest **set version** of each set, which includes its supplement. Because the supplement shares the set version, no query changes: `sampling.current` already selects every pick of the newest version. Within the dev set, emails stay oldest first, so supplement emails sit among the original ones by date; a `changes` link can point either way. Saving a supplement email labels it into the dev set version it joined. The page's dev count includes the supplement (140 instead of 100).

### 5. Reports give the whole dev set and the supplement alone

`python -m evals.run --set dev --live` reads the current dev set version from the label table (`evals/real.py`), runs it once in `sent_at` order, and reports the usual counts and metrics for the whole set, plus `parts.random` and `parts.deadline_boost` with the same counts and metrics for each part. A change in one part is scored against the item matched for its target in either part, so the parts add up to the whole.

The supplement is chosen by keywords, so it overstates how often mail holds a deadline. Precision and the notice share are read from `parts.random`; recall and due accuracy for deadlines from the whole set and `parts.deadline_boost`.

The dev set has no recorded replies: replies of real mail are never saved, so `--set dev` is live only and refuses `--save-replies`. The report holds counts and metrics only, as before (ADR 0009, decision 5).

## Consequences

- The owner runs the draw on real mail after merging; the agent never does.
- The supplement adds 40 emails to label in the dev queue.
- Dev metrics from before and after the supplement are not comparable; reports name the set version and give the parts.
- If keywords miss a deadline format that matters, the owner can add a sender to the local list before drawing; changing the keyword list after a draw changes what a new dev set version would draw, not the recorded one.
