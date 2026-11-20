---
name: log-review
description: Build the reviewer's one-minute "Review Logs" row for a pull request (Ancillary Code Reviews sheet), counting AI findings from the factory review markers and thread resolution, and asking the reviewer only what cannot be derived. Prints a paste-ready TSV line and the same row as schema-valid JSON. Use after reviewing or approving a PR, or when someone says "log this review" or "fill the review log".
argument-hint: "<PR number or URL>"
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/references/logrow.py *) Bash(gh pr view *) Read
---

# Log a review

Produce one Review Logs row for the PR in `$ARGUMENTS`. Everything on GitHub is READ-ONLY here: never approve, comment, resolve threads, push, or edit files. The numbers judge the agent, never a person; do not comment on the reviewer's time or findings.

Helper: `${CLAUDE_SKILL_DIR}/references/logrow.py` (Python 3.9 stdlib). Column rules, counting rules and the detection heuristics are in `${CLAUDE_SKILL_DIR}/references/row-rules.md`; read it only when you need to explain a value or something looks off.

## 1. Gather

If `$ARGUMENTS` is empty, run `gh pr view --json url -q .url` for the current branch; if that fails, ask for the PR number or URL and stop.

Run:

```bash
python3 ${CLAUDE_SKILL_DIR}/references/logrow.py gather $ARGUMENTS
```

Add `--repo OWNER/NAME` when the argument is a bare number and the current directory is not that repo. Exit 2 means a `gh` failure: show the error and stop. The output is the facts JSON; its `facts_path` is the file to pass to `build`.

## 2. Show what was derived

Print, compactly:

- If any warning starts with `UNANSWERED FINDINGS`, put it FIRST as a bold warning line, e.g. **WARNING: F2 is not resolved. The gate is "none merged with an unanswered finding".** Say whether the PR is already merged. Do not resolve anything yourself.
- Right after it, any warning starting with `UNCHECKED FINDINGS` (findings with no review thread, such as review-body or check-run findings), also in bold: the reviewer must confirm by hand that each was answered.
- Other `warnings`, one line each.
- PR cell (`#n - title (url)`), Ticket (`tickets` joined with ` / `, or `N/A`) with `ticket_source`, Date Reviewed and `date_source`.
- How the AI review was identified: `ai_review_evidence` and, when `managed.detected`, `managed.method` verbatim (it says whether the managed Code Review comments were recognised by the inferred bot/severity rule or only counted from the check run).
- A table of `findings`: key, severity, `path:line`, title, thread (resolved / UNRESOLVED / unknown), fixed (yes / no / unknown with `fixed_evidence`).

## 3. Ask once

Ask only what cannot be derived, in ONE question block. Skip items 4 to 6 when `findings` is empty, skip item 8 unless `repeat_check_needed` is true, and skip 4 to 8 when `ai_reviewed` is false (then add one line: "No Claude review found on this PR. Was it reviewed by Claude some other way? (default: no)").

1. Your first name (two reviewers: `A/B`) [default `reviewer_suggestion`]
2. Minutes you spent reviewing
3. Complexity: Low / Medium / High [default `complexity_suggestion`]
4. Which AI findings were false (wrong, or noise REVIEW.md says not to flag)? ids or "none" [list the keys]
5. Of the problems YOU found, how many did the AI also find?
6. Fixed findings detected: <keys with fixed=true, or "none">. Correct? (reply only if wrong)
7. How many problems did you find that the AI did NOT?
8. The AI reviewed this PR more than once. Which keys repeat an earlier finding (counted once)? [suggest `repeat_candidates` as `R2.F1 = R1.F1`, or "none suggested"]
9. Ticket: <Ticket> (from `ticket_source`). Correct? (reply only if wrong)

If the reviewer answers yes to the "some other way" line, the AI findings are not on the PR, so nothing can be counted from it. Ask once more: "How many findings did that review make, how many were false, how many of yours did it also find, how many did it miss, highest severity? Or 'unknown' to leave the Findings columns empty." Never fill those counts with 0 yourself.

If the AskUserQuestion tool is available, use one call with four questions: (a) "Reviewer and minutes?" with options like `<suggestion>, 15 min` / `<suggestion>, 30 min` / `<suggestion>, 60 min` (free text through Other); (b) Complexity, the suggestion first; (c) false findings, multiSelect over the finding keys plus "None false" (when there are more keys than the tool allows, offer "None false" and take ids as free text); (d) "Your problems: also found by AI / missed by AI?" with options `0 / 0`, `1 / 0`, `0 / 1` (free text `n / m` through Other). Put items 6, 8 and 9 in the question text of (c) (for 8, ask the repeated keys as free text through Other, offering the suggestion as an option). Otherwise ask the numbered list above as plain text and wait for one reply.

Do not ask for anything else. Accept short answers ("Sam, 20, low, F2, 1, 0").

## 4. Build

Pass the answers as one inline JSON object (keys in `row-rules.md`; normally `reviewer`, `minutes`, `complexity`, `false_ids`, `both_found`, `human_only`, plus `duplicate_ids` when item 8 was asked, `fixed_ids` only if the reviewer corrected item 6, `ticket` only if they corrected item 9, and `notes` only if they volunteered one). For a yes to the "some other way" line pass `reviewed_by_claude: "yes"` and, only if the reviewer gave them, all of `ai_findings`, `false_count`, `both_found`, `human_only` (and `severity`, `accepted`); with "unknown" pass none of them, so the Findings columns stay empty. Never pass a count the reviewer did not give. Escape any `'` in values as `'\''`.

```bash
python3 ${CLAUDE_SKILL_DIR}/references/logrow.py build --facts <facts_path> --answers '<answers JSON>'
```

- Exit 1 with `answers do not add up`: quote the message, re-ask only the conflicting item, rebuild.
- Exit 1 with `VALIDATION ERRORS`: fix the offending answer (never hand-edit the row), rebuild.
- Exit 0: continue.

## 5. Output

Print, in this order:

1. The ARITHMETIC lines as they came (they show AI total = Both + AI Only Real + AI Only False).
2. The TSV line inside a ```text block, untouched (real tab characters), with: "Paste into column A of the first empty row of Review Logs."
3. The JSON row inside a ```json block, and the validation status line.
4. If Human Only > 0: "Each miss should become a REVIEW.md rule and an eval case (rerun the eval after changing REVIEW.md)." Then every EVAL CASE SKELETON block with its suggested path, saying the `TODO` summary must be replaced with what was wrong (at least 10 characters), specifically enough for a judge to match a finding to it. The path is relative to the onion-factory repo (the team's eval set, where `run_evals.py` reads `evals/review/cases/<project>/`), NOT the project repo being reviewed. Do not write these files unless the reviewer asks, and then only into a local onion-factory checkout whose path the reviewer gives.

## 6. Writing to the Google Sheet (optional)

Only when a Google Sheets tool is available in this session (one that can read and append values) AND the reviewer explicitly says yes in this run to "Append this row to Review Logs now?". Otherwise stop after step 5. When both hold:

1. Ask for the spreadsheet URL or ID if the reviewer has not given it in this run. Never guess one.
2. Read `'Review Logs'!A1:P1` and compare it cell by cell to the 16 keys of the JSON row, in order. Any difference: stop and show both; do not write.
3. Read `'Review Logs'!D:D`; if a cell already contains this PR's URL, show that row number and ask before appending.
4. Append ONE row to `'Review Logs'!A:P` with input option RAW: the JSON row's values in column order, `null` as an empty string, integers as numbers.
5. Report the range the tool says it wrote. Never edit or delete existing rows.
