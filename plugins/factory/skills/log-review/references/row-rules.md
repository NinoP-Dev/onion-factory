# Review Logs row: how each value is decided

Contract: `schemas/review-log-row.schema.json` (property names = sheet headers, `x-column-order` = TSV order A..P). `logrow.py` carries an embedded copy of the constraints for repos that do not have the schema file; `SchemaContractTest` in `test_logrow.py` (next to it) fails if the two drift apart, and also checks the eval-case constraints `logrow.py` copies from `schemas/eval-case.schema.json`. Run it alone with `python3 -m unittest discover -s plugins/factory/skills/log-review/references -v`; the repo's `python3 -m unittest discover -s tests` run (and CI) also runs it, through the loader `tests/test_log_review.py`.

## Columns

| Column | Source | Rule |
|---|---|---|
| Reviewer | asked | First name. Two reviewers: `A/B`. Default offered: first word of `gh api user` name. |
| Date Reviewed | derived | Local date of `mergedAt`; if not merged, latest APPROVED review; else today. Format `Nov 05, 2026` (two-digit day). Reviewer may override with `date`. |
| Ticket | derived, confirmed | Upper-case Jira keys (`ABC-123`) from the PR title. Only when the title has none: a key of either case at the start of a head-branch path segment, followed by `-`, `_` or the segment end (`feature/wid-12-x`), upper-cased. Version-like names (`lodash-4`, `python-3.12`, `axios-1.6.0`) are not keys. Joined with ` / `; `N/A` when none. Always shown to the reviewer for confirmation; override with `ticket`. |
| PR | derived | `#<n> - <title> (<url>)`, title on one line. |
| Complexity | asked | Low / Medium / High. Suggestion from size: additions+deletions <= 50 Low, <= 300 Medium, else High. |
| Reviewed By Claude AI | derived | `Yes` when a factory review marker, a factory finding marker, or a managed Code Review result is found. Override with `reviewed_by_claude` (see "Claude review not on the PR" below). |
| Recommendation | derived | `YES` when at least one AI finding is counted. |
| Accepted Recommendation | derived | `YES` when at least one finding NOT judged false was fixed. Fixed = a later commit changed the finding's lines (see below), or the reviewer lists it in `fixed_ids`, or sets `accepted`. |
| Actual Time (mins) | asked | Whole minutes. |
| Est. Time w/o Claude (min) | optional | Left empty unless the reviewer volunteers `est_minutes`. No gate uses it. |
| Notes | derived | `AI findings: F1 Important fixed, F2 Nit false.`, unresolved threads, then the reviewer's own `notes`. Tabs and newlines become spaces. |
| Recommendation Severity | derived | Highest severity among counted AI findings, ranked Important > Nit > Pre-existing; `''` when none. |
| Findings: Both Found | asked | Problems the reviewer found that the AI also found. Required when any AI finding is judged real; otherwise it can only be 0. |
| Findings: AI Only, Real | computed | AI total - AI Only False - Both Found. |
| Findings: AI Only, False | asked (as ids) | Number of AI finding ids the reviewer marks false. |
| Findings: Human Only | asked | Problems the reviewer found that the AI did not. Always required when the AI reviewed the PR; never defaulted to 0. |

Invariant: `Both + AI Only Real + AI Only False = AI total`. `build` rejects `both_found` greater than the real AI findings (total - false).

No AI review on the PR: the four Findings columns are written EMPTY (null), never 0. Empty means "no data"; 0 means "reviewed, nothing found". `build` never fills a Findings count the reviewer did not give.

Claude review not on the PR (`reviewed_by_claude: "yes"` but `gather` found no marker or managed result): nothing can be counted from GitHub. Either the reviewer gives ALL of `ai_findings`, `false_count`, `both_found`, `human_only` (optionally `severity`, `accepted`) and the same arithmetic applies, or none of them and the Findings columns stay empty, Recommendation is `NO` unless `recommendation` is given, and Notes say "Claude review not on the PR; findings not counted." A partial set is rejected.

## Answers JSON accepted by `logrow.py build`

```json
{
  "reviewer": "Pat",            "minutes": 25,          "complexity": "Medium",
  "false_ids": ["F2"],          "both_found": 1,        "human_only": 0,
  "fixed_ids": ["F1"],          "accepted": "YES",      "duplicate_ids": ["R2.F1"],
  "reviewed_by_claude": "Yes",  "est_minutes": 40,      "notes": "free text",
  "date": "Nov 19, 2026",       "ticket": "WID-12",     "miss_summaries": ["..."],
  "ai_findings": 3,             "false_count": 1,       "severity": "Nit",
  "recommendation": "YES"
}
```

Only the first six are normally needed (`both_found` may be left out when no AI finding is judged real). `ai_findings`, `false_count`, `severity`, `recommendation` are only for a Claude review that is not on the PR. `false_ids`, `fixed_ids`, `duplicate_ids` take a list or a comma/space separated string; `"none"` means empty. Unknown ids are rejected.

## Finding the AI findings

1. **Factory markers** (written by `/factory:review-pr`). An inline review comment whose first line is `<!-- factory:finding id=F3 severity=Important -->` is one finding. Replies (`in_reply_to_id`) are ignored. A review body starting with `<!-- factory:review head=<sha> findings=<n> -->` marks one review run; finding markers at the start of a line inside that body count as findings only when no inline comment of the same run has the same id (findings with no postable line). If the declared `findings=<n>` differs from the markers found, `gather` warns.
2. **Several runs** on one PR get keys `R1.F1`, `R2.F1`, ... The same problem reported in two runs is counted twice unless listed in `duplicate_ids`. `gather` sets `repeat_check_needed` when findings come from more than one round (several factory runs, managed inline comments on more than one commit, or factory and managed reviews together) and lists `repeat_candidates`: a later finding with the same file and title as an earlier one from another run or commit. The skill asks the reviewer to confirm the repeats; the confirmed keys are passed as `duplicate_ids`.
3. **Managed Code Review** (Anthropic's GitHub service). Inferred, not documented: an inline comment by a Bot account whose login contains `claude`, whose start carries a severity marker (red circle Important, yellow circle Nit, purple circle Pre-existing, or the word itself). Keys `M1`, `M2`, ... Cross-check: the `Claude Code Review` check run's last line `<!-- bughunter-severity: {"nit":1,"normal":2,"pre_existing":0} -->` (`normal` = Important) on the head commit, following "not repeated" results back up to 10 commits. When no inline comment matches but the check run has counts, placeholder findings are created from the counts (no thread, no line). A mismatch between the two is a warning. `facts.managed.method` states which rule decided.

## Answered or not

GraphQL `pullRequest.reviewThreads` (`isResolved`, first comment's `databaseId` = REST comment id). A finding whose thread is not resolved is UNANSWERED; the Stage 2/3 gate is "none merged with an unanswered finding". Findings without an inline thread (review-body or check-run placeholders) have resolution `null` and cannot be checked automatically; `gather` lists them in an `UNCHECKED FINDINGS` warning (right after `UNANSWERED FINDINGS`) so the reviewer confirms each by hand. If the GraphQL call fails, every resolution is `null` and `gather` warns.

## Fixed or not

For each inline finding, compare the commit it was posted on (`original_commit_id`) with the PR head (`GET repos/{o}/{r}/compare/<sha>...<head>`). The finding counts as fixed when that file's patch deletes, edits, or inserts within 2 lines of the finding's old-side range, or when the file was removed or renamed. Comments on deleted (LEFT side) lines and findings without a line are not checked (`fixed: null`). It is a heuristic: show it to the reviewer, who can override with `fixed_ids`.

## Eval case skeletons

One per Human Only miss: `evals/review/cases/<repo-name>/<repo-name>-pr<n>-miss<k>.json`, relative to the onion-factory repo (the team's eval set; `run_evals.py` reads `evals/review/cases/<project>/` next to itself), not the project repo being reviewed. Each is checked against the eval-case schema's constraints (id pattern, `owner/name` repo, PR number, summary at least 10 characters, source and severity enums) with `source: human-only-finding`, `reference` = the PR cell, `added` = today. The summary is the reviewer's `miss_summaries` entry, or a `TODO` to replace; an entry under 10 characters becomes a `TODO` that quotes it, with a warning. The runner needs the PR merged; `build` warns when it is not.
