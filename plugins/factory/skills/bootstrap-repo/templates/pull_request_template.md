<!--
Title: start with the Jira key, e.g. "ABC-123: Short summary of the change".
Keep the PR small: one concern, reviewable in one sitting.
-->

## What changed

<!-- What and why, in a few lines. Link the Jira ticket. -->

Ticket: <ABC-123>

## How it was tested

<!-- Commands you ran, cases you tried by hand, and what you did not test. -->

## Review checklist

- [ ] Title starts with the Jira key
- [ ] The all-checks command from CLAUDE.md passes locally
- [ ] Ran `/factory:review-pr` on the branch and fixed what applied
- [ ] Ran `/factory:review-pr <PR number> --comment` and answered every finding (fixed, or resolved with one line why not)
