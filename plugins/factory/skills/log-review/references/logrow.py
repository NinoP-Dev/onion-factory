#!/usr/bin/env python3
"""Build one "Review Logs" row for a pull request.

Used by the /factory:log-review skill. Python 3.9+, standard library only.

  logrow.py gather <PR number or URL> [--repo OWNER/NAME] [--out FILE]
      Read-only GitHub calls through `gh`: PR metadata, AI findings (factory
      markers, plus the managed Code Review feature when identifiable), review
      thread resolution, and whether each finding's lines changed after it was
      posted. Writes the facts JSON to FILE (default: a file under the system
      temp dir, named after the PR) and prints it; "facts_path" holds FILE.

  logrow.py build --facts FILE --answers JSON|FILE|- [--format text|json]
      Combine the facts with the reviewer's answers (inline JSON object, a
      file, or - for stdin). Prints the arithmetic, one TSV line in the
      schema's x-column-order, the same row as JSON (validated), and one
      eval-case skeleton per Human Only miss.

Exit codes: 0 ok, 1 invalid answers or row fails validation, 2 usage or gh error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Row contract (mirrors schemas/review-log-row.schema.json; the schema file
# wins when found, and the unit test checks the two agree).
# ---------------------------------------------------------------------------

COLUMNS = [
    "Reviewer", "Date Reviewed", "Ticket", "PR", "Complexity", "Reviewed By Claude AI",
    "Recommendation", "Accepted Recommendation", "Actual Time (mins)", "Est. Time w/o Claude (min)",
    "Notes", "Recommendation Severity", "Findings: Both Found", "Findings: AI Only, Real",
    "Findings: AI Only, False", "Findings: Human Only",
]

_NN_INT = {"type": ["integer", "null"], "minimum": 0}
# Same constraints as the schema file, without descriptions. Used when the
# schema file is not reachable (the plugin runs inside other repositories).
EMBEDDED_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": COLUMNS[:9],
    "properties": {
        "Reviewer": {"type": "string", "minLength": 1},
        "Date Reviewed": {"type": "string", "pattern": r"^[A-Z][a-z]{2} \d{2}, \d{4}$"},
        "Ticket": {"type": "string"},
        "PR": {"type": "string", "pattern": r"^#\d+ - .+ \(https://github\.com/[^/]+/[^/]+/pull/\d+\)$"},
        "Complexity": {"enum": ["Low", "Medium", "High"]},
        "Reviewed By Claude AI": {"enum": ["Yes", "No"]},
        "Recommendation": {"enum": ["YES", "NO"]},
        "Accepted Recommendation": {"enum": ["YES", "NO"]},
        "Actual Time (mins)": {"type": "integer", "minimum": 0},
        "Est. Time w/o Claude (min)": dict(_NN_INT),
        "Notes": {"type": "string"},
        "Recommendation Severity": {"type": "string"},
        "Findings: Both Found": dict(_NN_INT),
        "Findings: AI Only, Real": dict(_NN_INT),
        "Findings: AI Only, False": dict(_NN_INT),
        "Findings: Human Only": dict(_NN_INT),
    },
    "x-column-order": COLUMNS,
}

SEVERITY_RANK = {"Important": 3, "Nit": 2, "Pre-existing": 1}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

FINDING_MARKER = re.compile(r"^\s*<!--\s*factory:finding\b([^>]*?)-->")
REVIEW_MARKER = re.compile(r"<!--\s*factory:review\b([^>]*?)-->")
ATTR = re.compile(r"([a-z_]+)=([^\s>]+)")
# Title: upper-case keys only ("WID-12"), so "lodash-4" or "node-18" are not tickets.
JIRA_KEY = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9}-[0-9]+)(?![A-Za-z0-9]|\.[0-9])")
# Branch: a key of either case at the START of a path segment, followed by '-', '_' or the
# segment end ("feature/wid-12-checkout"), so "dependabot/.../axios-1.6.0" gives nothing.
BRANCH_JIRA_KEY = re.compile(r"(?:^|/)([A-Za-z][A-Za-z0-9]{1,9}-[0-9]+)(?=[-_/]|$)")
NOT_JIRA_PREFIXES = {"UTF", "ISO", "RFC", "SHA", "CVE", "HTTP", "TLS", "SSL", "MD", "PR", "V"}
PR_URL = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")


class UsageError(Exception):
    """Bad arguments or a failed gh call (exit 2)."""


class AnswerError(Exception):
    """Reviewer answers that do not add up (exit 1)."""


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def sheet_date(day: dt.date) -> str:
    """'Nov 05, 2026' -- the writer format required by the schema."""
    return "%s %02d, %d" % (MONTHS[day.month - 1], day.day, day.year)


def local_date(ts: dt.datetime) -> dt.date:
    return ts.astimezone().date()


def attrs(text: str) -> Dict[str, str]:
    return dict(ATTR.findall(text))


def _keep_keys(raws: List[str]) -> List[str]:
    keys: List[str] = []
    for raw in raws:
        key = raw.upper()
        if key.split("-")[0] in NOT_JIRA_PREFIXES or key in keys:
            continue
        keys.append(key)
    return keys


def jira_keys(title: Optional[str], branch: Optional[str] = None) -> List[str]:
    """Jira keys from the PR title; the head branch is used only when the title has none.

    A guess either way: the skill shows the Ticket to the reviewer for confirmation.
    """
    keys = _keep_keys(JIRA_KEY.findall(title or ""))
    if keys:
        return keys
    return _keep_keys(BRANCH_JIRA_KEY.findall(branch or ""))


def one_line(text: Any) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(text)).strip()


def parse_pr_ref(ref: str, repo: Optional[str]) -> Tuple[Optional[str], int]:
    m = PR_URL.search(ref)
    if m:
        return "%s/%s" % (m.group(1), m.group(2)), int(m.group(3))
    m = re.fullmatch(r"#?(\d+)", ref.strip())
    if m:
        return repo, int(m.group(1))
    raise UsageError("not a PR number or GitHub PR URL: %r" % ref)


def finding_title(body: str) -> str:
    """First non-empty line after the marker, without markdown or a severity prefix."""
    lines = [ln.strip() for ln in body.replace("\r\n", "\n").split("\n")]
    for ln in lines[1:]:
        cleaned = ln.strip("*#_> ").strip()
        cleaned = re.sub(r"^(Important|Nit|Pre-existing)\s*:\s*", "", cleaned)
        if cleaned:
            return cleaned[:100]
    return ""


# Review-body finding heading written by review-pr: **F3 Pre-existing** `path:line` <title>
BODY_FINDING_HEAD = re.compile(r"^\*\*F\d+\s+[A-Za-z-]+\*\*\s+`([^`]+?):(\d+)`\s*(.*)$")


def suggest_complexity(additions: int, deletions: int) -> str:
    size = (additions or 0) + (deletions or 0)
    if size <= 50:
        return "Low"
    if size <= 300:
        return "Medium"
    return "High"


def patch_touches(patch: Optional[str], start: int, end: int, margin: int = 2) -> bool:
    """True when a unified-diff patch deletes, edits, or inserts next to old lines [start, end].

    Line numbers are on the OLD side of the patch (the commit the finding was posted on).
    """
    if not patch:
        return False
    lo, hi = start - margin, end + margin
    old = 0
    for line in patch.split("\n"):
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
        if m:
            old = int(m.group(1))
            continue
        if not line or line.startswith("\\"):
            continue
        tag = line[0]
        if tag == " ":
            old += 1
        elif tag == "-":
            if lo <= old <= hi:
                return True
            old += 1
        elif tag == "+":
            # An insertion sits between old-1 and old.
            if lo <= old <= hi + 1:
                return True
    return False


# ---------------------------------------------------------------------------
# Finding extraction (pure: works on REST/GraphQL payloads)
# ---------------------------------------------------------------------------

def factory_findings(comments: List[dict], reviews: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Return (runs, findings) from factory markers.

    A run is one posted review: a review whose body carries the factory:review
    marker, or the review that owns marker-bearing inline comments.
    """
    review_by_id = {r.get("id"): r for r in reviews}
    run_ids: List[Any] = []
    run_meta: Dict[Any, dict] = {}

    def add_run(review_id: Any) -> None:
        if review_id in run_meta:
            return
        r = review_by_id.get(review_id) or {}
        marker = REVIEW_MARKER.search(r.get("body") or "")
        meta = attrs(marker.group(1)) if marker else {}
        declared = meta.get("findings")
        run_meta[review_id] = {
            "review_id": review_id,
            "head": meta.get("head"),
            "declared_findings": int(declared) if declared and declared.isdigit() else None,
            "submitted_at": r.get("submitted_at"),
            "has_summary_marker": bool(meta),
        }
        run_ids.append(review_id)

    for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        if REVIEW_MARKER.search(r.get("body") or ""):
            add_run(r.get("id"))

    raw: List[dict] = []
    for c in sorted(comments, key=lambda c: c.get("created_at") or ""):
        if c.get("in_reply_to_id"):
            continue
        m = FINDING_MARKER.match(c.get("body") or "")
        if not m:
            continue
        a = attrs(m.group(1))
        rid = c.get("pull_request_review_id")
        add_run(rid)
        line = c.get("original_line") or c.get("line")
        start = c.get("original_start_line") or c.get("start_line") or line
        raw.append({
            "run": rid,
            "id": a.get("id", "F?"),
            "severity": a.get("severity", ""),
            "source": "factory-inline",
            "comment_id": c.get("id"),
            "path": c.get("path"),
            "line": line,
            "start_line": start,
            "side": c.get("side") or "RIGHT",
            "commit": c.get("original_commit_id") or c.get("commit_id"),
            "url": c.get("html_url"),
            "title": finding_title(c.get("body") or ""),
            "created_at": c.get("created_at"),
        })

    # Findings listed only in a review body (no postable line): one marker per line start.
    for r in reviews:
        body = (r.get("body") or "").replace("\r\n", "\n")
        inline_ids = {f["id"] for f in raw if f["run"] == r.get("id")}
        chunks = re.split(r"\n(?=\s*<!--\s*factory:finding\b)", body)
        for chunk in chunks:
            m = FINDING_MARKER.match(chunk)
            if not m:
                continue
            a = attrs(m.group(1))
            if a.get("id") in inline_ids:
                continue
            add_run(r.get("id"))
            second = (chunk.split("\n", 2) + ["", ""])[1].strip()
            head = BODY_FINDING_HEAD.match(second)
            path, line = (head.group(1), int(head.group(2))) if head else (a.get("file"), None)
            raw.append({
                "run": r.get("id"), "id": a.get("id", "F?"), "severity": a.get("severity", ""),
                "source": "factory-review-body", "comment_id": None, "path": path,
                "line": line, "start_line": line, "side": "RIGHT" if line else None,
                "commit": r.get("commit_id"), "url": r.get("html_url"),
                "title": (head.group(3).strip()[:100] if head and head.group(3).strip() else finding_title(chunk)),
                "created_at": r.get("submitted_at"),
            })

    multi = len(run_ids) > 1
    runs = []
    for n, rid in enumerate(run_ids, 1):
        meta = dict(run_meta[rid])
        meta["key"] = "R%d" % n
        runs.append(meta)
    run_key = {r["review_id"]: r["key"] for r in runs}
    for f in raw:
        f["key"] = "%s.%s" % (run_key[f["run"]], f["id"]) if multi else f["id"]
        f["run"] = run_key[f["run"]]
    return runs, raw


# --- Managed Code Review identification -------------------------------------
# INFERRED, keep isolated: the docs say managed Code Review posts inline
# comments labelled with a severity marker (red circle Important, yellow
# circle Nit, purple circle Pre-existing) and a check run named
# "Claude Code Review" whose output text ends with
#   <!-- bughunter-severity: {"nit":1,"normal":2,"pre_existing":0} -->
# ('normal' = Important). The author login of the inline comments is not
# documented; we require a Bot account whose login contains "claude".

MANAGED_CHECK_NAME = "Claude Code Review"
MANAGED_SEVERITY = [("\U0001F534", "Important"), ("\U0001F7E1", "Nit"), ("\U0001F7E3", "Pre-existing")]
BUGHUNTER = re.compile(r"<!--\s*bughunter-severity:\s*(\{.*?\})\s*-->")


def managed_severity(body: str) -> Optional[str]:
    head = (body or "")[:200]
    for emoji, sev in MANAGED_SEVERITY:
        if emoji in head:
            return sev
    m = re.match(r"\s*\**\s*(Important|Nit|Pre-existing)\b", body or "")
    return m.group(1) if m else None


def managed_title(body: str) -> str:
    """First line with text once the severity marker is removed (managed comments carry no HTML marker)."""
    for ln in (body or "").replace("\r\n", "\n").split("\n"):
        cleaned = ln
        for emoji, _ in MANAGED_SEVERITY:
            cleaned = cleaned.replace(emoji, "")
        cleaned = re.sub(r"^[\s*_#>]*(Important|Nit|Pre-existing)\b[\s*_:]*", "", cleaned.strip())
        cleaned = cleaned.strip("*#_> ").strip()
        if cleaned:
            return cleaned[:100]
    return ""


def is_managed_comment(c: dict) -> bool:
    user = c.get("user") or {}
    login = (user.get("login") or "").lower()
    return (user.get("type") == "Bot" and "claude" in login and not c.get("in_reply_to_id")
            and not FINDING_MARKER.match(c.get("body") or "")
            and managed_severity(c.get("body") or "") is not None)


def managed_findings(comments: List[dict]) -> List[dict]:
    out = []
    picked = [c for c in sorted(comments, key=lambda c: c.get("created_at") or "") if is_managed_comment(c)]
    for n, c in enumerate(picked, 1):
        line = c.get("original_line") or c.get("line")
        out.append({
            "key": "M%d" % n, "id": "M%d" % n, "run": "managed",
            "severity": managed_severity(c.get("body") or ""), "source": "managed-inline",
            "comment_id": c.get("id"), "path": c.get("path"), "line": line,
            "start_line": c.get("original_start_line") or c.get("start_line") or line,
            "side": c.get("side") or "RIGHT",
            "commit": c.get("original_commit_id") or c.get("commit_id"),
            "url": c.get("html_url"), "title": managed_title(c.get("body") or ""),
            "created_at": c.get("created_at"),
        })
    return out


def parse_bughunter(text: Optional[str]) -> Optional[Dict[str, int]]:
    m = BUGHUNTER.search(text or "")
    if not m:
        return None
    try:
        raw = json.loads(m.group(1))
    except ValueError:
        return None
    return {
        "Important": int(raw.get("normal", 0)),
        "Nit": int(raw.get("nit", 0)),
        "Pre-existing": int(raw.get("pre_existing", 0)),
    }


def findings_from_counts(counts: Dict[str, int]) -> List[dict]:
    out = []
    for sev in ("Important", "Nit", "Pre-existing"):
        for _ in range(counts.get(sev, 0)):
            n = len(out) + 1
            out.append({
                "key": "M%d" % n, "id": "M%d" % n, "run": "managed", "severity": sev,
                "source": "managed-check-run", "comment_id": None, "path": None, "line": None,
                "start_line": None, "side": None, "commit": None, "url": None,
                "title": "(count from check run; no inline comment identified)", "created_at": None,
            })
    return out
# --- end of inferred managed Code Review logic -------------------------------


def _norm_title(title: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def repeat_candidates(findings: List[dict]) -> List[dict]:
    """Suggest findings that repeat an earlier one: same file and title, but another run or commit.

    Only a suggestion (titles can be reworded between runs); the reviewer confirms the list,
    which becomes duplicate_ids.
    """
    first: Dict[Tuple[str, str], dict] = {}
    out: List[dict] = []
    for f in sorted(findings, key=lambda f: f.get("created_at") or ""):
        title = _norm_title(f.get("title"))
        if not f.get("path") or not title or f.get("source") == "managed-check-run":
            continue
        sig = (f["path"], title)
        prev = first.get(sig)
        if prev is None:
            first[sig] = f
        elif prev.get("run") != f.get("run") or prev.get("commit") != f.get("commit"):
            out.append({"key": f["key"], "repeats": prev["key"]})
    return out


def needs_repeat_check(runs: List[dict], findings: List[dict]) -> bool:
    """True when findings come from more than one review round (factory runs, managed pushes, or both)."""
    managed_commits = {f.get("commit") for f in findings if f.get("source") == "managed-inline"}
    has_managed = any(f.get("run") == "managed" for f in findings)
    return len(runs) > 1 or len(managed_commits) > 1 or (bool(runs) and has_managed)


def attach_threads(findings: List[dict], threads: Optional[List[dict]]) -> None:
    """Set finding['thread'] = {resolved, replies, outdated} from GraphQL reviewThreads.

    INFERRED schema: PullRequest.reviewThreads nodes with isResolved, isOutdated and
    comments(first:1){totalCount nodes{databaseId}}; databaseId equals the REST comment id.
    """
    by_root: Dict[Any, dict] = {}
    for t in threads or []:
        nodes = ((t.get("comments") or {}).get("nodes")) or []
        if nodes:
            by_root[nodes[0].get("databaseId")] = t
    for f in findings:
        if f.get("comment_id") is None:
            f["thread"] = {"resolved": None, "replies": None, "outdated": None, "why": "no inline thread"}
            continue
        if threads is None:
            f["thread"] = {"resolved": None, "replies": None, "outdated": None, "why": "thread query failed"}
            continue
        t = by_root.get(f["comment_id"])
        if t is None:
            f["thread"] = {"resolved": None, "replies": None, "outdated": None, "why": "thread not found"}
            continue
        total = (t.get("comments") or {}).get("totalCount") or 1
        f["thread"] = {"resolved": bool(t.get("isResolved")), "replies": total - 1,
                       "outdated": bool(t.get("isOutdated")), "why": ""}


def attach_fixed(findings: List[dict], head_sha: Optional[str], compares: Dict[str, Optional[dict]]) -> None:
    """Mark each inline finding fixed=True when a later commit changed lines near it.

    compares maps a finding's commit sha to the GitHub compare payload <sha>...<head>
    (None when the call failed).
    """
    for f in findings:
        f["fixed"], f["fixed_evidence"] = None, ""
        if not f.get("path") or not f.get("line") or not f.get("commit"):
            f["fixed_evidence"] = "no line to check"
            continue
        if f.get("side") == "LEFT":
            f["fixed_evidence"] = "comment on a deleted line; not checked"
            continue
        if f["commit"] == head_sha:
            f["fixed"], f["fixed_evidence"] = False, "no commits after the finding"
            continue
        cmp_ = compares.get(f["commit"])
        if cmp_ is None:
            f["fixed_evidence"] = "compare %s...head failed" % f["commit"][:7]
            continue
        f["fixed"] = False
        f["fixed_evidence"] = "lines unchanged after the finding"
        for entry in cmp_.get("files") or []:
            if f["path"] not in (entry.get("filename"), entry.get("previous_filename")):
                continue
            if entry.get("status") in ("removed", "renamed") and entry.get("filename") != f["path"]:
                f["fixed"], f["fixed_evidence"] = True, "file %s after the finding" % entry.get("status")
            elif entry.get("patch") is None and entry.get("status") == "modified":
                f["fixed"], f["fixed_evidence"] = None, "file changed but patch too large to check"
            elif patch_touches(entry.get("patch"), int(f["start_line"] or f["line"]), int(f["line"])):
                f["fixed"], f["fixed_evidence"] = True, "lines %s-%s changed in a later commit" % (
                    f["start_line"] or f["line"], f["line"])
            break


# ---------------------------------------------------------------------------
# gh access (side-effect free: GET requests and GraphQL queries only)
# ---------------------------------------------------------------------------

def gh(args: List[str], check: bool = True) -> Optional[str]:
    proc = subprocess.run(["gh"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        if check:
            raise UsageError("gh %s failed: %s" % (" ".join(args[:3]), proc.stderr.strip()))
        return None
    return proc.stdout


def json_stream(text: str) -> List[Any]:
    """Parse gh --paginate output: concatenated JSON arrays become one list."""
    out: List[Any] = []
    dec = json.JSONDecoder()
    i = 0
    text = text.strip()
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        if isinstance(obj, list):
            out.extend(obj)
        else:
            out.append(obj)
    return out


THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line
          comments(first: 1) { totalCount nodes { databaseId } }
        }
      }
    }
  }
}
"""


def fetch_threads(repo: str, number: int) -> Optional[List[dict]]:
    owner, name = repo.split("/", 1)
    nodes: List[dict] = []
    cursor: Optional[str] = None
    for _ in range(20):
        args = ["api", "graphql", "-f", "query=" + THREADS_QUERY, "-f", "owner=" + owner,
                "-f", "name=" + name, "-F", "number=%d" % number]
        if cursor:
            args += ["-f", "cursor=" + cursor]
        out = gh(args, check=False)
        if out is None:
            return None
        try:
            page = json.loads(out)["data"]["repository"]["pullRequest"]["reviewThreads"]
        except (ValueError, KeyError, TypeError):
            return None
        nodes.extend(page.get("nodes") or [])
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        cursor = page["pageInfo"]["endCursor"]
    return nodes


def fetch_managed_counts(repo: str, commits: List[str]) -> Tuple[Optional[Dict[str, int]], Optional[str]]:
    """Newest commit first; follow 'not repeated' titles back at most 10 commits."""
    for sha in commits[:10]:
        out = gh(["api", "repos/%s/commits/%s/check-runs?check_name=%s" % (
            repo, sha, MANAGED_CHECK_NAME.replace(" ", "%20"))], check=False)
        if out is None:
            return None, None
        runs = (json.loads(out).get("check_runs") or [])
        if not runs:
            continue
        output = runs[0].get("output") or {}
        counts = parse_bughunter(output.get("text"))
        if counts is not None:
            return counts, sha
        title = output.get("title") or ""
        if "no issues" in title.lower():
            return {"Important": 0, "Nit": 0, "Pre-existing": 0}, sha
    return None, None


def gather(ref: str, repo: Optional[str]) -> dict:
    repo, number = parse_pr_ref(ref, repo)
    if not repo:
        repo = one_line(gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]) or "")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo or ""):
        raise UsageError("could not tell the repository; pass --repo OWNER/NAME or a PR URL")
    fields = ("number,title,url,headRefName,headRefOid,baseRefName,state,isDraft,mergedAt,"
              "updatedAt,createdAt,additions,deletions,changedFiles,commits,latestReviews")
    pr = json.loads(gh(["pr", "view", str(number), "--repo", repo, "--json", fields]) or "{}")
    comments = json_stream(gh(["api", "--paginate", "repos/%s/pulls/%d/comments?per_page=100" % (repo, number)]) or "")
    reviews = json_stream(gh(["api", "--paginate", "repos/%s/pulls/%d/reviews?per_page=100" % (repo, number)]) or "")
    warnings: List[str] = []

    runs, findings = factory_findings(comments, reviews)
    evidence = []
    if runs:
        evidence.append("factory markers: %d review run(s), %d finding comment(s)" % (len(runs), len(findings)))
        for r in runs:
            if r["declared_findings"] is not None:
                got = len([f for f in findings if f["run"] == r["key"]])
                if got != r["declared_findings"]:
                    warnings.append("%s summary declares findings=%d but %d finding marker(s) were found" % (
                        r["key"], r["declared_findings"], got))
        if len(runs) > 1:
            warnings.append("%d factory review runs on this PR; the same problem may appear in more than one run. "
                            "List repeats as duplicate_ids so they are counted once." % len(runs))

    commit_shas = [c.get("oid") for c in reversed(pr.get("commits") or []) if c.get("oid")]
    if pr.get("headRefOid") and (not commit_shas or commit_shas[0] != pr["headRefOid"]):
        commit_shas.insert(0, pr["headRefOid"])
    managed = managed_findings(comments)
    counts, counts_sha = fetch_managed_counts(repo, commit_shas)
    managed_info = {"detected": False, "method": "", "check_run_counts": counts, "check_run_commit": counts_sha}
    if managed:
        managed_info.update(detected=True, method=(
            "inline comments by a Bot whose login contains 'claude', first line carrying a severity "
            "marker (red/yellow/purple circle or Important/Nit/Pre-existing); inferred, not documented"))
        if counts is not None and sum(counts.values()) != len(managed):
            warnings.append("managed Code Review: check run counts %s but %d inline comment(s) identified" % (
                counts, len(managed)))
    elif counts is not None:
        managed = findings_from_counts(counts)
        managed_info.update(detected=True, method=(
            "'%s' check run severity line (bughunter-severity) on %s; no inline comments identified"
            % (MANAGED_CHECK_NAME, (counts_sha or "")[:7])))
    if managed_info["detected"]:
        evidence.append("managed Code Review: " + managed_info["method"])
    findings.extend(managed)

    threads = fetch_threads(repo, number)
    if threads is None and findings:
        warnings.append("GraphQL reviewThreads query failed: thread resolution is UNKNOWN; "
                        "check the PR's conversations by hand before logging")
    attach_threads(findings, threads)

    compares: Dict[str, Optional[dict]] = {}
    for f in findings:
        sha = f.get("commit")
        if sha and sha != pr.get("headRefOid") and sha not in compares and f.get("path"):
            out = gh(["api", "repos/%s/compare/%s...%s" % (repo, sha, pr.get("headRefOid"))], check=False)
            compares[sha] = json.loads(out) if out else None
    attach_fixed(findings, pr.get("headRefOid"), compares)

    repeat_check = needs_repeat_check(runs, findings)
    repeats = repeat_candidates(findings) if repeat_check else []
    if repeat_check and len(runs) <= 1:
        warnings.append("AI findings come from more than one review round (managed Code Review after several pushes, "
                        "or both factory and managed reviews); the same problem may be counted more than once. "
                        "List repeats as duplicate_ids so they are counted once.")

    unresolved = [f["key"] for f in findings if (f.get("thread") or {}).get("resolved") is False]
    unchecked = [f["key"] for f in findings if (f.get("thread") or {}).get("resolved") is None]
    if unchecked:
        warnings.insert(0, "UNCHECKED FINDINGS: %s have no review thread that could be checked; confirm by hand that "
                           "each was answered before merge." % ", ".join(unchecked))
    if unresolved:
        warnings.insert(0, "UNANSWERED FINDINGS: thread(s) %s are not resolved. The gate is 'none merged with an "
                           "unanswered finding'%s." % (", ".join(unresolved),
                                                        " and this PR is already MERGED" if pr.get("mergedAt") else ""))

    if pr.get("mergedAt"):
        date, source = local_date(parse_ts(pr["mergedAt"])), "merged_at"
    else:
        approvals = sorted((r.get("submittedAt") or "") for r in pr.get("latestReviews") or []
                           if r.get("state") == "APPROVED" and r.get("submittedAt"))
        if approvals:
            date, source = local_date(parse_ts(approvals[-1])), "latest approval"
        else:
            date, source = dt.date.today(), "today (PR not merged or approved)"

    name = one_line(gh(["api", "user", "--jq", ".name // .login"], check=False) or "")
    return {
        "repo": repo,
        "number": pr.get("number", number),
        "title": one_line(pr.get("title", "")),
        "url": pr.get("url") or "https://github.com/%s/pull/%d" % (repo, number),
        "head_ref": pr.get("headRefName"),
        "head_sha": pr.get("headRefOid"),
        "state": pr.get("state"),
        "is_draft": pr.get("isDraft"),
        "merged_at": pr.get("mergedAt"),
        "updated_at": pr.get("updatedAt"),
        "date_reviewed": sheet_date(date),
        "date_source": source,
        "tickets": jira_keys(pr.get("title"), pr.get("headRefName")),
        "ticket_source": "title" if jira_keys(pr.get("title")) else (
            "head branch" if jira_keys(None, pr.get("headRefName")) else "none found"),
        "size": {"additions": pr.get("additions"), "deletions": pr.get("deletions"),
                 "changed_files": pr.get("changedFiles")},
        "complexity_suggestion": suggest_complexity(pr.get("additions") or 0, pr.get("deletions") or 0),
        "reviewer_suggestion": name.split(" ")[0] if name else "",
        "ai_reviewed": bool(runs or managed_info["detected"]),
        "ai_review_evidence": evidence,
        "runs": runs,
        "managed": managed_info,
        "findings": findings,
        "repeat_check_needed": repeat_check,
        "repeat_candidates": repeats,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Row building (pure)
# ---------------------------------------------------------------------------

def _int(answers: dict, key: str, default: Optional[int] = None) -> Optional[int]:
    v = answers.get(key, default)
    if v is None or v == "":
        return default
    if isinstance(v, bool) or not re.fullmatch(r"\d+", str(v).strip()):
        raise AnswerError("%s must be a whole number >= 0, got %r" % (key, v))
    return int(str(v).strip())


def _ids(answers: dict, key: str) -> List[str]:
    v = answers.get(key) or []
    if isinstance(v, str):
        v = [p for p in re.split(r"[\s,]+", v) if p and p.lower() != "none"]
    return [str(x).strip() for x in v]


def pr_cell(facts: dict) -> str:
    return "#%d - %s (%s)" % (facts["number"], one_line(facts["title"]) or "(no title)", facts["url"])


def build(facts: dict, answers: dict, today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    keys = [f["key"] for f in facts.get("findings") or []]
    by_key = {f["key"]: f for f in facts.get("findings") or []}

    def known(ids: List[str], label: str) -> List[str]:
        bad = [i for i in ids if i not in by_key]
        if bad:
            raise AnswerError("%s has unknown finding id(s) %s; known: %s" % (label, ", ".join(bad), ", ".join(keys) or "none"))
        return ids

    reviewer = one_line(answers.get("reviewer") or "")
    if not reviewer:
        raise AnswerError("reviewer is required")
    complexity = str(answers.get("complexity") or "").strip().capitalize()
    if complexity not in ("Low", "Medium", "High"):
        raise AnswerError("complexity must be Low, Medium or High, got %r" % answers.get("complexity"))
    minutes = _int(answers, "minutes")
    if minutes is None:
        raise AnswerError("minutes is required")

    reviewed = bool(facts.get("ai_reviewed", False))
    override = answers.get("reviewed_by_claude")
    if override is not None:
        reviewed = str(override).strip().lower() in ("yes", "true", "y")
    # Claude reviewed the change, but not on this PR (no factory marker, no managed result):
    # there is nothing to count from, so the counts come from the reviewer or stay empty.
    external = reviewed and not facts.get("ai_reviewed", False)

    dupes = set(known(_ids(answers, "duplicate_ids"), "duplicate_ids"))
    counted = [f for f in facts.get("findings") or [] if f["key"] not in dupes] if reviewed else []
    false_ids = known(_ids(answers, "false_ids"), "false_ids")
    false_ids = [i for i in dict.fromkeys(false_ids) if i not in dupes]
    lines: List[str] = []
    warnings = list(facts.get("warnings") or [])
    human = 0
    findings_cols: List[Optional[int]] = [None, None, None, None]
    ai_total = 0          # AI findings behind Recommendation / Accepted
    severity = ""

    def arithmetic(total: int, n_false: int, both: int, human_: int, false_label: str) -> List[Optional[int]]:
        real = total - n_false
        if both > real:
            raise AnswerError("both_found=%d is more than the %d AI finding(s) judged real (%d total - %d false)"
                              % (both, real, total, n_false))
        ai_real = real - both
        lines.append("AI Only, False = %d (%s)" % (n_false, false_label))
        lines.append("Both Found      = %d (reviewer)" % both)
        lines.append("AI Only, Real   = %d total - %d false - %d both = %d" % (total, n_false, both, ai_real))
        lines.append("Check: Both + AI Only Real + AI Only False = %d + %d + %d = %d = AI total %d" % (
            both, ai_real, n_false, both + ai_real + n_false, total))
        lines.append("Human Only      = %d (reviewer)" % human_)
        if total:
            lines.append("False finding rate this PR = %d/%d = %.2f (target < 0.20)" % (n_false, total, n_false / total))
        return [both, ai_real, n_false, human_]

    if reviewed and not external:
        total = len(counted)
        n_false = len(false_ids)
        # Never default to 0: an empty Findings cell means "no data", 0 means "reviewed, none".
        human = _int(answers, "human_only")
        if human is None:
            raise AnswerError("human_only is required: how many problems did the reviewer find that the AI did not?")
        both = _int(answers, "both_found")
        if both is None:
            if total - n_false > 0:
                raise AnswerError("both_found is required: of the reviewer's problems, how many did the AI also find?")
            both = 0  # no real AI finding, so none can be shared
        lines.append("AI findings counted: %d (%s)%s" % (
            total, ", ".join("%s %s" % (f["key"], f["severity"]) for f in counted) or "none",
            "; duplicates excluded: %s" % ", ".join(sorted(dupes)) if dupes else ""))
        findings_cols = arithmetic(total, n_false, both, human, ", ".join(false_ids) or "none")
        ai_total = total
        if counted:
            severity = max((f["severity"] for f in counted), key=lambda s: SEVERITY_RANK.get(s, 0))
    elif external:
        count_keys = ("ai_findings", "false_count", "both_found", "human_only")
        given = [k for k in count_keys if answers.get(k) not in (None, "")]
        if given and len(given) < len(count_keys):
            raise AnswerError("the Claude review was not on this PR: give all of %s, or none of them to leave "
                              "the Findings columns empty (missing: %s)"
                              % (", ".join(count_keys), ", ".join(k for k in count_keys if k not in given)))
        if given:
            total, n_false = _int(answers, "ai_findings"), _int(answers, "false_count")
            both, human = _int(answers, "both_found"), _int(answers, "human_only")
            if n_false > total:
                raise AnswerError("false_count=%d is more than ai_findings=%d" % (n_false, total))
            lines.append("AI findings counted: %d (reviewer; Claude review not on this PR)" % total)
            findings_cols = arithmetic(total, n_false, both, human, "reviewer")
            ai_total = total
            sev = str(answers.get("severity") or "").strip().capitalize()
            if sev and sev not in SEVERITY_RANK:
                raise AnswerError("severity must be Important, Nit or Pre-existing, got %r" % answers.get("severity"))
            severity = sev if total else ""
        else:
            lines.append("Claude review not on this PR and no counts given: Findings columns left empty "
                         "(no data, not 0).")
    else:
        lines.append("No AI review on this PR: Findings columns left empty (no data, not 0).")

    real_keys = [f["key"] for f in counted if f["key"] not in false_ids]
    fixed_override = answers.get("fixed_ids")
    if fixed_override is not None:
        fixed = known(_ids(answers, "fixed_ids"), "fixed_ids")
        fixed_src = "reviewer"
    else:
        fixed = [k for k in real_keys if by_key[k].get("fixed")]
        fixed_src = "detected from later commits"
    accepted = "YES" if fixed else "NO"
    if answers.get("accepted") is not None:
        accepted = "YES" if str(answers["accepted"]).strip().upper() in ("YES", "Y", "TRUE") else "NO"
        fixed_src = "reviewer"
    recommendation = "YES" if ai_total else "NO"
    if external and answers.get("recommendation") is not None:
        recommendation = "YES" if str(answers["recommendation"]).strip().upper() in ("YES", "Y", "TRUE") else "NO"
    if recommendation == "NO":
        accepted = "NO"
    if external and findings_cols[0] is None:
        basis = "reviewer" if answers.get("recommendation") is not None else "not known, written as NO"
    else:
        basis = "%d AI finding(s)" % ai_total
    lines.append("Recommendation = %s (%s); Accepted = %s (fixed: %s; %s); Severity = %r" % (
        recommendation, basis, accepted, ", ".join(fixed) or "none", fixed_src, severity))

    unresolved = [f["key"] for f in counted if (f.get("thread") or {}).get("resolved") is False]
    note_parts = []
    if external:
        note_parts.append("Claude review not on the PR; %s." % (
            "findings counted by the reviewer" if findings_cols[0] is not None else "findings not counted"))
    elif reviewed and counted:
        note_parts.append("AI findings: " + ", ".join(
            "%s %s%s" % (f["key"], f["severity"], " false" if f["key"] in false_ids
                         else " fixed" if f["key"] in fixed else "") for f in counted) + ".")
    elif reviewed:
        note_parts.append("AI review found no issues.")
    else:
        note_parts.append("No Claude AI review.")
    if unresolved:
        note_parts.append("Unresolved at logging: %s." % ", ".join(unresolved))
    if answers.get("notes"):
        note_parts.append(one_line(answers["notes"]))

    ticket = one_line(answers.get("ticket") or " / ".join(facts.get("tickets") or []) or "N/A")
    row = {
        "Reviewer": reviewer,
        "Date Reviewed": one_line(answers.get("date") or facts["date_reviewed"]),
        "Ticket": ticket,
        "PR": pr_cell(facts),
        "Complexity": complexity,
        "Reviewed By Claude AI": "Yes" if reviewed else "No",
        "Recommendation": recommendation,
        "Accepted Recommendation": accepted,
        "Actual Time (mins)": minutes,
        "Est. Time w/o Claude (min)": _int(answers, "est_minutes"),
        "Notes": one_line(" ".join(note_parts)),
        "Recommendation Severity": severity,
        "Findings: Both Found": findings_cols[0],
        "Findings: AI Only, Real": findings_cols[1],
        "Findings: AI Only, False": findings_cols[2],
        "Findings: Human Only": findings_cols[3],
    }

    eval_cases = []
    if reviewed and human:
        summaries = answers.get("miss_summaries") or []
        project = facts["repo"].split("/", 1)[1].lower()
        slug = re.sub(r"[^a-z0-9]+", "-", project).strip("-") or "repo"
        if not facts.get("merged_at"):
            warnings.append("PR is not merged yet: the eval runner resolves base/head from the merge commit, "
                            "so save the eval case after merge (or replace pr with base/head).")
        for k in range(1, human + 1):
            given = one_line(summaries[k - 1]) if k - 1 < len(summaries) and summaries[k - 1] else ""
            summary = given or EVAL_TODO_SUMMARY
            case_id = "%s-pr%d-miss%d" % (slug, facts["number"], k)
            case = {
                "id": case_id,
                "repo": facts["repo"],
                "pr": facts["number"],
                "known_problem": {"summary": summary, "files": [], "severity": "Important"},
                "source": "human-only-finding",
                "reference": row["PR"],
                "added": today.isoformat(),
            }
            problems = eval_case_errors(case)
            if given and any(e.startswith("known_problem.summary") for e in problems):
                warnings.append("miss %d summary %r is too short for a judge to match (at least %d characters); "
                                "replaced with a TODO" % (k, given, EVAL_SUMMARY_MIN))
                case["known_problem"]["summary"] = "TODO: expand %r -- file, input or state, and what goes wrong" % given
                problems = eval_case_errors(case)
            if problems:
                warnings.append("eval case %s is not valid against eval-case.schema.json: %s" % (
                    case_id, "; ".join(problems)))
            eval_cases.append({
                "path": "evals/review/cases/%s/%s.json" % (slug, case_id),
                "path_base": EVAL_PATH_BASE,
                "valid": not problems,
                "case": case,
            })
    return {"row": row, "arithmetic": lines, "warnings": warnings, "eval_cases": eval_cases}


# Eval case constraints (mirror schemas/eval-case.schema.json; the unit test checks them).
EVAL_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
EVAL_REPO_PATTERN = r"^[^/]+/[^/]+$"
EVAL_SUMMARY_MIN = 10
EVAL_TODO_SUMMARY = "TODO: what the AI missed -- file, input or state, and what goes wrong"
# run_evals.py reads cases from ITS OWN directory (evals/review/cases/<project>), so the path is
# relative to the onion-factory repo (the team's eval set), not the project under review.
EVAL_PATH_BASE = "onion-factory repo root (the team's eval set), not the project repo being reviewed"


def eval_case_errors(case: dict) -> List[str]:
    """Check an eval-case skeleton against the eval-case schema's constraints (stdlib only)."""
    errors = []
    allowed = {"id", "repo", "pr", "base", "head", "known_problem", "source", "reference", "added"}
    errors.extend("%s: not an eval-case field" % k for k in case if k not in allowed)
    if not re.match(EVAL_ID_PATTERN, str(case.get("id") or "")):
        errors.append("id: %r does not match %s" % (case.get("id"), EVAL_ID_PATTERN))
    if not re.match(EVAL_REPO_PATTERN, str(case.get("repo") or "")):
        errors.append("repo: %r is not owner/name" % case.get("repo"))
    pr = case.get("pr")
    if not (isinstance(pr, int) and not isinstance(pr, bool) and pr >= 1):
        errors.append("pr: %r is not a PR number" % pr)
    kp = case.get("known_problem") or {}
    summary = kp.get("summary")
    if not isinstance(summary, str) or len(summary) < EVAL_SUMMARY_MIN:
        errors.append("known_problem.summary: shorter than %d characters" % EVAL_SUMMARY_MIN)
    if kp.get("severity", "Important") not in ("Important", "Nit"):
        errors.append("known_problem.severity: %r not Important or Nit" % kp.get("severity"))
    if case.get("source") not in ("bug-reached-users", "human-only-finding", "other"):
        errors.append("source: %r not allowed" % case.get("source"))
    return errors


def to_tsv(row: dict, columns: List[str]) -> str:
    cells = []
    for col in columns:
        v = row.get(col)
        cells.append("" if v is None else one_line(v))
    return "\t".join(cells)


# ---------------------------------------------------------------------------
# Validation: jsonschema when installed, else a checker for the keywords the
# row schema uses (type, enum, pattern, minimum, minLength, required,
# additionalProperties).
# ---------------------------------------------------------------------------

def find_schema(explicit: Optional[str]) -> Optional[Path]:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        candidates.append(Path(os.environ["CLAUDE_PROJECT_DIR"]) / "schemas" / "review-log-row.schema.json")
    here = Path(__file__).resolve()
    candidates.append(here.parents[5] / "schemas" / "review-log-row.schema.json" if len(here.parents) > 5 else here)
    candidates.append(Path.cwd() / "schemas" / "review-log-row.schema.json")
    for c in candidates:
        if c.is_file() and c.name.endswith(".schema.json"):
            return c
    return None


def _type_ok(value: Any, t: str) -> bool:
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "null": value is None,
        "object": isinstance(value, dict),
    }.get(t, True)


def validate_row(row: dict, schema: dict) -> List[str]:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        jsonschema = None
    if jsonschema is not None:
        cls = jsonschema.validators.validator_for(schema)
        return ["%s: %s" % ("/".join(str(p) for p in e.path) or "(row)", e.message)
                for e in cls(schema).iter_errors(row)]
    errors = []
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in row:
            errors.append("%s: required" % req)
    if schema.get("additionalProperties") is False:
        for k in row:
            if k not in props:
                errors.append("%s: not a column" % k)
    for k, v in row.items():
        p = props.get(k, {})
        types = p.get("type")
        if types is not None:
            types = types if isinstance(types, list) else [types]
            if not any(_type_ok(v, t) for t in types):
                errors.append("%s: %r is not %s" % (k, v, "/".join(types)))
                continue
        if "enum" in p and v not in p["enum"]:
            errors.append("%s: %r not in %s" % (k, v, p["enum"]))
        if isinstance(v, str):
            if "pattern" in p and not re.search(p["pattern"], v):
                errors.append("%s: %r does not match %s" % (k, v, p["pattern"]))
            if len(v) < p.get("minLength", 0):
                errors.append("%s: too short" % k)
        if isinstance(v, int) and not isinstance(v, bool) and "minimum" in p and v < p["minimum"]:
            errors.append("%s: below %s" % (k, p["minimum"]))
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def render_text(result: dict, tsv: str, errors: List[str], schema_path: Optional[Path]) -> str:
    out = []
    if result["warnings"]:
        out.append("== WARNINGS ==")
        out.extend("!! " + w for w in result["warnings"])
        out.append("")
    out.append("== ARITHMETIC ==")
    out.extend(result["arithmetic"])
    out.append("")
    out.append("== TSV (paste into Review Logs, column A) ==")
    out.append(tsv)
    out.append("")
    if errors:
        status = "INVALID"
    elif schema_path:
        status = "valid against %s" % schema_path
    else:
        status = "valid against the embedded copy of review-log-row.schema.json"
    out.append("== JSON (%s) ==" % status)
    out.append(json.dumps(result["row"], indent=2, ensure_ascii=False))
    if errors:
        out.append("")
        out.append("== VALIDATION ERRORS ==")
        out.extend(errors)
    for c in result["eval_cases"]:
        out.append("")
        out.append("== EVAL CASE SKELETON -> %s (relative to the %s)%s ==" % (
            c["path"], c.get("path_base", EVAL_PATH_BASE), "" if c.get("valid", True) else " INVALID"))
        out.append(json.dumps(c["case"], indent=2, ensure_ascii=False))
    return "\n".join(out)


def default_facts_path(repo: str, number: int) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", repo)
    return Path(tempfile.gettempdir()) / "factory-log-review" / ("%s_pr%d.json" % (safe, number))


def load_answers(spec: str) -> dict:
    if spec == "-":
        text = sys.stdin.read()
    elif spec.lstrip()[:1] in ("{", "["):
        text = spec
    else:
        text = Path(spec).read_text()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise AnswerError("answers must be a JSON object")
    return data


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gather", help="read the PR through gh and print facts JSON")
    g.add_argument("pr", help="PR number or URL")
    g.add_argument("--repo", help="OWNER/NAME when pr is a number (default: current repo)")
    g.add_argument("--out", help="where to write the facts JSON (default: temp dir)")
    b = sub.add_parser("build", help="combine facts and answers into the row")
    b.add_argument("--facts", required=True)
    b.add_argument("--answers", required=True, help="inline JSON object, a file path, or - for stdin")
    b.add_argument("--schema", help="path to review-log-row.schema.json")
    b.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "gather":
            facts = gather(args.pr, args.repo)
            out = Path(args.out) if args.out else default_facts_path(facts["repo"], facts["number"])
            out.parent.mkdir(parents=True, exist_ok=True)
            facts["facts_path"] = str(out)
            text = json.dumps(facts, indent=2, ensure_ascii=False)
            out.write_text(text + "\n")
            print(text)
            return 0
        facts = json.loads(Path(args.facts).read_text())
        answers = load_answers(args.answers)
        result = build(facts, answers)
    except UsageError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except AnswerError as e:
        print("answers do not add up: %s" % e, file=sys.stderr)
        return 1

    schema_path = find_schema(args.schema)
    columns = COLUMNS
    if schema_path:
        schema = json.loads(schema_path.read_text())
        columns = schema.get("x-column-order") or COLUMNS
    else:
        schema = EMBEDDED_SCHEMA
    errors = validate_row(result["row"], schema)
    tsv = to_tsv(result["row"], columns)
    if args.format == "json":
        print(json.dumps(dict(result, tsv=tsv, validation_errors=errors,
                              schema=str(schema_path) if schema_path else None), indent=2, ensure_ascii=False))
    else:
        print(render_text(result, tsv, errors, schema_path))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
