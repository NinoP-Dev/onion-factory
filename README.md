# onion-factory

Tooling for the team's AI code-review rollout (the "Review Phase Rollout Plan"): an AI review on
every pull request, with a person still approving every merge. The repo packages the plan's steps
as a Claude Code plugin (`factory`), an eval runner, a weekly scorecard and a GitHub Actions
template. Every gate is a number read from the **Review Logs** tab of the Ancillary Code Reviews
sheet, or from an eval run.

The rules every piece follows:

- The agent comments; a person approves. Nothing here approves, requests changes, merges or pushes.
- Every finding is answered before merge: fix it, or resolve the thread with one line saying why
  not. **Important** is fixed unless the reviewer agrees it is wrong, **Nit** is the author's call
  (at most five per review), and **Pre-existing** becomes a Jira ticket.
- The numbers judge the agent, never a person.

## What serves which stage

| Piece | Path | Stage | What it does |
|---|---|---|---|
| `/factory:bootstrap-repo` | `plugins/factory/skills/bootstrap-repo/` | 0, 4 | Checks a project's groundwork (CLAUDE.md under 200 lines ending with the REVIEW.md pointer, CI running one all-checks command, main-branch ruleset, PR template, `.gitignore`, connection points and CODEOWNERS) and prints the gate table. Without `--check` it offers to create missing files from its templates, as local changes only. |
| REVIEW.md, CLAUDE.md, PR template, CODEOWNERS templates | `plugins/factory/skills/bootstrap-repo/templates/` | 0, 1, 4 | The team-wide REVIEW.md (`## Always check`, `## Do not flag`, `## Severity`) plus a `## This project` section. |
| `/factory:review-pr` | `plugins/factory/skills/review-pr/` | 1, 2 | Reviews the branch, a PR or a base ref against CLAUDE.md, REVIEW.md and `.claude/rules`. `--comment` posts one COMMENT review with inline findings; `--json` prints the review-result object the eval runner reads. |
| Eval runner | `evals/review/run_evals.py` | 1, 2, 3 | Runs `/factory:review-pr <base> --json` headless on each eval case and has a judge call decide whether the known problem was found. Gate: 8 of 10. |
| Eval cases and results | `evals/review/cases/<project>/`, `evals/review/results/<project>/` | 1 onward | Ten past changes with known problems per project, and one results file per full run. |
| `/factory:log-review` | `plugins/factory/skills/log-review/` | 2, 3 | Builds the reviewer's Review Logs row for a PR: counts AI findings from the factory markers, checks which threads are resolved, asks only what it cannot derive, and prints a paste-ready TSV line. |
| Scorecard | `scorecard/scorecard.py` | 0 (baseline), 2, 3, 4 | Reads the Review Logs CSV export and prints the measures, and with `--stage 2` or `--stage 3` the gate verdict. |
| Automatic review fallback | `templates/github/claude-review.yml` | 3 | GitHub Action for repos that cannot use Claude's managed Code Review. Same markers, comment-only. See `templates/github/README.md`. |
| Contracts | `schemas/` | all | JSON Schemas shared by the pieces above: Review Logs row, finding, review result, eval case, eval result. |
| Repo checks | `tools/validate.py`, `tests/`, `.github/workflows/ci.yml` | n/a | Validate the schemas, eval data and plugin layout, and run the unit tests. |

`/factory:review-pr` and `/factory:log-review` share one marker format, which the Action template
writes too. The first line of every inline finding comment is
`<!-- factory:finding id=F3 severity=Important -->`, and the first line of the review body is
`<!-- factory:review head=<sha> findings=<n> -->`.

## Install the plugin

The marketplace is this repo (marketplace `onion-factory`, plugin `factory`). In Claude Code:

```text
/plugin marketplace add NinoP-Dev/onion-factory
/plugin install factory@onion-factory
```

From a shell, the same steps are `claude plugin marketplace add NinoP-Dev/onion-factory` and
`claude plugin install factory@onion-factory`. The GitHub form works only once the repo is pushed,
and only for people with read access to it. To try a local checkout for one session, run
`claude --plugin-dir ./plugins/factory`.

The skills are then `/factory:bootstrap-repo`, `/factory:review-pr` and `/factory:log-review`.
Run them from inside the project repo, not from this one.

## First week on the lighthouse project

Run the skills in the project repo. Run the eval runner and the scorecard from a checkout of this
repo. `<project>` is the short name of the project's eval folder, for example `widgets`.

1. **Groundwork (Stage 0).** In the project repo run `/factory:bootstrap-repo --check`, then
   `/factory:bootstrap-repo` to create the missing files as local changes. Write CLAUDE.md with
   `/init` and trim it to under 200 lines. It must end with
   `## Reviews` / `Before any code review, read REVIEW.md.` Open a PR with the Jira key in the
   title and merge it. A repo admin turns on the main-branch ruleset from the steps the skill
   prints. Record the review-time baseline:
   `python3 scorecard/scorecard.py --csv review-logs.csv --repo <owner/name> --baseline-from <YYYY-MM-DD> --baseline-to <YYYY-MM-DD>`.
2. **REVIEW.md (Stage 1).** Start from the template that bootstrap-repo creates. Keep it to one
   page, fill `## This project` with rules learned from real misses, and merge it to the default
   branch. The managed Code Review reads it only from there.
3. **Ten eval cases.** Pick ten merged PRs with a known problem: a bug that reached users, or a
   Review Logs row with a Human Only finding. Write each one as
   `evals/review/cases/<project>/<id>.json` (see `evals/review/cases/example/`), then run
   `python3 tools/validate.py`.
4. **Run the eval.**
   `python3 evals/review/run_evals.py --project <project> --repo-path <clone> --dry-run` makes no
   model calls. Run it again without `--dry-run` for real. The gate is the known problem found in
   at least 8 of 10 cases. The team also marks each other finding `useful` or `noise` in the
   results file, and fewer than 1 in 5 may be noise. Rerun the eval every time REVIEW.md changes.
5. **Every PR (Stage 2).** The author runs `/factory:review-pr` on the branch and fixes what
   applies. They open the PR with the Jira key in the title and run
   `/factory:review-pr <PR number> --comment` to post the findings. Then they answer every
   finding. The reviewer reads the change before the agent's findings, approves, and runs
   `/factory:log-review <PR number>`. They paste the TSV line into Review Logs.
6. **Weekly, 30 minutes.** Export Review Logs as CSV and run
   `python3 scorecard/scorecard.py --csv review-logs.csv --repo <owner/name> --baseline-from <...> --baseline-to <...> --eval-results evals/review/results/<project> --stage 2`.
   Take the most common false finding and the most important miss, then change REVIEW.md or
   CLAUDE.md, rerun the eval, and post the result in Slack. Every miss becomes a new eval case
   (log-review prints a skeleton for each).

The Stage 2 gate covers 15 PRs in a row. Every PR must be reviewed and logged, the false finding
rate must stay below 1 in 5, none may merge with an unanswered finding, the eval must stay at 8 of
10 or better, and the team must say the findings help. Stage 3 automation, the managed Code Review
or `templates/github/claude-review.yml`, starts after that gate. Its gate is `--stage 3` over the
last 30 PRs.

## Develop and test

Python 3.9 or later, standard library only (`jsonschema` is optional; `tools/validate.py` uses it
when installed). From the repo root:

```sh
python3 tools/validate.py                      # schemas, eval cases/results, plugin layout
python3 -m unittest discover -s tests -v       # all unit tests, including log-review's
claude plugin validate .                       # Claude Code's marketplace checks
claude plugin validate plugins/factory         # Claude Code's plugin checks
```

CI (`.github/workflows/ci.yml`) runs the first two on Python 3.9 and 3.12, and the plugin checks as
an advisory job. The tests make no network, `gh` or `claude` calls.

## Data rules

- Never commit real Review Logs rows, reviewer names or sheet notes. Test fixtures are synthetic
  (`acme/widgets`, fake names).
- Eval cases and results describe real project code. Keep this repo's visibility no wider than
  the projects it holds cases for.
- Changing a file in `schemas/` changes a contract. Update every reader and writer in the same PR.
