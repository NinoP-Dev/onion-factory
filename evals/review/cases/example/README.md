# Example eval case

`bulk-discount-double-applied.json` is made up: `acme/widgets` and PR 101 do not
exist. It shows the shape of a case; running it fails at `gh pr view`. Real
cases go in `evals/review/cases/<project>/`, one folder per project, one file
per case, named `<id>.json`. The schema is
[`schemas/eval-case.schema.json`](../../../../schemas/eval-case.schema.json).

## Picking the ten cases

A project's eval set is ten past changes where the team already knows what was
wrong. A change qualifies when either:

- **a bug reached users** (`"source": "bug-reached-users"`): it shipped in that
  change and was found afterwards (incident, support ticket, hotfix); or
- **a person caught what the agent missed** (`"source": "human-only-finding"`):
  its Review Logs row has a number above 0 in "Findings: Human Only".

For each one:

1. Prefer a merged PR whose merge commit holds the whole change (merge commit
   or squash merge): set `pr`. For a rebase merge, or a change with no PR, set
   `base` and `head` to full commit SHAs instead. Never set both.
2. Write `known_problem.summary` specifically enough that a judge reading only
   the findings can tell whether one of them is this problem: the function, the
   wrong behaviour, and an input that shows it. "Pricing bug" is too vague.
3. List `known_problem.files` (repo-relative) and the `severity` REVIEW.md would
   give it (`Important` unless it is a Nit).
4. Put the evidence in `reference`: a Jira key, an incident link, or the
   Review Logs PR cell. Set `added` to today's date.
5. Use a short kebab-case `id` and save the file as `<id>.json`.

Pick changes that are small enough to review (a few hundred lines), spread
across the parts of the code that matter, and not all the same kind of
problem. Keep at least ten; replace a case only when its change can no longer
be fetched.

Every miss found in the Stage 2 weekly tuning joins the set as a new case
(`"source": "human-only-finding"`). Rerun the set every time REVIEW.md changes.

Do not copy real names, customer data, or Review Logs notes into a case beyond
what the summary needs.
