# M2 evaluation

The reported M2 metrics (Issue #87).

```text
commit 776c5034fee3 | labels test v1 | model claude-haiku-5-5 | prompt v7 sha256:fab461ad06bb | 2026-11-18
```

## Metrics

All rows use the prompt and model in the header. Percent, with hits/total for the official run.

| Metric | Test, official | Test, range (2 runs) | Dev | Synthetic |
| --- | --- | --- | --- | --- |
| Item recall | 80.95 (17/21) | 80.95–85.71 | 77.61 | 88.71 |
| Item precision | 65.38 (17/26) | 62.96–69.23 | 74.29 (random part 70.37) | 100.0 |
| Due-time accuracy | 100.0 (17/17) | 100.0–100.0 | 94.23 | 96.36 |
| Change accuracy | — (no labeled changes) | — | — | 80.0 |
| Demoted to notice | 0.0 (0/1) | 0.0–0.0 | 0.0 | 2.08 |
| Notice escalated | 10.0 (2/20) | 5.0–5.0 | 3.17 | 0.0 |

- **Test, official:** the one official run on the 50-email test set, of which the prefilter passed 40. Its raw replies are kept in the local database only, and `--set test --recorded` reproduces these numbers exactly (checked 2026-11-18).
- **Test, range:** two more live runs, min–max only. They never replace the official numbers.
- **Dev:** dev set v1 (140 emails), one live run.
- **Synthetic:** 68 fictional emails, with recorded replies, as in CI.

## Known limits

- **The test set is small, and almost all notices.** It has 50 emails, 21 labeled items and only 1 deadline or to-do, so one email moves a metric by several points, and deadline metrics rest on the synthetic set.
- **That one required test item was held as `untrusted_sender`.** Its sender's domain is on the trust list, but Gmail's results did not show a pass. This is noted for M3 and not tuned against the test set.
- **Precision is the weak spot.** The model reports some notices for mail labeled as having none: 9 of 50 test emails.
- **The model is not deterministic.** The range shows how much repeat runs vary.

## Reproducing

```bash
docker compose run --rm --no-deps worker python -m evals.run --set test --recorded
```

The image must be built from the commit in the header (`SINAN_COMMIT`), and the local database must hold the official run (`eval_runs`).
