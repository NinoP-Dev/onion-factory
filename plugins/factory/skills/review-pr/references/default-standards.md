# Factory default review standards

Use these only for the parts the repo does not define: when REVIEW.md is missing, or when it lacks one of the three sections. CLAUDE.md and `.claude/rules` instructions still apply on top. Tell the user in the summary that defaults were used; a missing REVIEW.md is a Stage 1 gap.

## Always check
- The change does what the PR title and description say, and nothing silently different.
- Input from users, requests, files, env vars and external APIs is validated before use; no SQL, shell, path or template injection.
- Every new or changed endpoint, handler or job checks authentication and permission the same way its neighbours do.
- No secrets, tokens, credentials or personal data in code, logs, error messages or test fixtures.
- Error paths: failures are handled or propagated, not swallowed; resources (files, connections, locks, transactions) are released on every path.
- Null, empty, zero, negative, duplicate and very large inputs; off-by-one at boundaries; time zones and date parsing.
- Callers of every changed function, type, schema, config key, API response or database column still work with the new behaviour.
- Database migrations are reversible or safe to run on live data, and code tolerates the old and new schema during deploy.
- Concurrency: shared state, retries and idempotency of jobs, webhooks and payment-like operations.
- New behaviour that can break users has a test; a test that was changed to pass still tests the original intent.

## Do not flag
- Formatting, naming, import order and anything a linter, formatter, type checker or CI step already enforces.
- Style or structure preferences that are not written in CLAUDE.md, REVIEW.md or `.claude/rules`.
- Missing comments or docstrings, unless a rule requires them.
- "Consider adding tests" with no specific untested behaviour that can break.
- Hypothetical problems you cannot tie to a concrete input or state.
- Generated files, lock files, vendored code and snapshots.
- Suggestions to refactor code the change did not touch.

## Severity
- **Important**: a bug, a security hole, or a break for the core project or its callers. Fix before merge.
- **Nit**: worth fixing, not blocking; the author decides. At most five per review: keep the most valuable.
- **Pre-existing**: a real problem in code this change did not introduce. Report it so it becomes a Jira ticket; it does not block the merge.
