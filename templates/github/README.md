# Automatic review on GitHub (Stage 3)

Stage 3 of the review rollout makes the AI review run by itself on every pull request. There are
two ways to do that. **Use the managed Code Review feature when you can.** The GitHub Action in
this folder (`claude-review.yml`) is the fallback.

Both ways keep the same rules as the hand review in Stage 2: the agent comments, a person
approves, every finding is answered before merge, and the severities are Important / Nit /
Pre-existing.

## Which one to use

|                         | Managed Code Review (preferred)                                   | GitHub Action fallback (`claude-review.yml`)                          |
|-------------------------|-------------------------------------------------------------------|-----------------------------------------------------------------------|
| Who can use it          | Claude Team or Enterprise plans, GitHub-hosted repos. Not for Zero Data Retention, HIPAA-configured or CMEK orgs | Any plan with an Anthropic API key |
| What lives in the repo  | Nothing. It runs on Anthropic's side                              | A workflow file, a repo secret                                        |
| Turned on by            | A Claude org Owner: Admin settings > Claude Code > Code review ("Allow code review"), then Connect GitHub, install the Claude GitHub App, pick repos | A repo admin: `/install-github-app`, or the manual steps below |
| Reads                   | `CLAUDE.md` and `REVIEW.md` from the base branch, never from the PR | The same: a workflow step swaps in the base branch's copies of every `CLAUDE.md`, `CLAUDE.local.md` and `REVIEW.md` (any folder) and `.claude/` before Claude starts |
| Severities              | Important / Nit / Pre-existing, built in                          | Important / Nit / Pre-existing, from the prompt and `REVIEW.md`       |
| Approves or blocks      | Never. Its check run never fails                                  | Never. Claude has no GitHub write access; a workflow step posts its findings as one review with event `COMMENT`, so it cannot approve or request changes |
| When it runs            | Per-repo "Review runs" setting: Manual (`@claude review`), Once after PR creation, or After every push | `opened` and `ready_for_review`; `synchronize` (every push) is commented out. Drafts, fork PRs and bot-opened PRs (Dependabot etc.) are skipped |
| Paid with               | Claude plan usage, with an org monthly spend limit                | API key usage plus GitHub Actions minutes                             |
| Finding markers for `/factory:log-review` | Not added; its comments carry its own severity labels instead | Added to every inline comment and to the summary              |

Pick the managed feature if the org is on Team or Enterprise and an Owner can switch it on. Pick
the Action if the plan does not include Code Review, or the org cannot use it (ZDR, HIPAA, CMEK).
Never run both on the same repo: every PR would get two reviews and two bills.

### Rolling out the managed feature

Step the "Review runs" setting up only as the Stage 3 gate numbers allow:

1. **Manual (`@claude review`)**: someone comments `@claude review` as a new top-level PR
   comment. Use this while measuring cost per review.
2. **Once after PR creation**: runs when a PR is opened, reopened or marked ready.
3. **After every push**: the most thorough and the most expensive.

Protect the default branch (the Stage 0 ruleset does this) so the review also reads `REVIEW.md`
@-includes and subdirectory `REVIEW.md` files.

## Setting up the Action

The workflow file is a **template**. It does nothing in onion-factory. It must be copied into
the project repo, where it runs on that repo's pull requests.

Before you start, the project needs `CLAUDE.md` and `REVIEW.md` merged to its default branch
(Stages 0 and 1). Without them the review has no project rules to apply.

### Option A: `/install-github-app` (quickest)

Needs repo admin rights and `gh auth login`; works for github.com remotes only.

1. In the project repo, run `claude`, then `/install-github-app`.
2. Install the Claude GitHub App on the repo when the browser opens.
3. Choose an **API key** when asked for a credential. It is saved as the repo secret
   `ANTHROPIC_API_KEY`.
4. The command pushes a branch with its own workflow files. Before merging that PR, replace its
   PR-review workflow with this folder's `claude-review.yml` (keep its `@claude` mention
   workflow only if the team wants it). Our file adds the base-branch rules step, the severity
   rules, the finding markers and the comment-only posting step.

### Option B: manual

1. Create an API key in an Anthropic Console workspace that has a spend limit (see Cost).
2. In the project repo: Settings > Secrets and variables > Actions > New repository secret,
   name `ANTHROPIC_API_KEY`.
3. Copy the template:

   ```sh
   mkdir -p .github/workflows
   cp <path-to>/onion-factory/templates/github/claude-review.yml .github/workflows/claude-review.yml
   ```

4. Open a PR with the file (Jira key in the title, as usual) and merge it after review.

### After either option

- Leave Settings > Actions > General > "Allow GitHub Actions to create and approve pull
  requests" **off**. It is a second guard: with it off the job's token cannot approve. It does
  not stop a "request changes" review; the workflow's posting step does that (it always sends
  event `COMMENT`).
- Check that the PR ruleset still requires one human approval and all conversations resolved:
  that is what stops a merge with an unanswered finding.
- Open a small test PR. The "Claude review" job should post one review whose body starts with
  `<!-- factory:review head=<sha> findings=<n> -->`, plus one inline comment per finding, each
  starting with `<!-- factory:finding id=F<n> severity=<Severity> -->`.
- For supply-chain safety you may pin `anthropics/claude-code-action@v1` to a full commit SHA.

### What the workflow does

- Runs on `opened` and `ready_for_review`. Drafts and fork PRs are skipped (forks get no
  secrets). PRs opened by bots, such as Dependabot or Renovate, are skipped too: Dependabot runs
  get no Actions secrets and the action rejects bot actors, so the job could only fail. Review
  those PRs by hand, or run `/factory:review-pr` on them.
- One run per PR at a time (`concurrency` with `cancel-in-progress`).
- Permissions are `contents: read` and `pull-requests: write` only.
- Puts back the base branch's copy of every `CLAUDE.md`, `CLAUDE.local.md` and `REVIEW.md` the PR
  adds, edits, renames or deletes, in any folder, and the base branch's `.claude/`, so a PR cannot
  change those rules. One gap: a file that a `CLAUDE.md` pulls in with an `@path` import is read
  as the PR has it, so review changes to such files by eye.
- Claude may read code, run `gh pr diff`, `gh pr view` and read-only `git` commands, and write
  one file: the review payload. It has no GitHub write access.
- A separate step then checks the payload (summary marker first, a finding marker on every
  inline comment, event `COMMENT`) and posts it as **one** review. It sets the event to
  `COMMENT` and the commit to the PR head itself and passes on only the comment fields, so the
  review can never approve or request changes. If GitHub rejects an inline line as outside the
  diff (422), it retries once with every finding moved into the review body under
  `### Findings outside the diff`, the layout `/factory:log-review` reads.

## Cost

- **Set a spend limit first.** For the Action, put the API key in its own Console workspace with
  a monthly limit. For the managed feature, set the org's monthly spend limit in Admin settings.
- Each review is billed on its own. Cost grows with the size of the diff and the files Claude
  reads, so keeping PRs small (one of the findings rules) also keeps the bill down.
- The Action also uses GitHub Actions minutes on the project's account (one job per review,
  20-minute timeout).
- Enabling `synchronize` (Action) or "After every push" (managed) multiplies the cost by the
  number of pushes per PR. Turn it on only after cost per review is known and inside the limit.
- Stage 3's gate needs **cost per review**: read it from the Claude admin / Console usage page
  and enter it by hand. Divide the period's spend for the review key or workspace by the number
  of reviews posted.

## Answering the findings

Same as Stage 2. Every finding gets an answer before merge: fix it, or resolve the conversation
with one line saying why not. Important findings are fixed unless the reviewer agrees they are
wrong; Nits are the author's call; Pre-existing findings become Jira tickets. False findings are
logged in the Review Logs row (`Findings: AI Only, False`), not argued in the PR. Then run
`/factory:log-review` to fill the row.
