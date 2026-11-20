#!/usr/bin/env python3
"""Weekly review scorecard for the AI code-review rollout.

Reads the "Review Logs" tab of the Ancillary Code Reviews sheet (exported as
CSV), computes the rollout plan's measures over the selected rows, and, with
--stage, evaluates that stage's gate. Python 3.9, standard library only.

    python3 scorecard/scorecard.py --csv review-logs.csv --repo acme/widgets --stage 2

Exit status: 0 whenever a scorecard was produced (whatever the verdict),
2 when an input cannot be read (CSV missing, no header row, eval result
unreadable) or the arguments are invalid.

These numbers judge the review agent, never a person: the scorecard never
reports anything per reviewer.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import statistics
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

FOOTER = "These numbers judge the review agent, never a person."

# Column headers of the Review Logs tab, verbatim (schemas/review-log-row.schema.json).
COL_REVIEWER = "Reviewer"
COL_DATE = "Date Reviewed"
COL_TICKET = "Ticket"
COL_PR = "PR"
COL_COMPLEXITY = "Complexity"
COL_CLAUDE = "Reviewed By Claude AI"
COL_RECOMMENDATION = "Recommendation"
COL_ACCEPTED = "Accepted Recommendation"
COL_TIME = "Actual Time (mins)"
COL_EST_TIME = "Est. Time w/o Claude (min)"
COL_NOTES = "Notes"
COL_SEVERITY = "Recommendation Severity"
COL_BOTH = "Findings: Both Found"
COL_AI_REAL = "Findings: AI Only, Real"
COL_AI_FALSE = "Findings: AI Only, False"
COL_HUMAN_ONLY = "Findings: Human Only"

COLUMNS = [
    COL_REVIEWER, COL_DATE, COL_TICKET, COL_PR, COL_COMPLEXITY, COL_CLAUDE,
    COL_RECOMMENDATION, COL_ACCEPTED, COL_TIME, COL_EST_TIME, COL_NOTES,
    COL_SEVERITY, COL_BOTH, COL_AI_REAL, COL_AI_FALSE, COL_HUMAN_ONLY,
]

FALSE_RATE_TARGET = 0.2          # false finding rate must be below this
DEFAULT_EVAL_THRESHOLD = 8       # used only when a result omits "threshold"
STAGE_WINDOWS = {2: 15, 3: 30}   # gate window, in PRs
COST_VOLUME_DAYS = 30            # reviews counted for the spend projection
TIME_COVERAGE_MIN = 0.9          # share of gate-window PRs that must have a review time
MIN_BASELINE_PRS = 5             # baseline PRs with a time needed to judge review time

PASS = "PASS"
FAIL = "FAIL"
NO_DATA = "NO DATA"
MANUAL = "MANUAL CHECK"


class InputError(Exception):
    """An input file cannot be read; the CLI exits 2."""


# --------------------------------------------------------------------------
# Cell parsing
# --------------------------------------------------------------------------

def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\ufeff", "")).strip().lower()


_KNOWN_HEADERS = {_norm_header(c): c for c in COLUMNS}

_MONTHS = {
    "jan": (1, "january"), "feb": (2, "february"), "mar": (3, "march"),
    "apr": (4, "april"), "may": (5, "may"), "jun": (6, "june"),
    "jul": (7, "july"), "aug": (8, "august"), "sep": (9, "september"),
    "oct": (10, "october"), "nov": (11, "november"), "dec": (12, "december"),
}
_DATE_WORDS = re.compile(
    r"^(?:[A-Za-z]+,\s*)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})$")
_DATE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$")
_DATE_US = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def parse_date(value: Optional[str]) -> Optional[dt.date]:
    """Parse the sheet's date spellings; None when empty or unrecognised.

    Accepts "Apr 01 2026", "May 13, 2026", "Sept 23 2026", "Aug 1, 2026",
    "September 3, 2026", "2026-04-01" and US "4/1/2026".
    """
    if value is None:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return None
    try:
        m = _DATE_WORDS.match(text)
        if m:
            word = m.group(1).lower()
            month = _MONTHS.get(word[:3])
            if month is None or not month[1].startswith(word):
                return None
            return dt.date(int(m.group(3)), month[0], int(m.group(2)))
        m = _DATE_ISO.match(text)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = _DATE_US.match(text)
        if m:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:  # e.g. "Feb 31 2026"
        return None
    return None


_INT = re.compile(r"^(\d+)(?:\.0+)?$")


def parse_int(value: Optional[str]) -> Optional[int]:
    """" 15 " -> 15. Empty, whitespace, text or negative -> None (no data, never 0)."""
    if value is None:
        return None
    m = _INT.match(value.strip())
    return int(m.group(1)) if m else None


def parse_yes_no(value: Optional[str]) -> Optional[bool]:
    text = (value or "").strip().lower()
    if text in ("yes", "y", "true"):
        return True
    if text in ("no", "n", "false"):
        return False
    return None


_PR_URL = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)", re.I)
_PR_HEAD = re.compile(r"^#\s*(\d+)\s*(?:[-\u2013\u2014:]\s*)?(.*)$", re.S)


def parse_pr(value: Optional[str]) -> Tuple[Optional[str], Optional[int], str]:
    """Return (repo "owner/name" lower-cased, PR number, title) from a PR cell.

    Handles "#412 - Title (https://github.com/o/r/pull/412)", with or without a
    line break before the "(url)". The number in the URL wins over "#n".
    """
    text = value or ""
    repo = None
    number = None
    m = _PR_URL.search(text)
    if m:
        repo = "{}/{}".format(m.group(1), m.group(2)).lower()
        number = int(m.group(3))
        text = text[:m.start()] + text[m.end():]
        text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    title = text
    h = _PR_HEAD.match(text)
    if h:
        if number is None:
            number = int(h.group(1))
        title = h.group(2).strip()
    return repo, number, title


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------

@dataclass
class Row:
    """One parsed Review Logs row. Reviewer names are deliberately not kept."""
    record: int                       # 1-based CSV record number (header included)
    date: Optional[dt.date]
    date_raw: str
    ticket: str
    repo: Optional[str]
    pr_number: Optional[int]
    pr_title: str
    reviewed_by_claude: Optional[bool]
    actual_time: Optional[int]
    severity: str
    both: Optional[int]
    ai_real: Optional[int]
    ai_false: Optional[int]
    human_only: Optional[int]
    order_date: dt.date = dt.date.min  # date used for ordering (forward-filled)

    @property
    def pr_key(self) -> str:
        if self.pr_number is not None:
            return "{}#{}".format(self.repo or "?", self.pr_number)
        return "record:{}".format(self.record)

    @property
    def pr_label(self) -> str:
        if self.pr_number is None:
            return "row {}".format(self.record)
        return "{}#{}".format(self.repo, self.pr_number) if self.repo else "#{}".format(self.pr_number)

    @property
    def has_ai_counts(self) -> bool:
        return None not in (self.both, self.ai_real, self.ai_false)

    @property
    def is_logged(self) -> bool:
        """All four findings columns are filled (0 counts as filled, blank does not)."""
        return self.has_ai_counts and self.human_only is not None


@dataclass
class Sheet:
    rows: List[Row]
    warnings: List[str]


def _is_header_like(cells: Sequence[str]) -> bool:
    hits = {_KNOWN_HEADERS[n] for n in (_norm_header(c) for c in cells) if n in _KNOWN_HEADERS}
    return COL_PR in hits and len(hits) >= 4


def read_sheet(path: str) -> Sheet:
    """Read a Review Logs CSV export by header name. Raises InputError."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            records = list(csv.reader(fh))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise InputError("cannot read CSV {}: {}".format(path, exc))

    warnings: List[str] = []
    header_at = None
    for i, cells in enumerate(records):
        if _is_header_like(cells):
            header_at = i
            break
    if header_at is None:
        raise InputError(
            "no header row in {} (expected the Review Logs headers, including 'PR')".format(path))
    if header_at:
        warnings.append("skipped {} row(s) above the header".format(header_at))

    index: Dict[str, int] = {}
    for pos, cell in enumerate(records[header_at]):
        name = _KNOWN_HEADERS.get(_norm_header(cell))
        if name and name not in index:
            index[name] = pos
    missing = [c for c in COLUMNS if c not in index]
    if missing:
        warnings.append("columns missing from the CSV, treated as no data: " + ", ".join(missing))

    rows: List[Row] = []
    extra_headers = 0
    blank = 0
    for i in range(header_at + 1, len(records)):
        cells = records[i]
        if not any(c.strip() for c in cells):
            blank += 1
            continue
        if _is_header_like(cells):
            extra_headers += 1
            continue

        def cell(name: str) -> str:
            pos = index.get(name)
            if pos is None or pos >= len(cells):  # short rows: trailing columns missing
                return ""
            return cells[pos]

        if not (cell(COL_PR).strip() or cell(COL_DATE).strip() or cell(COL_REVIEWER).strip()):
            warnings.append("row {}: no PR, date or reviewer; skipped".format(i + 1))
            continue
        repo, number, title = parse_pr(cell(COL_PR))
        date_raw = re.sub(r"\s+", " ", cell(COL_DATE)).strip()
        date = parse_date(date_raw)
        if date is None and date_raw:
            warnings.append("row {}: unrecognised date {!r}".format(i + 1, date_raw))
        rows.append(Row(
            record=i + 1,
            date=date,
            date_raw=date_raw,
            ticket=cell(COL_TICKET).strip(),
            repo=repo,
            pr_number=number,
            pr_title=title,
            reviewed_by_claude=parse_yes_no(cell(COL_CLAUDE)),
            actual_time=parse_int(cell(COL_TIME)),
            severity=cell(COL_SEVERITY).strip(),
            both=parse_int(cell(COL_BOTH)),
            ai_real=parse_int(cell(COL_AI_REAL)),
            ai_false=parse_int(cell(COL_AI_FALSE)),
            human_only=parse_int(cell(COL_HUMAN_ONLY)),
        ))
    if extra_headers:
        warnings.append("skipped {} repeated header row(s)".format(extra_headers))

    # Ordering: by date; an undated row keeps its place in the sheet by taking
    # the date of the nearest dated row above it.
    last = dt.date.min
    for row in rows:
        if row.date is not None:
            last = row.date
        row.order_date = last
    rows.sort(key=lambda r: r.order_date)  # stable: sheet order breaks ties
    return Sheet(rows=rows, warnings=warnings)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

def filter_repos(rows: Iterable[Row], repos: Optional[Sequence[str]]) -> List[Row]:
    if not repos:
        return list(rows)
    wanted = {r.strip().lower() for r in repos}
    return [r for r in rows if r.repo in wanted]


def _record_list(rows: Sequence[Row]) -> str:
    return _short_list([str(r.record) for r in rows])


def pr_rows(rows: Sequence[Row], repos: Optional[Sequence[str]], warnings: List[str]) -> List[Row]:
    """Rows that identify one PR unambiguously, so each counts once in a PR window.

    With --repo: rows whose PR URL is in those repos. Without it: rows with a PR
    number; and when some rows carry a PR URL, rows without one are left out too,
    since "#5" alone cannot be matched to "acme/widgets#5" in a multi-repo sheet.
    """
    if repos:
        no_repo = sum(1 for r in rows if r.repo is None)
        if no_repo:
            warnings.append("{} row(s) have no PR URL and were left out by --repo".format(no_repo))
        return filter_repos(rows, repos)
    no_number = [r for r in rows if r.pr_number is None]
    out = [r for r in rows if r.pr_number is not None]
    if no_number:
        warnings.append("row(s) {}: no PR number in the PR cell; left out of the scorecard".format(
            _record_list(no_number)))
    if any(r.repo for r in out):
        no_repo = [r for r in out if r.repo is None]
        if no_repo:
            out = [r for r in out if r.repo]
            warnings.append("row(s) {}: PR cell has no PR URL, so the repo is unknown; left out of "
                            "the scorecard".format(_record_list(no_repo)))
    return out


def last_prs(rows: Sequence[Row], n: int) -> List[Row]:
    """All rows of the newest n distinct PRs (rows must be in date order)."""
    keys: List[str] = []
    for row in reversed(rows):
        if row.pr_key not in keys:
            if len(keys) == n:
                break
            keys.append(row.pr_key)
    chosen = set(keys)
    return [r for r in rows if r.pr_key in chosen]


def distinct_prs(rows: Iterable[Row]) -> List[str]:
    seen: List[str] = []
    for r in rows:
        if r.pr_key not in seen:
            seen.append(r.pr_key)
    return seen


def time_per_pr(rows: Iterable[Row]) -> List[int]:
    """Review minutes per PR: rows of one PR are summed; rows with no number in Actual Time add nothing."""
    totals: Dict[str, int] = {}
    for r in rows:
        if r.actual_time is not None:
            totals[r.pr_key] = totals.get(r.pr_key, 0) + r.actual_time
    return list(totals.values())


# --------------------------------------------------------------------------
# Eval results
# --------------------------------------------------------------------------

def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


def _valid_eval(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    score = data.get("score")
    if not isinstance(score, dict):
        return False
    return all(isinstance(score.get(k), int) and not isinstance(score.get(k), bool)
               for k in ("found", "total", "errors"))


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_newest_eval(path: str, warnings: List[str]) -> Optional[Dict[str, Any]]:
    """Return the newest eval result under `path` (file or directory), with "_source".

    A missing path, or a single file that is not an eval result, raises
    InputError. In a directory, JSON files that are not eval results are
    skipped with a warning; None is returned when none is found.
    """
    if os.path.isfile(path):
        try:
            data = _load_json(path)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise InputError("cannot read eval result {}: {}".format(path, exc))
        if not _valid_eval(data):
            raise InputError("{} is not an eval result (see schemas/eval-result.schema.json)".format(path))
        data["_source"] = path
        return data
    if not os.path.isdir(path):
        raise InputError("eval results path not found: {}".format(path))

    candidates: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            if not name.endswith(".json"):
                continue
            full = os.path.join(root, name)
            try:
                data = _load_json(full)
            except (OSError, UnicodeDecodeError, ValueError):
                warnings.append("skipped unreadable eval result {}".format(full))
                continue
            if not _valid_eval(data):
                warnings.append("skipped {}: not an eval result".format(full))
                continue
            data["_source"] = full
            stamp = _parse_ts(data.get("started_at"))
            key = (stamp.timestamp() if stamp else float("-inf"),
                   str(data.get("run_id", "")), os.path.getmtime(full))
            candidates.append((key, data))
    if not candidates:
        return None
    projects = {str(d.get("project")) for _k, d in candidates}
    newest = max(candidates, key=lambda kd: kd[0])[1]
    if len(projects) > 1:
        warnings.append(
            "eval results from several projects ({}); newest used: {}. Point --eval-results "
            "at evals/review/results/<project> to choose one.".format(
                ", ".join(sorted(projects)), newest.get("project")))
    return newest


# --------------------------------------------------------------------------
# Measures
# --------------------------------------------------------------------------

def _ratio(num: float, den: float) -> Optional[float]:
    return (num / den) if den else None


def measure_false_rate(rows: Sequence[Row]) -> Dict[str, Any]:
    data = [r for r in rows if r.has_ai_counts]
    false = sum(r.ai_false for r in data)  # type: ignore[misc]
    total = sum(r.both + r.ai_real + r.ai_false for r in data)  # type: ignore[operator]
    value = _ratio(false, total)
    return {
        "value": value,
        "ai_only_false": false,
        "ai_findings": total,
        "rows_with_data": len(data),
        "rows": len(rows),
        "target": "< {}".format(FALSE_RATE_TARGET),
        "met": None if value is None else value < FALSE_RATE_TARGET,
    }


def measure_human_only(rows: Sequence[Row]) -> Dict[str, Any]:
    """Human Only findings per PR: rows of one PR are summed (like review time),
    over the PRs with at least one row where the column is filled."""
    per_pr: Dict[str, int] = {}
    for r in rows:
        if r.human_only is not None:
            per_pr[r.pr_key] = per_pr.get(r.pr_key, 0) + r.human_only
    total = sum(per_pr.values())
    return {
        "value": _ratio(total, len(per_pr)),
        "human_only": total,
        "prs_with_data": len(per_pr),
        "prs": len(distinct_prs(rows)),
        "rows_with_data": sum(1 for r in rows if r.human_only is not None),
        "rows": len(rows),
        "target": "falling",
    }


def measure_coverage(rows: Sequence[Row]) -> Dict[str, Any]:
    reviewed = sum(1 for r in rows if r.reviewed_by_claude is True)
    return {
        "value": _ratio(reviewed, len(rows)),
        "reviewed": reviewed,
        "rows": len(rows),
        "target": "every PR",
    }


def _stats(values: Sequence[int]) -> Dict[str, Any]:
    if not values:
        return {"median": None, "mean": None, "prs_with_time": 0}
    return {
        "median": float(statistics.median(values)),
        "mean": round(float(statistics.mean(values)), 1),
        "prs_with_time": len(values),
    }


def measure_review_time(rows: Sequence[Row], baseline_rows: Optional[Sequence[Row]],
                        baseline_window: Optional[Tuple[dt.date, dt.date]]) -> Dict[str, Any]:
    """Median review minutes per PR vs the baseline median.

    "met" is the bare comparison of the two medians. "enough_data" says whether
    the comparison may judge a gate: at least TIME_COVERAGE_MIN of the PRs have
    a time and the baseline has at least MIN_BASELINE_PRS PRs with a time.
    """
    out = _stats(time_per_pr(rows))
    out["prs"] = len(distinct_prs(rows))
    out["prs_needed"] = int(math.ceil(round(TIME_COVERAGE_MIN * out["prs"], 6)))
    out["baseline"] = None
    out["met"] = None
    out["enough_data"] = False
    if baseline_window is not None and baseline_rows is not None:
        base = _stats(time_per_pr(baseline_rows))
        base["from"] = baseline_window[0].isoformat()
        base["to"] = baseline_window[1].isoformat()
        base["prs_needed"] = MIN_BASELINE_PRS
        out["baseline"] = base
        if out["median"] is not None and base["median"] is not None:
            out["met"] = out["median"] <= base["median"]
            out["enough_data"] = (out["prs"] > 0 and out["prs_with_time"] >= out["prs_needed"]
                                  and base["prs_with_time"] >= MIN_BASELINE_PRS)
    out["target"] = "<= baseline median"
    return out


def measure_catch_rate(result: Optional[Dict[str, Any]], note: str) -> Dict[str, Any]:
    if result is None:
        return {"value": None, "note": note}
    score = result["score"]
    found, total, errors = score["found"], score["total"], score["errors"]
    threshold = result.get("threshold")
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        threshold = DEFAULT_EVAL_THRESHOLD
    passed = (found >= threshold and errors == 0) if total else None
    return {
        "value": _ratio(found, total),
        "found": found,
        "total": total,
        "errors": errors,
        "threshold": threshold,
        "passed": passed,
        "passed_field": result.get("passed"),
        "run_id": result.get("run_id"),
        "project": result.get("project"),
        "started_at": result.get("started_at"),
        "source": result.get("_source"),
        "note": note,
    }


def measure_cost(all_rows: Sequence[Row], cost: Optional[float], limit: Optional[float],
                 monthly_spend: Optional[float] = None) -> Dict[str, Any]:
    """Compare spend with the org-wide monthly spend limit.

    The authoritative figure is `monthly_spend`, read from the Claude admin
    page. Without it the sheet gives only a LOWER BOUND: cost per review x
    distinct Claude-reviewed PRs (all repos in the sheet, whatever --repo says)
    dated within the COST_VOLUME_DAYS days that end on the newest dated row.
    It undercounts because "After every push" runs one review per push and
    repos not logged in the sheet also spend. So the lower bound can show that
    spend is over the limit, never that it is inside it.
    """
    dated = [r for r in all_rows if r.date is not None]
    end = max((r.date for r in dated), default=None)  # type: ignore[type-var]
    volume = None
    start = None
    if end is not None:
        start = end - dt.timedelta(days=COST_VOLUME_DAYS - 1)
        volume = len(distinct_prs(
            r for r in dated if r.reviewed_by_claude is True and start <= r.date <= end))  # type: ignore[operator]
    projected = None
    if cost is not None and volume is not None:
        projected = round(cost * volume, 2)
    within = None
    basis = None
    if limit is not None:
        if monthly_spend is not None:
            within, basis = monthly_spend <= limit, "monthly_spend"
        elif projected is not None and projected > limit:
            within, basis = False, "lower_bound"
    return {
        "cost_per_review": cost,
        "spend_limit": limit,
        "monthly_spend": monthly_spend,
        "volume_window": None if end is None else {"from": start.isoformat(), "to": end.isoformat()},  # type: ignore[union-attr]
        "reviews_in_window": volume,
        "projected_spend": projected,
        "projection_is_lower_bound": True,
        "within_limit": within,
        "within_limit_basis": basis,
    }


# --------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------

def _item(key: str, name: str, status: str, detail: str) -> Dict[str, str]:
    return {"id": key, "name": name, "status": status, "detail": detail}


def _fmt_rate(value: Optional[float]) -> str:
    return "n/a" if value is None else "{:.2f}".format(value)


def evaluate_gate(stage: int, window_rows: Sequence[Row], measures: Dict[str, Any]) -> Dict[str, Any]:
    required = STAGE_WINDOWS[stage]
    prs = distinct_prs(window_rows)
    items: List[Dict[str, str]] = []

    n = len(prs)
    items.append(_item(
        "window", "{} PRs in a row".format(required),
        PASS if n >= required else NO_DATA,
        "{} of {} PRs logged in the window".format(n, required)))

    # Every PR reviewed by the agent and logged with all four findings counts.
    by_pr: Dict[str, List[Row]] = {}
    for r in window_rows:
        by_pr.setdefault(r.pr_key, []).append(r)
    not_reviewed = [rs[0].pr_label for rs in by_pr.values()
                    if not any(r.reviewed_by_claude is True for r in rs)]
    not_logged = [rs[0].pr_label for rs in by_pr.values() if not any(r.is_logged for r in rs)]
    if not by_pr:
        status, detail = NO_DATA, "no PRs in the window"
    elif not_reviewed or not_logged:
        parts = []
        if not_reviewed:
            parts.append("not reviewed by Claude: " + _short_list(not_reviewed))
        if not_logged:
            parts.append("findings not logged: " + _short_list(not_logged))
        status, detail = FAIL, "; ".join(parts)
    else:
        status, detail = PASS, "all {} PRs reviewed and logged".format(len(by_pr))
    items.append(_item("reviewed_and_logged", "every PR reviewed and logged", status, detail))

    fr = measures["false_finding_rate"]
    if fr["value"] is None:
        status = NO_DATA
        detail = "no AI findings logged" if fr["rows_with_data"] else "no rows with AI findings counts"
    else:
        status = PASS if fr["met"] else FAIL
        detail = "{} ({} false of {} AI findings, {} rows)".format(
            _fmt_rate(fr["value"]), fr["ai_only_false"], fr["ai_findings"], fr["rows_with_data"])
    items.append(_item("false_finding_rate", "false finding rate < {}".format(FALSE_RATE_TARGET),
                       status, detail))

    cr = measures["catch_rate"]
    if cr.get("passed") is None:
        status, detail = NO_DATA, cr.get("note") or "no eval run"
    else:
        status = PASS if cr["passed"] else FAIL
        detail = "{}/{} found, {} errors, run {}".format(
            cr["found"], cr["total"], cr["errors"], cr.get("run_id"))
    threshold = cr.get("threshold", DEFAULT_EVAL_THRESHOLD)
    items.append(_item("eval", "eval catch >= {}".format(threshold), status, detail))

    items.append(_item("unanswered_findings", "none merged with an unanswered finding", MANUAL,
                       "check the main-branch ruleset (all conversations resolved) held for these PRs"))

    if stage == 2:
        items.append(_item("team_says_helps", "team says findings help", MANUAL,
                           "ask at the weekly tuning meeting"))
    else:
        rt = measures["review_time"]
        if rt["baseline"] is None:
            status, detail = NO_DATA, "no baseline (pass --baseline-from and --baseline-to)"
        else:
            base = rt["baseline"]
            detail = "median {} min ({} of {} PRs have a time) vs baseline {} min ({})".format(
                _fmt_num(rt["median"]), rt["prs_with_time"], rt["prs"],
                _fmt_num(base["median"]), _prs(base["prs_with_time"]))
            short = []
            if rt["prs"] == 0 or rt["prs_with_time"] < rt["prs_needed"]:
                short.append("need a time on at least {} of {} PRs".format(rt["prs_needed"], rt["prs"]))
            if base["prs_with_time"] < MIN_BASELINE_PRS:
                short.append("need at least {} baseline PRs with a time".format(MIN_BASELINE_PRS))
            if rt["met"] is None or not rt["enough_data"]:
                status = NO_DATA
                if short:
                    detail += "; " + "; ".join(short)
            else:
                status = PASS if rt["met"] else FAIL
        items.append(_item("review_time", "review time per PR <= baseline", status, detail))

        cost = measures["cost"]
        if cost["cost_per_review"] is None or cost["spend_limit"] is None:
            status, detail = NO_DATA, "pass --cost-per-review, --spend-limit and --monthly-spend"
        elif cost["within_limit_basis"] == "monthly_spend":
            status = PASS if cost["within_limit"] else FAIL
            detail = "${:.2f}/review; monthly spend ${:.2f} vs limit ${:.2f}".format(
                cost["cost_per_review"], cost["monthly_spend"], cost["spend_limit"])
        elif cost["within_limit_basis"] == "lower_bound":
            status = FAIL
            detail = "${:.2f} x at least {} reviews = at least ${:.2f}, over limit ${:.2f}".format(
                cost["cost_per_review"], cost["reviews_in_window"],
                cost["projected_spend"], cost["spend_limit"])
        else:
            status = NO_DATA
            if cost["projected_spend"] is None:
                detail = "no dated rows; pass --monthly-spend from the Claude admin page"
            else:
                detail = ("sheet projection ${:.2f} ({} Claude-reviewed PRs in {} days) is only a lower "
                          "bound; pass --monthly-spend from the Claude admin page").format(
                    cost["projected_spend"], cost["reviews_in_window"], COST_VOLUME_DAYS)
        items.append(_item("cost", "cost per review known and inside spend limit", status, detail))

    statuses = [i["status"] for i in items if i["status"] != MANUAL]
    if FAIL in statuses:
        verdict = FAIL
    elif NO_DATA in statuses:
        verdict = NO_DATA
    else:
        verdict = PASS
    return {
        "stage": stage,
        "window_prs": required,
        "prs_in_window": n,
        "verdict": verdict,
        "manual_checks": sum(1 for i in items if i["status"] == MANUAL),
        "items": items,
    }


def _short_list(labels: Sequence[str], limit: int = 5) -> str:
    shown = ", ".join(labels[:limit])
    if len(labels) > limit:
        shown += " and {} more".format(len(labels) - limit)
    return shown


def _prs(n: int) -> str:
    return "1 PR" if n == 1 else "{} PRs".format(n)


def _fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return "{:g}".format(value)


# --------------------------------------------------------------------------
# Scorecard
# --------------------------------------------------------------------------

def build_scorecard(sheet: Sheet, *, repos: Optional[Sequence[str]] = None,
                    since: Optional[dt.date] = None, last: Optional[int] = None,
                    baseline: Optional[Tuple[dt.date, dt.date]] = None,
                    eval_result: Optional[Dict[str, Any]] = None, eval_note: str = "",
                    cost_per_review: Optional[float] = None, spend_limit: Optional[float] = None,
                    monthly_spend: Optional[float] = None,
                    stage: Optional[int] = None, warnings: Optional[List[str]] = None,
                    today: Optional[dt.date] = None) -> Dict[str, Any]:
    warnings = list(sheet.warnings) + list(warnings or [])
    repo_rows = pr_rows(sheet.rows, repos, warnings)

    candidates = repo_rows
    if since is not None:
        candidates = [r for r in candidates if r.date is not None and r.date >= since]
    window = last if last is not None else (STAGE_WINDOWS[stage] if stage else None)
    selected = last_prs(candidates, window) if window is not None else candidates

    baseline_rows = None
    if baseline is not None:
        baseline_rows = [r for r in repo_rows
                         if r.date is not None and baseline[0] <= r.date <= baseline[1]]

    catch = measure_catch_rate(eval_result, eval_note)
    # Spend is checked against an org-wide limit, so the lower-bound projection
    # counts every repo in the sheet, not only the --repo selection.
    cost = measure_cost(pr_rows(sheet.rows, None, []), cost_per_review, spend_limit, monthly_spend)

    def measures_for(rows: Sequence[Row]) -> Dict[str, Any]:
        return {
            "false_finding_rate": measure_false_rate(rows),
            "catch_rate": catch,
            "human_only_per_pr": measure_human_only(rows),
            "coverage": measure_coverage(rows),
            "review_time": measure_review_time(rows, baseline_rows, baseline),
            "cost": cost,
            "unanswered_findings_at_merge": {
                "value": None, "target": 0,
                "note": "manual check: the main-branch ruleset requires all conversations resolved",
            },
        }

    measures = measures_for(selected)
    gate = None
    if stage:
        # The gate is always judged over the plan's window (15 or 30 PRs);
        # --last only shapes the measures table.
        gate_rows = last_prs(candidates, STAGE_WINDOWS[stage])
        if last is not None and last != STAGE_WINDOWS[stage]:
            warnings.append("--last {} applies to the measures table; the Stage {} gate uses the "
                            "last {} PRs".format(last, stage, STAGE_WINDOWS[stage]))
            gate = evaluate_gate(stage, gate_rows, measures_for(gate_rows))
        else:
            gate = evaluate_gate(stage, gate_rows, measures)
    dates = [r.date for r in selected if r.date is not None]
    card = {
        "generated_on": (today or dt.date.today()).isoformat(),
        "filters": {
            "repos": sorted({r.lower() for r in repos}) if repos else [],
            "since": since.isoformat() if since else None,
            "last_prs": window,
            "baseline": None if baseline is None else {
                "from": baseline[0].isoformat(), "to": baseline[1].isoformat()},
        },
        "selection": {
            "rows": len(selected),
            "prs": len(distinct_prs(selected)),
            "repos": sorted({r.repo for r in selected if r.repo}),
            "first_date": min(dates).isoformat() if dates else None,
            "last_date": max(dates).isoformat() if dates else None,
        },
        "measures": measures,
        "gate": gate,
        "warnings": warnings,
        "footer": FOOTER,
    }
    return card


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else "{:.0f}%".format(value * 100)


def measure_lines(card: Dict[str, Any]) -> List[Tuple[str, str, str, str]]:
    """(measure, value, target, data) rows shared by the text and markdown outputs."""
    m = card["measures"]
    out: List[Tuple[str, str, str, str]] = []

    fr = m["false_finding_rate"]
    value = NO_DATA if fr["value"] is None else "{} ({} of {})".format(
        _fmt_rate(fr["value"]), fr["ai_only_false"], fr["ai_findings"])
    out.append(("False finding rate", value, "< {:.2f}".format(FALSE_RATE_TARGET),
                "{} of {} rows have AI counts".format(fr["rows_with_data"], fr["rows"])))

    cr = m["catch_rate"]
    if cr["value"] is None:
        out.append(("Catch rate (eval)", NO_DATA, ">= {}".format(DEFAULT_EVAL_THRESHOLD),
                    cr.get("note") or "no eval run"))
    else:
        verdict = "passed" if cr["passed"] else "failed"
        errors = ", {} errors".format(cr["errors"]) if cr["errors"] else ""
        out.append(("Catch rate (eval)", "{}/{} ({}{})".format(cr["found"], cr["total"], verdict, errors),
                    ">= {}".format(cr["threshold"]), "run {}".format(cr.get("run_id"))))

    ho = m["human_only_per_pr"]
    value = NO_DATA if ho["value"] is None else "{:.2f} ({} in {})".format(
        ho["value"], ho["human_only"], _prs(ho["prs_with_data"]))
    out.append(("Agent misses (Human Only/PR)", value, "falling",
                "{} of {} PRs filled".format(ho["prs_with_data"], ho["prs"])))

    cv = m["coverage"]
    value = NO_DATA if cv["value"] is None else "{}/{} ({})".format(
        cv["reviewed"], cv["rows"], _pct(cv["value"]))
    out.append(("Coverage (Claude = Yes)", value, "every PR", "rows reviewed by Claude AI"))

    rt = m["review_time"]
    value = NO_DATA if rt["median"] is None else "{} min median, {} mean".format(
        _fmt_num(rt["median"]), _fmt_num(rt["mean"]))
    base = rt["baseline"]
    with_time = "{} of {} PRs have a time".format(rt["prs_with_time"], rt["prs"])
    if base is None:
        target, data = "no baseline", with_time
    else:
        target = "<= {} min".format(_fmt_num(base["median"])) if base["median"] is not None \
            else "baseline: no data"
        data = "{}; baseline {}..{}, {} with a time".format(
            with_time, base["from"], base["to"], _prs(base["prs_with_time"]))
    out.append(("Review time per PR", value, target, data))

    cost = m["cost"]
    if cost["cost_per_review"] is None:
        value, data = "not entered", "pass --cost-per-review (Claude admin page)"
    else:
        value = "${:.2f}/review".format(cost["cost_per_review"])
        if cost["monthly_spend"] is not None:
            data = "monthly spend ${:.2f} (admin page)".format(cost["monthly_spend"])
        elif cost["projected_spend"] is None:
            data = "no dated rows; pass --monthly-spend"
        else:
            data = "at least ${:.2f} ({} Claude-reviewed PRs in {} days, all repos); pass --monthly-spend".format(
                cost["projected_spend"], cost["reviews_in_window"], COST_VOLUME_DAYS)
    target = "no limit given" if cost["spend_limit"] is None else "<= ${:.2f}".format(cost["spend_limit"])
    out.append(("Cost per review", value, target, data))

    out.append(("Unanswered findings at merge", MANUAL.lower(), "0", "ruleset enforces"))
    return out


def _title_lines(card: Dict[str, Any]) -> List[str]:
    sel = card["selection"]
    f = card["filters"]
    scope = ", ".join(f["repos"]) if f["repos"] else (", ".join(sel["repos"]) or "all repos")
    bits = ["{} rows, {} PRs".format(sel["rows"], sel["prs"])]
    if sel["first_date"]:
        bits.append("{} to {}".format(sel["first_date"], sel["last_date"]))
    if f["last_prs"]:
        bits.append("last {} PRs".format(f["last_prs"]))
    if f["since"]:
        bits.append("since {}".format(f["since"]))
    return ["Review scorecard: {} ({})".format(scope, card["generated_on"]), "; ".join(bits)]


def render_text(card: Dict[str, Any]) -> str:
    lines = _title_lines(card)
    lines.append("")
    rows = [("Measure", "Value", "Target", "Data")] + measure_lines(card)
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        lines.append("  ".join(r[i].ljust(widths[i]) for i in range(3)) + "  " + r[3])
    gate = card["gate"]
    if gate:
        lines.append("")
        lines.append("Gate: Stage {} (last {} PRs): {}".format(gate["stage"], gate["window_prs"], gate["verdict"]))
        status_w = max(len(i["status"]) for i in gate["items"]) + 2
        name_w = max(len(i["name"]) for i in gate["items"])
        for i in gate["items"]:
            lines.append("  {}  {}  {}".format(
                ("[" + i["status"] + "]").ljust(status_w), i["name"].ljust(name_w), i["detail"]))
        if gate["manual_checks"]:
            lines.append("  Manual checks must also hold before the gate is passed.")
    lines.append("")
    lines.append(card["footer"])
    return "\n".join(line.rstrip() for line in lines) + "\n"


def _md(cell: str) -> str:
    return cell.replace("|", "\\|").replace("\n", " ")


def render_markdown(card: Dict[str, Any]) -> str:
    title, sub = _title_lines(card)
    lines = ["### " + _md(title), "", _md(sub), "",
             "| Measure | Value | Target | Data |", "|---|---|---|---|"]
    for r in measure_lines(card):
        lines.append("| " + " | ".join(_md(c) for c in r) + " |")
    gate = card["gate"]
    if gate:
        lines += ["", "**Gate: Stage {} (last {} PRs): {}**".format(
            gate["stage"], gate["window_prs"], gate["verdict"]), "",
            "| Status | Gate item | Detail |", "|---|---|---|"]
        for i in gate["items"]:
            lines.append("| {} | {} | {} |".format(_md(i["status"]), _md(i["name"]), _md(i["detail"])))
        if gate["manual_checks"]:
            lines += ["", "Manual checks must also hold before the gate is passed."]
    lines += ["", "_{}_".format(card["footer"])]
    return "\n".join(lines) + "\n"


def render_json(card: Dict[str, Any]) -> str:
    return json.dumps(card, indent=2, sort_keys=False) + "\n"


RENDERERS = {"text": render_text, "markdown": render_markdown, "json": render_json}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _iso_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD, got {!r}".format(value))


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError("expected a positive integer, got {!r}".format(value))
    return n


def _money(value: str) -> float:
    try:
        n = float(value)
    except ValueError:
        n = -1.0
    if n < 0:
        raise argparse.ArgumentTypeError("expected an amount >= 0, got {!r}".format(value))
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scorecard.py",
        description="Weekly scorecard for the AI review rollout, from the Review Logs CSV export.",
        epilog=FOOTER)
    p.add_argument("--csv", required=True, help="Review Logs tab exported as CSV")
    p.add_argument("--repo", action="extend", nargs="+", metavar="OWNER/NAME",
                   help="only rows whose PR URL is in these repos (repeatable)")
    p.add_argument("--since", type=_iso_date, metavar="YYYY-MM-DD",
                   help="only rows reviewed on or after this date")
    p.add_argument("--last", type=_positive_int, metavar="N",
                   help="measures table over the newest N PRs (default with --stage: 15 for 2, "
                        "30 for 3); the gate always uses 15 or 30")
    p.add_argument("--baseline-from", type=_iso_date, metavar="YYYY-MM-DD",
                   help="first day of the Stage 0 review-time baseline window")
    p.add_argument("--baseline-to", type=_iso_date, metavar="YYYY-MM-DD",
                   help="last day of the Stage 0 review-time baseline window")
    p.add_argument("--eval-results", metavar="PATH",
                   help="eval result JSON, or a directory of them (newest run is used)")
    p.add_argument("--cost-per-review", type=_money, metavar="USD",
                   help="average cost per review, from the Claude admin page")
    p.add_argument("--spend-limit", type=_money, metavar="USD", help="monthly spend limit (org-wide)")
    p.add_argument("--monthly-spend", type=_money, metavar="USD",
                   help="Code Review spend over the last 30 days, from the Claude admin page")
    p.add_argument("--stage", type=int, choices=sorted(STAGE_WINDOWS), help="evaluate this stage's gate")
    p.add_argument("--format", choices=sorted(RENDERERS), default="text", help="output format (default text)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.baseline_from is None) != (args.baseline_to is None):
        parser.error("--baseline-from and --baseline-to go together")
    baseline = None
    if args.baseline_from is not None:
        if args.baseline_from > args.baseline_to:
            parser.error("--baseline-from is after --baseline-to")
        baseline = (args.baseline_from, args.baseline_to)

    warnings: List[str] = []
    try:
        sheet = read_sheet(args.csv)
        eval_result = None
        eval_note = "no eval results given (--eval-results)"
        if args.eval_results:
            eval_result = load_newest_eval(args.eval_results, warnings)
            if eval_result is None:
                eval_note = "no eval results in {}".format(args.eval_results)
            elif not eval_result["score"]["total"]:
                eval_note = "eval run has no cases that ran"
            else:
                eval_note = ""
    except InputError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2

    card = build_scorecard(
        sheet, repos=args.repo, since=args.since, last=args.last, baseline=baseline,
        eval_result=eval_result, eval_note=eval_note,
        cost_per_review=args.cost_per_review, spend_limit=args.spend_limit,
        monthly_spend=args.monthly_spend, stage=args.stage, warnings=warnings)
    for w in card["warnings"]:
        print("warning: {}".format(w), file=sys.stderr)
    sys.stdout.write(RENDERERS[args.format](card))
    return 0


if __name__ == "__main__":
    sys.exit(main())
