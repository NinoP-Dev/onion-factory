---
name: bootstrap-repo
description: Check a project repo's Stage 0 groundwork for AI code review (CLAUDE.md under 200 lines ending with the REVIEW.md pointer, one all-checks command run by CI on pull requests, main-branch ruleset, PR template, .gitignore entry, secrets hygiene, plus REVIEW.md and CODEOWNERS for core connection points) and print a gate table. Without --check, offers to create the missing files from templates as local changes only. Never commits, pushes or changes GitHub settings.
when_to_use: Stage 0 on the lighthouse project, and Stage 4 when each new project joins the rollout. Run from inside the project repo. Use --check for a read-only gate check.
argument-hint: "[--check]"
disable-model-invocation: true
allowed-tools: Read, Grep, Glob, Bash(git rev-parse *), Bash(git ls-files *), Bash(git check-ignore *), Bash(git cat-file *), Bash(git show origin/*), Bash(git log --all --format=*), Bash(git symbolic-ref --short refs/remotes/origin/HEAD), Bash(git remote get-url *), Bash(gh repo view *), Bash(gh run list *), Bash(gh pr list *), Bash(grep *), Bash(command -v *)
---

# Bootstrap a repo for AI review (Stage 0 / Stage 4)

Arguments: `$ARGUMENTS`

If the arguments contain `--check`, run in **CHECK mode**: gather facts (step 2), print the gate table (step 4), stop. Write nothing.
Otherwise run in **SETUP mode**: gather facts, print the table, propose local changes, apply only the ones the user approves (step 3), then print the table again.

Templates live in `${CLAUDE_SKILL_DIR}/templates/`: `CLAUDE.md`, `REVIEW.md`, `pull_request_template.md`, `CODEOWNERS.example`.

Hard rules for the whole run:
- Local file changes only, and only after the user's yes in this conversation. Never commit, stage (`git add`, `git rm`), stash, reset, check out, create branches or push.
- Never change GitHub: no `gh api` call with `-X POST|PUT|PATCH|DELETE`, `-f`, `-F`, `--field`, `--raw-field` or `--input` (fields silently switch `gh api` to POST), no `gh repo edit`, no `gh ruleset`, no secrets or settings commands. Every `gh` call in this skill is a GET. For rulesets and CI, print the steps for the user.
- Never overwrite an existing file. Existing files get appends or small edits, shown as the exact text first.
- Run git only in the exact forms written below. Never pass `--output` (or `-o`) to `git log` or `git show` (it writes a file), and never give `git symbolic-ref` a second argument (that repoints HEAD).
- Never invent CI, commands or owners. Anything written into CLAUDE.md, CODEOWNERS or the table must come from a repo file or from the user.
- Never print secret values. Report secrets-scan results as counts and `file:line` only.

## 1. Locate the repo

Run `git rev-parse --show-toplevel`. If it fails, stop: "Run /factory:bootstrap-repo from inside the project repo." Use that directory as the root for every path below.
If the root contains `plugins/factory/.claude-plugin/plugin.json`, this is the onion-factory repo itself: stop and say to run the skill from a project repo.

Run `gh repo view --json nameWithOwner,defaultBranchRef,viewerPermission,visibility`. Record `REPO` (nameWithOwner), `BRANCH` (defaultBranchRef.name), `PERM` (viewerPermission).
If it fails (not on GitHub, or gh not signed in), set `GITHUB=unavailable`, take `BRANCH` from `git symbolic-ref --short refs/remotes/origin/HEAD` (strip `origin/`; fall back to `main`), and mark every row that needs the GitHub API as `unknown (GitHub not reachable)`.

Shell variables do not persist between commands: in every command below, write the recorded values of `REPO` and `BRANCH` in place of `$REPO` and `$BRANCH`.

## 2. Gather the facts (both modes)

Work through items 2.1 to 2.9. Record a status for each table row: `pass`, `fail` or `unknown`, plus a short detail. Item 2.7 can also be `n/a`.

### 2.1 CLAUDE.md
- Present: `CLAUDE.md` exists at the root. If only `.claude/CLAUDE.md` exists, mark `fail` (managed Code Review reads root CLAUDE.md; move it).
- Lines (working tree): `grep -c '' CLAUDE.md`. The "CLAUDE.md present, under 200 lines" row passes only when the file is at the root and this count is 199 or fewer; at 200 or more mark it `fail (<n> lines)` so SETUP proposes cuts, even if the merged copy is short or not merged yet.
- Merged on the default branch (gate row): first `gh api "repos/$REPO/contents/CLAUDE.md?ref=$BRANCH" --jq .path`. HTTP 404 means not merged: `fail`. Only if that succeeds, count the merged lines with `gh api -H "Accept: application/vnd.github.raw" "repos/$REPO/contents/CLAUDE.md?ref=$BRANCH" | grep -c ''` (do not skip the first call: on a 404 this one prints the error body and still returns a count). Without GitHub use `git cat-file -e "origin/$BRANCH:CLAUDE.md"` then `git show origin/$BRANCH:CLAUDE.md | grep -c ''`, and say "as of last fetch". Pass when merged and 199 lines or fewer.
- Reviews pointer: `grep -n '^## ' CLAUDE.md` and `grep -n -A2 '^## Reviews' CLAUDE.md`. Pass when `## Reviews` is the last `## ` heading and a line within the next two reads `Before any code review, read REVIEW.md.`
- All-checks command: Read the file's Commands section (or the whole file if it has none). Record `ALL_CHECKS` only when CLAUDE.md itself names one command that runs all checks (tests, lint, types). Do not pick one from package.json or a Makefile for this row; you may list candidates in the fix text.

### 2.2 REVIEW.md (Stage 1 row; not part of the Stage 0 gate)
- Present at the root.
- `grep -n '^## ' REVIEW.md` shows `## Always check`, `## Do not flag`, `## Severity` and `## This project`.
- The Severity section names Important (fix before merge), Nit (at most five per review) and Pre-existing (becomes a Jira ticket).
- `grep -c '' REVIEW.md` is about one page (80 lines or fewer; over that, report it in the detail, not as a fail).
- Merged: `gh api "repos/$REPO/contents/REVIEW.md?ref=$BRANCH" --jq .path` succeeds. Managed Code Review reads REVIEW.md only from the base branch.

### 2.3 PR template
- Find it: `git ls-files --cached --others --exclude-standard | grep -i 'pull_request_template'` (GitHub accepts `.github/`, the root or `docs/`, any case, or `.github/PULL_REQUEST_TEMPLATE/*.md`).
- Pass when one exists and asks for each of: Jira key in the title, What changed, How it was tested, ran `/factory:review-pr`, answered every finding. Check with `grep -i -n -e jira -e 'what changed' -e 'tested' -e 'review-pr' -e 'finding' <file>`. Name the missing parts in the detail.

### 2.4 .gitignore entry
- Run `git check-ignore -v --no-index .claude/settings.local.json`. Output is `<source>:<line>:<pattern><TAB><path>`.
- Pass only when it prints a line whose `<source>` is a `.gitignore` file inside the repo and whose `<pattern>` does not start with `!`.
- Fail when there is no output, when the pattern starts with `!` (un-ignored), or when the source is a global excludes file or `.git/info/exclude` (other clones would not ignore it; say so).

### 2.5 CI
- Workflows: `git ls-files --cached --others --exclude-standard .github/workflows`. Then `grep -l -E 'pull_request' <those .yml/.yaml files>`. Read each hit and keep it only if `pull_request` or `pull_request_target` is a trigger under the top-level `on:` key (not text in an `if:` or a comment). Record `PR_WORKFLOWS`.
- Other CI: `git ls-files | grep -E '^(\.gitlab-ci\.yml|Jenkinsfile|bitbucket-pipelines\.yml|azure-pipelines\.yml|\.travis\.yml|\.circleci/|\.buildkite/)'`. If found and `PR_WORKFLOWS` is empty, mark the CI rows `unknown (CI outside GitHub Actions: <file>; confirm by hand it runs on every PR)`.
- CI on pull_request (gate row): pass when `PR_WORKFLOWS` is non-empty.
- CI runs the all-checks command (gate row): pass when `ALL_CHECKS` is set and `grep -n -F -- "<ALL_CHECKS>" <PR_WORKFLOWS>` finds it. When CI runs the same checks as separate steps, mark `unknown` and say which steps you saw.
- Recent results (detail only): `gh run list --branch "$BRANCH" --limit 5 --json workflowName,event,conclusion,createdAt`. Mention failures on the default branch.
- Check names seen on PRs (used in 2.6): `gh pr list --state all --limit 5 --json number,statusCheckRollup --jq '[.[].statusCheckRollup[] | (.name // .context)] | unique | .[]'`. Record `CHECK_NAMES`.
- Never write or edit a workflow file. If CI is missing, the fix text is: "Add a GitHub Actions workflow on `pull_request` that runs `<ALL_CHECKS or the command the owner chooses>`; name that command in CLAUDE.md under Commands."

### 2.6 Main-branch ruleset (read-only)
Rules that apply to the default branch, from all active rulesets (repo and org):
```bash
gh api "repos/$REPO/rules/branches/$BRANCH?per_page=100" --jq '{
  pr_required: (map(select(.type=="pull_request")) | length > 0),
  approvals: ([.[] | select(.type=="pull_request") | .parameters.required_approving_review_count] | max // 0),
  thread_resolution: ([.[] | select(.type=="pull_request") | .parameters.required_review_thread_resolution] | any),
  required_checks: [.[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context],
  code_owner_review: ([.[] | select(.type=="pull_request") | .parameters.require_code_owner_review] | any)
}'
```
Rulesets and their enforcement (detail only; `evaluate` and `disabled` rulesets do not block merges, say so):
```bash
gh api "repos/$REPO/rulesets?includes_parents=true&per_page=100" --jq '.[] | [.name, .enforcement, .source_type, .target] | @tsv'
```
Classic branch protection (needs admin to read):
```bash
gh api "repos/$REPO/branches/$BRANCH/protection" --jq '{
  pr_required: (.required_pull_request_reviews != null),
  approvals: (.required_pull_request_reviews.required_approving_review_count // 0),
  thread_resolution: (.required_conversation_resolution.enabled // false),
  required_checks: ((.required_status_checks.checks // []) | map(.context)),
  code_owner_review: (.required_pull_request_reviews.require_code_owner_reviews // false)
}' 2>&1
```
Read the classic result this way: a body containing `Branch not protected` means no classic protection; any other 404 or a 403 means you cannot read it (no admin rights), so classic protection is unknown. A 403 on the rulesets calls that mentions upgrading the plan means rulesets cannot be enforced on this private repo's plan; say so.

Four gate rows. Each passes when the rules endpoint OR classic protection satisfies it:
- Ruleset: PR required (`pr_required`).
- Ruleset: 1 approval (`approvals >= 1`).
- Ruleset: required checks (`required_checks` non-empty). If a required check name is not in `CHECK_NAMES`, add to the detail: "required check `<name>` was not seen on recent PRs; PRs will wait on it forever if no job reports that name."
- Ruleset: conversations resolved (`thread_resolution`). This is what enforces "no PR merged with an unanswered finding".

When a row fails only because classic protection could not be read and the rules endpoint shows nothing, mark it `unknown (needs a repo admin to confirm classic branch protection)` rather than `fail`.

### 2.7 Core connection points (Stage 4; only for projects the core depends on)
- Applies when CLAUDE.md has `## Connection points with the core project` with at least one real entry (not a `<placeholder>`), or the user says the core project depends on this repo. Otherwise mark the row `n/a` (in SETUP mode, ask once: "Does the core project depend on this repo?").
- When it applies, all of these must hold:
  - CODEOWNERS exists at `.github/CODEOWNERS`, `CODEOWNERS` or `docs/CODEOWNERS`, and every connection-point path from CLAUDE.md has a line with an owner.
  - REVIEW.md has a rule that changes to the connection points are Important: `grep -n -i 'connection point' REVIEW.md` and the matching rule says Important. A match inside an HTML comment (`<!-- ... -->`) does not count: the template ships this rule commented out. Read REVIEW.md around the match; the rule must be an active list item under `## This project`.
  - `code_owner_review` from 2.6 is true (otherwise CODEOWNERS never blocks a merge).
  - No CODEOWNERS errors on the default branch: `gh api "repos/$REPO/codeowners/errors?ref=$BRANCH" --jq '.errors[] | "\(.path):\(.line) \(.kind): \(.message)"'`. A 404 means no CODEOWNERS is merged yet.

### 2.8 Secrets
- settings.local.json not tracked: `git ls-files --error-unmatch .claude/settings.local.json`. Exit 0 means tracked: `fail`. Fix text: "Run `git rm --cached .claude/settings.local.json`, commit it, and rotate any keys or tokens it held."
- Also run `git log --all --format='%h %ad' --date=short -- .claude/settings.local.json` and, if it prints anything, add: "settings.local.json is in git history; rotate anything secret it ever held. Rewriting history is the owner's call."
- Secrets scan: `command -v gitleaks`. In CHECK mode do not run it; mark `unknown (run: gitleaks git --redact --no-banner .)`. If gitleaks is missing, mark `unknown (install gitleaks, e.g. brew install gitleaks, then run: gitleaks git --redact --no-banner .)`. In SETUP mode the scan is an optional proposal item (step 3).

### 2.9 Manual gate items
- Newcomer test (gate row): `unknown` unless the user tells you it was done. Fix text: "Someone new (or a fresh clone in a temp directory) follows only CLAUDE.md from clone to passing `<ALL_CHECKS>`. Anything they had to ask goes into CLAUDE.md."
- Review time baseline: `unknown` unless the user gives it (the window dates and the median). Fix text: "Pick the pre-AI date window and record it. The baseline is the scorecard's median per PR over that window: from the onion-factory repo, export the Review Logs tab as CSV and run `python3 scorecard/scorecard.py --csv <export.csv> --repo <REPO> --baseline-from <YYYY-MM-DD> --baseline-to <YYYY-MM-DD>`. Time is summed per PR (two reviewer rows for one PR add up); non-numbers count as missing, never as 0. Use the same window for the Stage 3 gate."

## 3. SETUP mode: propose, confirm, apply

Skip this step in CHECK mode.

1. Print the gate table (step 4).
2. Build a numbered proposal from failing rows that a local file change can fix. Format each line as `N. <create|append|edit> <path> — <what, in one line>`. Use only these actions:
   - **CLAUDE.md missing**: do not write it by default. Tell the user: "Run `/init`, trim the result to under 200 lines using the sections in the bootstrap template (What this is, Layout, Commands, Conventions a newcomer gets wrong, Gotchas, Connection points with the core project, Reviews), then rerun `/factory:bootstrap-repo`." Offer one alternative proposal item: "create CLAUDE.md — draft from the template by reading the repo".
   - **CLAUDE.md drafted from the template** (only if approved): Read README*, the package or build manifests (package.json scripts, Makefile, pyproject.toml, go.mod, Cargo.toml, composer.json, Gemfile, build.gradle, *.csproj), `.github/workflows/*`, and `git ls-files | cut -d/ -f1 | sort | uniq -c`. Fill only facts you found in those files; every command you write must appear in one of them. Delete sections you cannot fill truthfully, except keep one `<placeholder>` line under Conventions and under Gotchas for the owner. Remove the template's leading HTML comment. Keep `## Reviews` / `Before any code review, read REVIEW.md.` as the last two lines.
   - **CLAUDE.md 200 lines or more**: do not trim on your own. Propose specific cuts (sections that repeat the README, the code or linter config), each as its own item.
   - **Reviews pointer missing or not last**: append (or move to the end) exactly these two lines:
     ```
     ## Reviews
     Before any code review, read REVIEW.md.
     ```
   - **REVIEW.md missing**: create it from `${CLAUDE_SKILL_DIR}/templates/REVIEW.md` unchanged, leaving the `## This project` placeholder for the owner. **REVIEW.md incomplete**: append only the missing sections from the template; never rewrite existing rules.
   - **PR template missing**: create `.github/pull_request_template.md` from `${CLAUDE_SKILL_DIR}/templates/pull_request_template.md`. **Incomplete**: append only the missing parts.
   - **.gitignore**: append the line `.claude/settings.local.json` to the root `.gitignore` (create it if missing). If a `!` pattern un-ignores the file, show that line and propose removing it as a separate item.
   - **Core connection points** (only when 2.7 applies): ask the user for the connection-point paths and the core owner's GitHub handle or team; never guess them (you may suggest candidate paths you saw). Then propose: add the paths under `## Connection points with the core project` in CLAUDE.md (before `## Reviews`); add the connection-points rule to REVIEW.md's `## This project` (the commented rule in the template, uncommented); create `.github/CODEOWNERS` from `${CLAUDE_SKILL_DIR}/templates/CODEOWNERS.example` with those paths and owners, or append the lines if CODEOWNERS exists.
   - **Secrets scan** (only if gitleaks is installed): "run `gitleaks git --redact --no-banner .` (read-only; may take a while on a long history)". If that subcommand is unknown (gitleaks older than 8.19), use `gitleaks detect --redact --no-banner`.
3. Below the list, print "Not done by this skill (yours):" with each failing row that needs a person or GitHub settings: CI workflow, ruleset (print the UI steps from step 5), untracking settings.local.json, the newcomer test, the baseline.
4. Ask: "Apply these local changes? Reply yes, no, or the numbers to apply." Stop and wait for the answer. Apply nothing without it.
5. Apply exactly the approved items with Write (new files) or Edit (existing files). After any CLAUDE.md change, recount with `grep -c '' CLAUDE.md`; it must stay at 199 lines or fewer, otherwise tell the user what to cut.
6. Rerun the checks for the rows you touched and print the final table, then this closing block:
   - "Local changes only. Review them with `git status` and `git diff`, then commit on a branch and open a PR with the Jira key in the title."
   - "CLAUDE.md and REVIEW.md count for the gate (and for managed Code Review) only once merged to `<BRANCH>`."
   - The ruleset steps from step 5 if any ruleset row is not `pass`.
   - "Rerun `/factory:bootstrap-repo --check` after merging."

## 4. Gate table

Print one Markdown table, rows in this order. Put the detail in parentheses after the status. Leave "How to fix" empty for `pass` rows.

| Item | Status | How to fix |
|---|---|---|
| CLAUDE.md present, under 200 lines (working tree) | | |
| CLAUDE.md merged on `<BRANCH>`, under 200 lines (gate) | | |
| CLAUDE.md ends with the REVIEW.md pointer | | |
| All-checks command named in CLAUDE.md | | |
| CI runs on pull_request (gate) | | |
| CI runs the all-checks command (gate) | | |
| Ruleset: PR required (gate) | | |
| Ruleset: 1 approval (gate) | | |
| Ruleset: required status checks (gate) | | |
| Ruleset: conversations resolved (gate) | | |
| Newcomer: clone to passing checks with only CLAUDE.md (gate) | | |
| PR template | | |
| .gitignore has .claude/settings.local.json | | |
| .claude/settings.local.json not tracked | | |
| Secrets scan | | |
| Review time baseline recorded | | |
| REVIEW.md (Stage 1) | | |
| Core connection points: CLAUDE.md, REVIEW.md rule, CODEOWNERS (Stage 4) | | |

After the table print one line:
- `Stage 0 gate: PASS` when every `(gate)` row is `pass`;
- `Stage 0 gate: NOT YET (<n> failing)` when any `(gate)` row is `fail`;
- otherwise `Stage 0 gate: UNKNOWN (<rows> need confirming)`.

In CHECK mode, end the run after this line.

## 5. Ruleset UI steps (print; never apply)

Print these when any ruleset row is not `pass`. If `PERM` is not `ADMIN`, start with "A repo admin must do this."

```
GitHub > <REPO> > Settings > Rules > Rulesets > New ruleset > New branch ruleset
1. Ruleset name: main. Enforcement status: Active. Bypass list: leave empty.
2. Target branches: Add target > Include default branch.
3. Turn on Restrict deletions and Block force pushes.
4. Turn on Require a pull request before merging:
   - Required approvals: 1
   - Require conversation resolution before merging: on
   - Require review from Code Owners: on (only for repos the core project depends on)
5. Turn on Require status checks to pass > Add checks > choose the CI check that runs
   the all-checks command (a check appears in the list after it has run at least once).
6. Click Create. Then run /factory:bootstrap-repo --check.
```
