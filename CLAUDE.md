# onion-factory

Tooling for the team's AI code-review rollout: a Claude Code plugin (`factory`), an eval runner,
a weekly scorecard and a GitHub Actions template. README.md maps each piece to its plan stage.

## Layout

- `.claude-plugin/marketplace.json`: marketplace `onion-factory`, one plugin `factory` at `./plugins/factory`.
- `plugins/factory/.claude-plugin/plugin.json`: the plugin manifest. Only plugin.json goes in `.claude-plugin/`.
- `plugins/factory/skills/<name>/SKILL.md`: `review-pr`, `log-review` and `bootstrap-repo`, invoked as `/factory:<name>`.
  - `review-pr/references/`: output format, posting steps and default standards.
  - `log-review/references/logrow.py` and `test_logrow.py`: the row builder and its tests.
  - `bootstrap-repo/templates/`: the CLAUDE.md, REVIEW.md, PR template and CODEOWNERS that get copied into project repos.
- `schemas/*.schema.json`: the shared contracts (review-log-row, finding, review-result, eval-case, eval-result).
- `evals/review/run_evals.py`: the eval runner. Cases are in `evals/review/cases/<project>/<id>.json` and results in `evals/review/results/<project>/<run_id>.json`.
- `scorecard/scorecard.py`: reads the Review Logs CSV export and prints the measures and gates.
- `templates/github/claude-review.yml`: Stage 3 fallback workflow. It is a template and never runs in this repo.
- `tools/validate.py`: checks the schemas, eval data and plugin layout.
- `tests/`: unittest suites. `tests/test_log_review.py` loads log-review's tests from the plugin folder.

## Commands

- All checks (what CI runs): `python3 tools/validate.py && python3 -m unittest discover -s tests -v`
- One suite: `python3 -m unittest tests.test_scorecard -v` (or `test_run_evals`, `test_validate`, `test_log_review`)
- Plugin manifests: `claude plugin validate .` and `claude plugin validate plugins/factory`
- Eval without model calls: `python3 evals/review/run_evals.py --project <p> --repo-path <clone> --dry-run`
- Try the plugin locally: `claude --plugin-dir ./plugins/factory`

## Rules

- Python 3.9 compatible. Start every Python module that has code with `from __future__ import annotations`. Do not use
  `match` statements, or `X | Y` types where they are evaluated at runtime (`isinstance`, casts,
  dataclass fields read by code); use `Optional`/`Union` there.
- Runtime code uses the standard library only. `jsonschema` is optional, and every use of it
  needs a built-in fallback.
- The schemas are contracts. Never change a file in `schemas/` without updating every reader and
  writer in the same change: `logrow.py` (it embeds a copy of the row schema, and its tests check
  the copy), `run_evals.py`, `scorecard.py`, `validate.py`, the review-pr output format and the
  Action template.
- The finding markers are a contract too. `<!-- factory:finding id=F<n> severity=<S> -->` is the
  first line of each finding, and `<!-- factory:review head=<sha> findings=<n> -->` is the first
  line of the review body. review-pr writes them, `claude-review.yml` writes them, and log-review
  parses them. Change all three together.
- Skill names and `/factory:<name>` invocations appear in the README, the runner, the PR template
  and the other skills. Rename them everywhere at once.
- Never commit real sheet data: no Review Logs rows, reviewer names or notes. Fixtures use fake
  people and fake repos (`acme/widgets`, `acme/gears`).
- Nothing here approves, requests changes, merges or pushes. Keep it that way: the agent
  comments and a person approves.
- Tests make no network, `gh` or `claude` calls. Use the fake runners the suites already have.
