# Review rules

<!-- Team-wide part: everything above "## This project" is shared by every project.
Change it in onion-factory (plugins/factory/skills/bootstrap-repo/templates/REVIEW.md)
and copy it out, so all projects stay in step. Rerun the review eval set every
time this file changes. -->

These rules are for the review agent. It comments; a person approves. Report a
finding only when you can name the input or state that makes the code go wrong.

## Always check

- Behaviour change without a test that would fail on the old code.
- Input from users, files, queues or other services used without validation at the boundary.
- New or changed endpoints, jobs and queries: who may call them, and whose data they can read or change.
- Secrets, tokens or personal data written to code, config, logs or error messages.
- Errors swallowed, retried forever, or turned into success; missing timeouts on network calls.
- Database or schema changes that break the running version during deploy, or cannot be rolled back.
- Null, empty, zero, duplicate and boundary values; time zones and date parsing.
- Concurrency: shared state, check-then-act, missing transactions or locks.
- Anything CLAUDE.md lists under "Conventions a newcomer gets wrong" or "Gotchas".

## Do not flag

- Formatting, import order, naming taste, or anything the linters and CI already check.
- Generated, vendored or lock files.
- Missing comments or docs on internal code.
- Refactors, abstractions or "consider using X" with no bug behind them.
- Speculative performance concerns without a realistic input size.
- Style of test code, unless the test cannot fail.

## Severity

- **Important**: a bug, a security hole, or a break for the core project. Fix before merge,
  unless the reviewer agrees the finding is wrong.
- **Nit**: worth fixing, not blocking; the author's call. At most five per review; keep the
  five that matter most.
- **Pre-existing**: a real problem this change did not introduce. Do not block the PR; it
  becomes a Jira ticket.

## This project

<!-- Project owner: add rules only this project needs, each one learned from a real
miss (a Human Only finding in Review Logs or a bug that reached users). Each rule
should also become an eval case. Delete the examples you do not use. -->

- <Rule, stated as what to check and why it matters here.>
<!-- If the core project depends on this repo, keep this rule and list the paths in
CLAUDE.md under "Connection points with the core project":
- Any change to a path listed in CLAUDE.md under "Connection points with the core
  project" is **Important** when it changes behaviour the core project relies on
  (signatures, payloads, schemas, config keys, error codes). Name the core caller
  that would break.
-->
