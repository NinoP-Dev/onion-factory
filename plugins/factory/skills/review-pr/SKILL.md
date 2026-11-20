---
name: review-pr
description: Review a pull request or the current branch against the repo's CLAUDE.md, REVIEW.md and path-scoped .claude/rules files, grading findings Important / Nit / Pre-existing. Optionally post the findings to the PR as one COMMENT review with inline comments (--comment), or print only a JSON result for the eval runner (--json). Never approves, never edits code.
when_to_use: Stage 2 hand review. The author runs it on the branch before opening a PR, then again with the PR number and --comment to post findings. The eval runner runs it with a base ref and --json.
argument-hint: "[PR number | base ref] [--comment] [--json]"
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(git rev-parse *), Bash(git branch --show-current), Bash(git symbolic-ref --short refs/remotes/origin/HEAD), Bash(git remote get-url *), Bash(git merge-base *), Bash(git diff *), Bash(git log *), Bash(git show *), Bash(git grep *), Bash(git ls-files *), Bash(git cat-file *), Bash(git ls-tree *), Bash(git fetch origin *), Bash(git status *), Bash(gh pr view *), Bash(gh pr diff *), Bash(gh repo view *)
---

# Review a change against REVIEW.md

You are the review agent. You comment; a person approves. Arguments: `$ARGUMENTS`

Hard rules for the whole run:
- Read-only. Never edit, create or delete files in the repo, never commit, push, check out, stash or reset, never run the project's build or tests. The only write allowed is the single GitHub review in step 6, and only when `--comment` was given.
- Never submit a review with event `APPROVE` or `REQUEST_CHANGES`. Only `COMMENT`.
- Report only problems you can demonstrate. A short list of real findings beats a long list with noise; the team's gate is fewer than 1 false finding in 5.

## 1. Parse arguments and resolve the target

Split `$ARGUMENTS` on whitespace. `--json` sets JSON mode. `--comment` sets comment mode. The first other token is the target:
- Digits, `#digits`, or a `https://github.com/<o>/<r>/pull/<n>` URL: **PR mode** with that PR number. For a URL, compare `<o>/<r>` (case-insensitive) with `gh repo view --json nameWithOwner -q .nameWithOwner`; if they differ, stop with an error saying the review needs a local clone of `<o>/<r>`. Never review a same-numbered PR of another repo.
- Any other token: **base-ref mode** with that ref. Check it with `git rev-parse --verify <ref>^{commit}`; if that fails, stop and say the ref was not found.
- Nothing: **branch mode**.
Ignore other tokens and say so in the summary.

Record these values; you need them for the output:

| Field | PR mode | Base-ref mode | Branch mode |
|---|---|---|---|
| repo | `nameWithOwner` from the PR URL | `gh repo view --json nameWithOwner -q .nameWithOwner`; on failure parse `owner/name` from `git remote get-url origin`; else `local/<name of the git rev-parse --show-toplevel directory>` | same as base-ref |
| pr | the number | `null` | `null` |
| head | `headRefOid` | `git rev-parse HEAD` | `git rev-parse HEAD` |
| base (diff base) | `git merge-base <baseRefOid> <headRefOid>` once both commits are local; `baseRefOid` only if merge-base fails (shallow clone) | `git merge-base <ref> HEAD` | `git merge-base <default> HEAD` |
| standards ref | `baseRefOid` (the base branch tip) | working tree | working tree |
| diff | `gh pr diff <n> --color=never` | `git diff --no-color <base> HEAD` | `git diff --no-color <base>` (includes uncommitted tracked changes) |
| ticket | Jira key in the PR title, else in `headRefName` | Jira key in `git branch --show-current` | same as base-ref |

- PR mode: run `gh pr view <n> --json number,title,url,state,headRefName,baseRefName,headRefOid,baseRefOid`. Make both commits local before anything else (fetching only adds objects; never fetch into a local branch):
  - `git cat-file -e <baseRefOid>^{commit}`; if it fails run `git fetch origin <baseRefName>` and check again.
  - `git cat-file -e <headRefOid>^{commit}`; if it fails run `git fetch origin pull/<n>/head` and check again.
  - If either commit is still missing, stop with an error (`base commit <sha> not found locally` or the head equivalent). Do not fall back to the factory defaults.
  To read code at the PR head: if `git rev-parse HEAD` equals `headRefOid`, read the working tree; otherwise read files with `git show <headRefOid>:<path>` and search with `git grep -n <pattern> <headRefOid>`.
- Base-ref mode reads only committed changes, so files laid over the working tree by the eval runner do not count as part of the change.
- Branch mode: the default branch is `git symbolic-ref --short refs/remotes/origin/HEAD`; if that fails use `gh repo view --json defaultBranchRef -q .defaultBranchRef.name` prefixed with `origin/`, then `origin/main`, then `main`. Also list untracked files with `git ls-files --others --exclude-standard` and review them as new files. If the branch is the default branch and there is no diff, say there is nothing to review and stop.
- Jira key regex: `[A-Z][A-Z0-9]+-\d+`, first match, case-sensitive. None found: `ticket` is `null`.
- `--comment` needs PR mode. Without a PR number, review normally, do not post, and say in the summary: `--comment needs a PR number: /factory:review-pr <n> --comment`.

Get the changed file list before reading the full diff: in PR mode `gh pr diff <n> --name-only` (or `git diff --no-color --name-status <base> <headRefOid>`; `gh pr diff` has no `--stat`/`--name-status`); in the other modes add `--name-status` to the diff command from the table. Skip lock files, vendored and generated files unless the standards say otherwise.

## 2. Read the standards

Always read these, even if some are already in context. In PR mode read them at the standards ref, `baseRefOid`, the tip of the base branch as the managed review service does (`git show <baseRefOid>:<path>`; list `.claude/rules` with `git ls-tree -r --name-only <baseRefOid> -- .claude/rules`), so a PR cannot change its own rules and a REVIEW.md change merged after the branch was cut still applies. Do not read them at the merge-base. In the other modes read them from the working tree.

In PR mode a file is missing only when `git cat-file -e <baseRefOid>:<path>` fails after step 1 confirmed the commit exists. Any other failure of `git show` or `git cat-file` is an error: stop and report it; never treat it as a missing REVIEW.md.
1. `CLAUDE.md` at the repo root, plus any `CLAUDE.md` in a directory that contains a changed file or is a parent of one.
2. `REVIEW.md` at the repo root, plus any `REVIEW.md` in a parent directory of a changed file. A line that is only `@<file>` includes that root-level file; read it too.
3. Every file under `.claude/rules/` (recursively). A rule applies when it has no `paths:` frontmatter, or when any of its `paths` globs matches a changed file. Record the applied rules' repo-relative paths for `standards.rules`.

Set `standards.claude_md` and `standards.review_md` to whether a root file of that name was found and read. If REVIEW.md is missing, set `review_md` false, start the summary with `No REVIEW.md found; reviewed against CLAUDE.md and the factory defaults.`, and use `${CLAUDE_SKILL_DIR}/references/default-standards.md` for the three sections below. If the change itself edits CLAUDE.md, REVIEW.md or `.claude/rules`, say so in the summary.

From REVIEW.md take three sections: `## Always check` (rules to enforce), `## Do not flag` (never report these, even if true), `## Severity` (how to grade). If a section is missing, use the matching part of the defaults file.

## 3. Review in full context

For each changed file, read the whole hunk and enough of the surrounding file to understand it, then find the callers and users of every changed function, type, config key or query with Grep or `git grep`. Look for:
- every `Always check` rule and every applicable CLAUDE.md or `.claude/rules` instruction;
- correctness: wrong conditions, off-by-one, null or empty input, error paths, concurrency, data loss, broken contracts with callers;
- security: injection, missing auth or permission checks, secrets, unsafe input handling.

Grade each candidate by the `Severity` section:
- **Important**: a bug, a security hole, or a break for the core project. Must be fixed before merge.
- **Nit**: worth fixing, not blocking. Keep at most 5 per review: the most valuable ones.
- **Pre-existing**: a real problem in code this change did not touch. It becomes a Jira ticket, not a merge blocker.

Drop anything listed in `Do not flag`, anything a linter, formatter, type checker or CI already enforces, style preferences not written in the standards, and speculation about code you have not read.

## 4. Verify every candidate

Before reporting a candidate, re-open the exact code at the head commit and confirm:
1. The line number is correct in the new version of the file (right side of the diff). For a problem caused by a deleted line, anchor it at the nearest remaining line it affects.
2. You can name a concrete input, state or call sequence that produces the wrong behaviour, and what happens. Put that in the body.
3. Nothing nearby (a guard, a caller's check, a test, a type) already prevents it.
4. It is not a duplicate of another finding. Merge duplicates.
If any check fails, drop the candidate. Do not report "might", "consider" or "ensure" findings with no demonstrated failure.

Then order the findings Important, then Nit, then Pre-existing, by file and line within each group, and number them `F1`, `F2`, ... in that order.

Each finding has: `id`, `severity`, `file` (repo-relative), `line`, optional `end_line`, `title` (one-line claim, at most 120 characters), `body` (what goes wrong, for which input or state, and the smallest fix; Markdown), `rule` (the REVIEW.md or CLAUDE.md rule it enforces, quoted briefly; omit when none applies).

## 5. Output

- Default: the readable Markdown report in `${CLAUDE_SKILL_DIR}/references/output-format.md`.
- `--json`: your final message is ONLY one JSON object matching the factory review-result schema: no prose before or after, no code fences. The field rules and an example are in `${CLAUDE_SKILL_DIR}/references/output-format.md`. Read it before writing the JSON. The eval runner parses this message directly. In JSON mode, do all the work silently and print nothing but the object at the end; on a fatal error (bad ref, PR not found, PR of another repo, base or head commit missing) still print a valid object with empty `findings`, the error in `summary`, `base`/`head` set to whatever you resolved (the ref text as given, or `unknown`), and `repo` set to whatever you resolved, or `local/unknown` if nothing was resolved yet. `repo` must always look like `owner/name`; never write a bare `local` or `unknown`.

## 6. Post (only with --comment in PR mode)

Read `${CLAUDE_SKILL_DIR}/references/posting.md` and follow it exactly: one `POST /repos/{owner}/{repo}/pulls/{n}/reviews` with `event: "COMMENT"`, `commit_id` = head SHA, one inline comment per finding whose line is in the diff (RIGHT side), every other finding in the review body, every comment starting with the factory finding marker, the body starting with the factory review marker. Set `posted` to true only when GitHub returned the created review. Post before printing the output, so the output can report the review URL (Markdown) or `posted` (JSON).

If the PR is closed or merged, do not post; say so.
