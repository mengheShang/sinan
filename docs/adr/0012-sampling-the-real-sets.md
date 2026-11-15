# ADR 0012: Sampling the real development and test sets

- Status: Proposed
- Date: 2026-11-12
- Decider: Menghe

## Context

Issue #41 draws the real sets the owner labels (ADR 0009, decision 4). The owner approved the mix on 2026-11-12: dev 100 emails and test 50; in each set, half from the school mailbox and half from the personal one; about 20% of each set from mail the prefilter skips. The draw must repeat from a fixed seed, the sets must not overlap, and the test set is fixed once drawn. Five details were open.

## Decisions

### 1. The draw is recorded as audit events, without a new table

`python -m apps.worker.sample --write` adds one `eval_sample.draw` event (seed, set version, rules source, counts) and one `eval_sample.pick` event per email (`subject_id` = message id; data: set, set version, role, stratum). `audit_events` is already append-only (migration 0002), so a draw cannot be edited or removed, and no migration is needed.

Trade-off: the database does not enforce one pick per email or the set names, as a dedicated table with constraints would. The command enforces both, under a transaction-scoped advisory lock so two draws cannot interleave, and tests cover them. Reads filter on `event_type` without an index, which is fine for a few hundred events. If draws ever need constraints or many more rows, a table can replace the events by copying them.

### 2. "Prefilter skipped" means the current rules skip the frozen snapshot

The command freezes each message exactly as labeling would and runs the prefilter's `decide` on that snapshot with the rules the Worker loads (the local rules file, or the example rules when it is absent; the output says which). Stored `skipped` extraction runs are not used: they exist only where extraction ran, and they reflect the rules at that time. The evaluation recomputes the prefilter on the snapshot too, so the stratum matches what is measured.

Messages that cannot be frozen (for example an empty body or a malformed sender) are left out, because they could never be labeled. The output counts them.

### 3. A shortfall stops the draw

If any role and prefilter outcome has fewer candidates than both sets need, nothing is recorded and the output lists every short stratum with what it needs and has. No stratum tops up another, so the approved mix holds exactly or not at all.

Each stratum's candidates are sorted by id and shuffled with a generator seeded from the seed, the set version and the stratum. The test set takes the first ones, then the dev set, so the two cannot overlap.

### 4. The test set is drawn once; the dev set can be redrawn with the next set version

The first draw creates both sets as set version 1. Any later run without `--redraw-dev` is refused, with or without `--write`. There is no option to redraw the test set.

`--redraw-dev` draws a new dev set of 100 with set version (newest dev version + 1), only from mail outside every earlier draw, so draws never overlap. Labels from an earlier dev draw keep their version and stay in the table; `load_set(session, "dev", set_version=n)` reads one draw.

The first draw is refused while any current label sits on an email outside every draw (one made with the stopgap set selector of PR #53), because the draw cannot place it.

### 5. The labeling page shows only the newest draw of each set

Amended by ADR 0018 (2026-11-14): "newest draw" means the newest set version, which includes its `deadline_boost` supplement. A dev set version can grow by one supplement of 40 emails (decision 4 then gives 100 plus 40).

The queue lists the dev set first, then the test set, each oldest first. Saving a label takes its set and set version from the draw; the request cannot choose either, and the set selector is gone. Corrections keep the old row's set and version, as before. Emails outside the newest draw answer 404, including the emails of an earlier dev draw.

## Consequences

- The owner runs the command on real mail after merging; the agent never does.
- The command prints counts only, never an address, subject or body.
- `labeling.SET_VERSION` is gone: the set version comes from the draw.
