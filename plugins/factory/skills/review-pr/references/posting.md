# Posting findings to the pull request

Only in PR mode with `--comment`. One review per run, event `COMMENT`. Never `APPROVE`, never `REQUEST_CHANGES`, never more than one POST per run (except the single retry in step 5).

Placeholders below: `OWNER/REPO` from the PR URL, `N` the PR number, `HEAD` the full `headRefOid` SHA recorded in step 1.

## 1. Refuse to post twice for the same head

```bash
gh api repos/OWNER/REPO/pulls/N/reviews --paginate --jq '.[] | select((.body // "") | startswith("<!-- factory:review head=HEAD ")) | .html_url'
```

If this prints a URL, a factory review already exists for this exact head commit. Do not post; report `Not posted: already reviewed at <url>` and `posted: false`.

## 2. Find which lines can take an inline comment

GitHub only accepts inline comments on lines inside a diff hunk. Print the right-side line ranges of every hunk:

```bash
gh pr diff N --color=never | awk '/^diff --git /{h=1;f="";next} h&&/^\+\+\+ /{if($0=="+++ /dev/null")f="";else{f=substr($0,7);sub(/\t$/,"",f)};next} /^@@ /{h=0; if(f=="")next; split($3,a,","); s=substr(a[1],2)+0; n=(a[2]=="")?1:a[2]+0; if(n>0) printf "%s\t%d\t%d\n", f, s, s+n-1}'
```

Each output line is `<path>`TAB`<first>`TAB`<last>`; split on the TAB, never on spaces, because paths may contain spaces (git's trailing TAB after such a path is already stripped). Git C-quotes paths with unusual characters (`"b/..."`); those do not match, so their findings go in the review body. A finding is **inline** when its `file` matches a path and `line` is within one range of that path. It is a multi-line comment only when it has `end_line` GREATER than `line` and both fall in the SAME range; an `end_line` equal to `line` is a single-line comment. When `end_line` is in another range or outside the diff, post it single-line on `end_line` if that is in a range, else on `line`. A finding that is not inline goes in the review body (step 3). Pre-existing findings are usually not inline.

## 3. Build the review

Comment body for each inline finding (the marker must be the very first line, exactly as shown):

```
<!-- factory:finding id=F1 severity=Important -->
**Important: <title>**

<body>

_Rule: <rule>_            <- only when rule is set

Answer before merge: fix it, or resolve this thread with one line saying why not.
```

Inline comment object: `{"path": "<file>", "line": <line>, "side": "RIGHT", "body": "<comment body>"}`. For a multi-line range (only when `end_line` > `line`; GitHub rejects `start_line` equal to `line`) add `"start_line": <line>, "start_side": "RIGHT"` and set `"line": <end_line>`. Never send `start_line` on a single-line comment.

Review body (the marker must be the very first line; `n` is the TOTAL number of findings, inline plus body):

```
<!-- factory:review head=HEAD findings=n -->
**factory:review-pr**: n findings (i Important, j Nit, k Pre-existing). The agent comments; a person approves.

<summary sentences>

<only when some findings are not inline:>
### Findings outside the diff
<!-- factory:finding id=F3 severity=Pre-existing -->
**F3 Pre-existing** `path:line` <title>
<body>
```

Each finding outside the diff gets its own marker line, so every finding carries exactly one marker whether it is inline or in the body. With zero findings, still post the review (body only, `"comments": []`) so the PR shows it was reviewed.

## 4. Send it

Write the JSON payload in a quoted heredoc and validate it with `jq` first; `gh` runs only when the JSON parses, so nothing half-built is ever sent. Do not write the payload to a file.

```bash
payload="$(jq -ce . <<'FACTORY_REVIEW_JSON'
{
  "commit_id": "HEAD",
  "event": "COMMENT",
  "body": "<review body>",
  "comments": [ <inline comment objects> ]
}
FACTORY_REVIEW_JSON
)" && printf '%s' "$payload" | gh api --method POST repos/OWNER/REPO/pulls/N/reviews --input - --jq '{id: .id, url: .html_url, state: .state}'
```

If `jq` reports a parse error, fix the escaping and run the command again; nothing was sent.

- `event` is the string `COMMENT`. Check this before running the command.
- JSON-escape every string: `\n` for newlines, `\"` for quotes, `\\` for backslashes.
- Success prints the review id, `html_url` and `state` `COMMENTED`. Set `posted` to true and report the URL.

## 5. If GitHub rejects it

- `422` mentioning a line, position or diff (`pull_request_review_thread.line`, "could not be resolved", "Line must be part of the diff"): retry ONCE with `"comments": []` and every finding moved into the review body under `### Findings outside the diff`, each with its marker line. Update nothing else.
- `422` mentioning `commit_id` or "not part of the pull request": the PR head moved. Do not retry; report `Not posted: the PR head changed since the review; run the review again.`
- `403`/`404`: report the message; do not retry.
- Any other failure: report it, `posted: false`.

Never fall back to posting separate issue comments, and never post a second review in the same run beyond the one retry above.
