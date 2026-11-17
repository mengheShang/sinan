# M2 evaluation

The reported M2 metrics (Issue #87). Placeholders (`—`) are filled in after the official run.

```text
<header line printed by the runner: commit | labels test vN | model | prompt vN sha256:… | date>
```

## Metrics

All rows use the same prompt and model as the header. Percent, with hits/total for the official run.

| Metric | Test, official | Test, range (2 runs) | Dev | Synthetic |
| --- | --- | --- | --- | --- |
| Item recall | — | — – — | — | — |
| Item precision | — | — – — | — | — |
| Due-time accuracy | — | — – — | — | — |
| Change accuracy | — | — – — | — | — |
| Demoted to notice | — | — – — | — | — |
| Notice escalated | — | — – — | — | — |

- **Test, official**: the one official run on the 50-email test set. Its raw replies are kept in the local database only, so `--set test --recorded` reproduces these numbers.
- **Test, range**: two more live runs, min–max only. They never replace the official numbers.
- **Dev**: the current dev set version, one live run. Read precision from its `random` part.
- **Synthetic**: 68 fictional emails, recorded replies, as in CI.

## Known limits

- The test set is small (50 emails), so one email moves a metric by several points.
- The model is not deterministic; the range shows how much repeat runs vary.
- The dev set was used for tuning, so dev numbers are likely higher than test.
- Sender trust reads `Authentication-Results` again from Gmail at run time; the official run keeps what it read so the replay matches.

## Reproducing

The commands are in `evals/official.py` and the PR for Issue #87.
