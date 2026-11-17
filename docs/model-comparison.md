# Model comparison: Haiku 5.5 and Sonnet 5.5 (Issue #43)

- Date: 2026-11-17
- Set: the real dev set, 140 labeled emails (100 random plus 40 deadline boost). The prefilter passed 97 of them to the model. The test set was not used.
- Prompt `v6`, link step `link-v1`, effort `low`, the same request for both models.
- Run in a Worker container with `python -m evals.run --set dev --live --model <id>`, twice per model.
- Summary metrics only (ADR 0009, decision 5). Retention terms for both models are in `docs/m0-validation.md` item 7.

## Dev set

Where the two runs differ, the cell gives run 1 / run 2.

| | Haiku 5.5 | Sonnet 5.5 |
| --- | --- | --- |
| Item recall | 77.61 (52/67) / 77.61 | 77.61 (52/67) / 77.61 |
| Item precision | 72.22 (52/72) / 74.29 (52/70) | 75.36 (52/69) / 75.36 |
| Due accuracy | 94.23 (49/52) / 94.23 | 98.08 (51/52) / 98.08 |
| Demoted to notice | 0.0 (0/4) / 0.0 | 0.0 (0/4) / 0.0 |
| Notice escalated | 3.17 (2/63) / 1.59 (1/63) | 0.0 (0/63) / 0.0 |
| `email_groups` agreement | 108/140 / 110/140 | 109/140 / 109/140 |
| Input tokens | 282,531 / 282,531 | 282,531 / 282,531 |
| Output tokens | 20,897 / 20,859 | 7,918 / 8,127 |
| Est. cost per 100 emails sent | $0.040 / $0.040 | $0.664 / $0.666 |
| Latency per call, median | 1.49 s / 1.18 s | 1.73 s / 1.82 s |
| Latency per call, p90 | 2.77 s / 2.77 s | 2.00 s / 2.10 s |

- No invalid replies in any run. The dev set has no labeled changes, so the link step was never asked and change accuracy cannot be measured here.
- Only 4 labeled required items are in the dev set, so differences of one or two items move the percentages a lot.
- Cost uses the per-token prices in the claude-api skill's model table (cached 2026-11-03, used 2026-11-17): Haiku 5.5 at $0.10 input and $0.50 output per million tokens (prompts up to 100K tokens), Sonnet 5.5 at $2 and $10. "Per 100 emails sent" divides by the 97 extraction calls; no prompt caching is used.
- Latency is wall-clock time per successful call from the container, including any SDK retries.

## Synthetic set, for reference (one live run each)

| | Haiku 5.5 | Sonnet 5.5 |
| --- | --- | --- |
| Item recall | 88.71 (55/62) | 87.10 (54/62) |
| Item precision | 100.0 | 100.0 |
| Due accuracy | 96.36 (53/55) | 100.0 (54/54) |
| Change accuracy | 80.0 (8/10) | 90.0 (9/10) |

These replies were not saved. The committed recorded replies and the baseline are unchanged.

## Recommendation

Keep `SINAN_LLM_MODEL=claude-haiku-5-5`. On the dev set both models find the same items (recall 77.61). Sonnet's gains are two more correct due times, one or two fewer notices escalated to required, and one to three fewer extra items. Sonnet costs about 17 times as much per email and has a higher median latency. Sonnet was steadier between runs and better on due times, so it is worth another look if due-time errors show up in daily use or on the test set (#87). The owner decides.
