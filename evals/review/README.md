# Review eval

Checks that the review agent still finds problems the team already knows about.
It is the Stage 1 gate (known problem found in at least 8 of 10 cases) and is
rerun every time a project's REVIEW.md changes, and before each later gate.

**Cost:** a real run spends Claude tokens. Each case makes two `claude` calls:
the review, then a judge call (skipped when the review has no findings). Use
`--dry-run` first; it calls no model.

## Layout

```
evals/review/
  run_evals.py                     the runner (Python 3.9, standard library only)
  cases/<project>/<id>.json        one known problem per file (schemas/eval-case.schema.json)
  cases/example/                   a made-up case and how to pick the ten
  results/<project>/<run_id>.json  one file per full run (schemas/eval-result.schema.json)
```

## Run it

Needs `git`, `gh` (signed in, for cases given as a PR number) and `claude`.

```sh
# check cases, resolve commits, print the exact commands; no model calls
python3 evals/review/run_evals.py --project widgets --repo-path ~/src/widgets --dry-run

# the real run
python3 evals/review/run_evals.py --project widgets --repo-path ~/src/widgets

# after editing REVIEW.md on a branch, judge the set by the branch's standards
python3 evals/review/run_evals.py --project widgets --repo-path ~/src/widgets --standards-ref my-review-md-branch

# one case, keep its worktree to look at (a partial run: see below)
python3 evals/review/run_evals.py --project widgets --repo-path ~/src/widgets --only bulk-discount-double-applied --keep-worktrees
```

| Option | Default | Meaning |
|---|---|---|
| `--project` | required | Name of the case folder under `cases/`. |
| `--repo-path` | required | Local clone of the project repo. Missing commits are fetched from `origin`. |
| `--cases` | `cases/<project>` | Case folder. |
| `--out` | `results/<project>` (full run), temp folder (partial run) | Where the run's JSON is written. A partial run may not write under `results/`. |
| `--standards-ref` | `origin/HEAD` | Ref whose `CLAUDE.md`, `REVIEW.md` and `.claude/rules/` are laid over every case. |
| `--threshold` | `8` | Cases that must be found to pass. Below 8 makes the run partial. |
| `--only ID...` | all | Run only these cases. Makes the run partial. |
| `--model` | CLI default | Model for both calls; recorded in the results. |
| `--plugin-dir` | this repo's `plugins/factory` | Plugin that provides `/factory:review-pr`. |
| `--dry-run` | off | Validate and resolve everything, print the commands, call no model. |
| `--keep-worktrees` | off | Leave each case's temporary worktree on disk. |
| `--timeout` | `900` | Seconds allowed per `claude` call. |
| `--no-fetch` | off | Skip `git fetch origin` before resolving `--standards-ref`. |

Exit codes: `0` PASS, `1` FAIL, `2` invalid configuration or at least one case
errored (an errored case is not counted as found, and the run cannot pass).

### Full and partial runs

The scorecard reads the newest file under `results/<project>/` as the
catch-rate gate result, so only full runs go there. A run is **partial** when
it uses `--only`, a `--cases` folder other than `cases/<project>`, or a
`--threshold` below 8. A partial run without `--out` is written to
`<system temp>/factory-eval-partial/<project>/` (the path is printed). A
partial run with `--out` anywhere under `results/` is refused before any
`claude` call (exit 2). Never copy a partial run's file into `results/`.

## What a run does

For each case:

1. **Resolve commits.** A `pr` case runs `gh pr view <pr> -R <repo> --json ...`;
   head is the merge commit, base is its first parent. A `base`/`head` case uses
   those commits. Missing commits are fetched with `git fetch origin <sha>`.
2. **Worktree.** `git worktree add --detach` at head, in a temp folder.
3. **Today's standards.** `CLAUDE.md`, `REVIEW.md` and `.claude/rules/` are
   replaced with their versions at `--standards-ref` (files missing there are
   removed), so an old change is judged by today's rules. Their blob SHAs are
   recorded, and the `run_id` ends with the first 7 characters of the REVIEW.md
   blob SHA (or `none`). The overlaid files are marked `skip-worktree`, so they
   do not show up as part of the change. A symlink at the standards ref (for
   example `CLAUDE.md -> AGENTS.md`, or `.claude/rules -> docs/rules`) is
   resolved inside the repo at that commit: the target's content is written in
   place of the link and the target's blob SHA is recorded. A link that points
   outside the repo, dangles or loops is left out with a warning.
4. **Review.** `claude -p "/factory:review-pr <base> --json"` in the worktree,
   with `--plugin-dir`, `--permission-mode dontAsk`, read-only allowed tools,
   and `Edit`/`Write`, web tools, all of `gh`, `curl`, `wget` and git writes
   denied. It also passes `--strict-mcp-config` (no MCP servers) and
   `--setting-sources user` (the case head's `.claude/settings.json`, with its
   allow rules and hooks, is not loaded). It never passes `--comment`.
5. **Check the target.** The review's `head` must be the case head and its
   `base` the case base or `git merge-base <base> <head>` (7+ character
   abbreviations are fine). Otherwise the case is an **error**, not a miss, and
   the review's `summary` goes into the error: this is how the skill reports a
   fatal problem (bad ref, a git command refused, ...), with empty findings.
6. **Judge.** A second `claude -p` call with no tools gets the known problem and
   the findings and answers `{found, matched_finding_ids, reasoning}`. A
   `found: true` that names no finding id from this review counts as not found.
7. **Clean up.** The worktree is removed (unless `--keep-worktrees`), even when
   the case failed.

A failing case records its `error` and `found: null`; the run continues.

## After a run

The results file lists every finding per case. For the Stage 1 noise gate
(fewer than 1 in 5 other findings are noise), the team fills
`other_findings_verdicts` for each case: finding id to `"useful"` or `"noise"`.

## Tests

```sh
python3 -m unittest tests.test_run_evals -v
```

The tests use a fake runner and a throwaway git repo; they make no network,
`gh` or `claude` calls.

## Known limits

- Every `claude` flag is in one block at the top of `run_evals.py`. `--max-turns`
  is not used because the local 2.1.319 `--help` does not list it; `--timeout`
  bounds each call instead.
- With `--json-schema` the CLI returns the answer in `structured_output`; when
  that field is missing the runner parses the `result` text instead (code fences
  and surrounding prose are tolerated).
- Only the root `CLAUDE.md` and `REVIEW.md` are overlaid. A subdirectory
  `CLAUDE.md` or `REVIEW.md` is reviewed as it was at the case's head.
- The read-only guarantee is what the deny list, `--strict-mcp-config` and
  `--setting-sources user` enforce. A user-level allow rule for some other
  command that can reach GitHub (a wrapper script, for example), or a hook in
  the user's own settings, is not blocked. Run evals from an account whose
  user settings do not allow such commands.
- `--setting-sources user` drops project settings for the review call; the
  skill reads `CLAUDE.md` and `REVIEW.md` itself, so the review still sees
  them.
