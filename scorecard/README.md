# Review scorecard

`scorecard.py` turns the **Review Logs** tab of the Ancillary Code Reviews sheet into the weekly
scorecard for the review rollout. It computes the plan's measures and, with `--stage`, checks the
Stage 2 or Stage 3 gate. It needs Python 3.9 or later and nothing outside the standard library.

These numbers judge the review agent, never a person. The script never reports anything per
reviewer and does not keep reviewer names after reading the CSV.

## Run it

1. In the sheet, open the **Review Logs** tab and choose File > Download > Comma-separated values.
2. Run:

```sh
python3 scorecard/scorecard.py --csv ~/Downloads/review-logs.csv \
  --repo acme/widgets \
  --baseline-from 2026-01-01 --baseline-to 2026-03-31 \
  --eval-results evals/review/results/widgets \
  --stage 2
```

Paste the text output into Slack inside a code block. Use `--format markdown` for a PR or wiki page
and `--format json` for scripts.

| Option | Meaning |
|---|---|
| `--csv PATH` | The Review Logs export. Required. |
| `--repo OWNER/NAME ...` | Only rows whose PR URL points at these repos. Repeatable. Rows without a PR URL are left out. |
| `--since YYYY-MM-DD` | Only rows reviewed on or after this date. Applies to the gate window too. |
| `--last N` | The measures table covers the newest N PRs (after `--repo` and `--since`). With `--stage` the default is 15 (Stage 2) or 30 (Stage 3). The gate always uses 15 or 30, whatever `--last` says. |
| `--baseline-from`, `--baseline-to` | The Stage 0 review-time baseline window. Both or neither. Without them the scorecard says "no baseline". |
| `--eval-results PATH` | One eval result JSON, or a directory searched recursively; the run with the newest `started_at` is used. Point it at `evals/review/results/<project>` so runs from other projects are not picked. |
| `--cost-per-review USD` | Average cost of one review, read from the Claude admin page. |
| `--spend-limit USD` | The monthly spend limit (set org-wide in the Claude admin settings). |
| `--monthly-spend USD` | Code Review spend over the last 30 days, from the Claude admin page. The Stage 3 cost item needs it to PASS. |
| `--stage 2\|3` | Evaluate that stage's gate. |
| `--format text\|markdown\|json` | Output format. Default `text`. |

Exit status is 0 whenever a scorecard is printed, whatever the verdict. It is 2 when the CSV or an
eval result cannot be read, or the arguments are invalid. Warnings (skipped rows, unrecognised
dates, missing columns) go to stderr, and into `warnings` in the JSON output.

## Measures

| Measure | How it is computed | Target |
|---|---|---|
| False finding rate | Sum of "Findings: AI Only, False" divided by the sum of "Both Found" + "AI Only, Real" + "AI Only, False", over rows where all three are filled. The output says how many rows had data. | below 0.2 |
| Catch rate (eval) | `score.found / score.total` from the newest eval run. Passed means `found >= threshold` and `errors == 0`. | 8 of 10 |
| Agent misses | "Findings: Human Only" summed per PR (two reviewer rows for one PR add up), divided by the PRs that have it filled. | falling |
| Coverage | Rows with "Reviewed By Claude AI" = Yes divided by all selected rows. | every PR |
| Review time per PR | "Actual Time (mins)" summed per PR (two reviewer rows for one PR add up), then median and mean. The output says how many PRs had a time. The gate compares the medians of the gate window and the baseline window. | no higher than baseline |
| Cost per review | As entered. `--monthly-spend` (admin page) is compared with the spend limit. Without it the scorecard shows a **lower bound**: cost per review x Claude-reviewed PRs dated in the 30 days ending on the newest row, across every repo in the sheet. It undercounts: "After every push" runs a review per push, and repos not in the sheet spend too. | inside the limit |
| Unanswered findings at merge | Not in the sheet. Reported as a manual check; the main-branch ruleset enforces it. | 0 |

"Est. Time w/o Claude (min)" is a guess and is not used.

## Gates

Each gate item is PASS, FAIL or NO DATA. Missing data is never a PASS. The verdict is FAIL if any
item fails, NO DATA if any item lacks data, and PASS otherwise. Items marked MANUAL CHECK are not
in the sheet; a person confirms them before calling the gate passed.

| Item | Stage 2 (last 15 PRs) | Stage 3 (last 30 PRs) |
|---|---|---|
| Window holds 15 / 30 PRs (fewer is NO DATA) | yes | yes |
| Every PR has a row with Claude = Yes and all four Findings columns filled | yes | yes |
| False finding rate below 0.2 | yes | yes |
| Newest eval run passed | yes | yes |
| None merged with an unanswered finding | manual check | manual check |
| Team says findings help | manual check | no |
| Median review time per PR at or below the baseline median | no | yes |
| Cost per review entered and monthly spend inside the limit | no | yes |

The gate window is always the newest 15 or 30 PRs after `--repo` and `--since`; `--last` changes
only the measures table. Rows that cannot be told apart as PRs are left out of every window, with a
warning: rows whose PR cell has no PR number (for example "N/A"), and, when other rows carry a PR
URL, rows whose PR cell has no URL (so "#5" and "acme/widgets#5" are not counted as two PRs).

Review time is NO DATA unless at least 90% of the window's PRs have a number in "Actual Time
(mins)" (27 of 30) and the baseline window has at least 5 PRs with a time. The detail line shows
"k of N PRs have a time".

Cost is PASS only from `--monthly-spend`. Without it the item is FAIL when the lower bound is
already over the limit and NO DATA otherwise.

## What the parser tolerates

The sheet is hand-filled. The script reads columns **by header name**, so column order does not
matter, and it handles:

- a UTF-8 byte-order mark, extra whitespace in headers, rows above the header, and the header row
  repeated further down (skipped);
- rows with trailing columns missing, and blank rows;
- dates written `Apr 01 2026`, `May 13, 2026`, `Sept 23 2026`, `Aug 1, 2026`, `September 3, 2026`,
  `2026-04-01` or `4/1/2026`; rows are ordered by date, and an undated row takes the date of the
  row above it;
- PR cells of the form `#412 - Title (https://github.com/<owner>/<repo>/pull/412)`, with or without
  a line break before the URL; the repo comes from the URL;
- numbers with surrounding spaces. Empty cells, whitespace and text such as "No Claude AI review"
  are **no data**, never 0. This matters most for the four Findings columns, which are empty on
  older rows.

## Tests

```sh
python3 -m unittest tests.test_scorecard -v
```

`tests/fixtures/review-logs.csv` is synthetic (fake reviewers and the repos `acme/widgets` and
`acme/gears`). Never copy real sheet rows or notes into this repo.
