"""Tests for scorecard/scorecard.py (stdlib unittest, Python 3.9)."""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scorecard"))

import scorecard as sc  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "review-logs.csv")
D = dt.date


def run_cli(*argv):
    """Run main(); return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = sc.main(list(argv))
        except SystemExit as exc:  # argparse errors
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def pr_cell(repo, number, title="Synthetic change"):
    return "#{} - {} (https://github.com/{}/pull/{})".format(number, title, repo, number)


def log_row(number, date, repo="acme/widgets", claude="Yes", time="20",
            counts=("1", "1", "0", "0")):
    """One synthetic Review Logs row (16 columns) in sheet date format."""
    both, real, false, human = counts
    return ["Ana", date.strftime("%b %d, %Y"), "ABC-{}".format(number), pr_cell(repo, number),
            "Low", claude, "YES", "YES", time, "30", "", "Nit", both, real, false, human]


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write_csv(self, rows, name="logs.csv", header=True, bom=False):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
            writer = csv.writer(fh)
            if header:
                writer.writerow(sc.COLUMNS)
            writer.writerows(rows)
        return path

    def write_eval(self, name, found, total=10, errors=0, threshold=8,
                   started_at="2026-11-01T10:00:00Z", project="widgets", run_id=None):
        """Write a results JSON shaped per schemas/eval-result.schema.json."""
        data = {
            "run_id": run_id or "20261101T100000Z-abc1234",
            "project": project,
            "started_at": started_at,
            "standards": {"repo_path": "/tmp/widgets", "standards_ref": "abc1234",
                          "claude_md_sha": None, "review_md_sha": "abc1234"},
            "model": None,
            "threshold": threshold,
            "score": {"found": found, "total": total, "errors": errors},
            "passed": found >= threshold and errors == 0,
            "cases": [],
        }
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path


# --------------------------------------------------------------------------
# Cell parsing
# --------------------------------------------------------------------------

class ParseCellTests(unittest.TestCase):
    def test_dates_in_every_sheet_spelling(self):
        self.assertEqual(sc.parse_date("Apr 01 2026"), D(2026, 4, 1))
        self.assertEqual(sc.parse_date("May 13, 2026"), D(2026, 5, 13))
        self.assertEqual(sc.parse_date("Sept 23 2026"), D(2026, 9, 23))
        self.assertEqual(sc.parse_date("Aug 1, 2026"), D(2026, 8, 1))
        self.assertEqual(sc.parse_date("Nov 19, 2026"), D(2026, 11, 19))
        self.assertEqual(sc.parse_date("September 3, 2026"), D(2026, 9, 3))
        self.assertEqual(sc.parse_date("  Oct  02 ,  2026 "), D(2026, 10, 2))
        self.assertEqual(sc.parse_date("2026-04-01"), D(2026, 4, 1))
        self.assertEqual(sc.parse_date("4/1/2026"), D(2026, 4, 1))

    def test_bad_dates_are_none(self):
        for value in (None, "", "   ", "soon", "Feb 31 2026", "Sepx 3 2026", "13/40/2026"):
            self.assertIsNone(sc.parse_date(value), value)

    def test_integers(self):
        self.assertEqual(sc.parse_int(" 15 "), 15)
        self.assertEqual(sc.parse_int("0"), 0)
        self.assertEqual(sc.parse_int("20.0"), 20)
        for value in (None, "", "   ", "No Claude AI review", "-3", "1.5", "15 mins"):
            self.assertIsNone(sc.parse_int(value), value)

    def test_yes_no(self):
        self.assertTrue(sc.parse_yes_no(" Yes "))
        self.assertFalse(sc.parse_yes_no("NO"))
        self.assertIsNone(sc.parse_yes_no(""))
        self.assertIsNone(sc.parse_yes_no("maybe"))

    def test_pr_cell_single_line(self):
        repo, number, title = sc.parse_pr("#412 - Add a cache (https://github.com/Acme/Widgets/pull/412)")
        self.assertEqual((repo, number, title), ("acme/widgets", 412, "Add a cache"))

    def test_pr_cell_with_newline_before_url(self):
        repo, number, title = sc.parse_pr("#12 - Fix gear ratio\n(https://github.com/acme/gears/pull/12)")
        self.assertEqual((repo, number, title), ("acme/gears", 12, "Fix gear ratio"))

    def test_pr_cell_url_number_wins_and_missing_url(self):
        self.assertEqual(sc.parse_pr("#7 - typo (https://github.com/acme/gears/pull/8)")[:2], ("acme/gears", 8))
        self.assertEqual(sc.parse_pr("#9 - no link"), (None, 9, "no link"))
        self.assertEqual(sc.parse_pr(""), (None, None, ""))


# --------------------------------------------------------------------------
# Reading the CSV
# --------------------------------------------------------------------------

class ReadSheetTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.sheet = sc.read_sheet(FIXTURE)
        self.by_pr = {}
        for r in self.sheet.rows:
            self.by_pr.setdefault(r.pr_number, []).append(r)

    def test_fixture_rows_and_skips(self):
        self.assertEqual(len(self.sheet.rows), 10)  # repeated header and blank row dropped
        self.assertTrue(any("repeated header" in w for w in self.sheet.warnings))

    def test_rows_sorted_by_date_not_sheet_order(self):
        order = [r.pr_number for r in self.sheet.rows]
        self.assertEqual(order, [11, 12, 13, 15, 14, 16, 17, 18, 19, 19])

    def test_multiline_pr_cell_and_repo(self):
        row = self.by_pr[12][0]
        self.assertEqual(row.repo, "acme/gears")
        self.assertEqual(row.pr_title, "Fix gear ratio")
        self.assertEqual(row.actual_time, 45)  # " 45 " under a header with extra spaces

    def test_text_and_blank_time_are_no_data(self):
        self.assertIsNone(self.by_pr[13][0].actual_time)  # "No Claude AI review"
        self.assertIsNone(self.by_pr[15][0].actual_time)  # whitespace only

    def test_short_rows_and_empty_findings_are_none_not_zero(self):
        short = self.by_pr[11][0]
        self.assertEqual(short.severity, "")
        self.assertIsNone(short.both)
        self.assertIsNone(short.human_only)
        empty = self.by_pr[14][0]
        self.assertEqual(empty.severity, "Minor")
        self.assertEqual((empty.both, empty.ai_real, empty.ai_false, empty.human_only),
                         (None, None, None, None))
        self.assertFalse(empty.has_ai_counts)
        filled = self.by_pr[17][0]
        self.assertEqual((filled.both, filled.ai_real, filled.ai_false, filled.human_only), (0, 1, 0, 2))
        self.assertEqual(filled.ticket, "ABC-107 / ABC-108")

    def test_no_reviewer_names_kept(self):
        self.assertFalse(hasattr(self.sheet.rows[0], "reviewer"))

    def test_bom_title_row_and_header_whitespace(self):
        path = os.path.join(self.tmp, "bom.csv")
        header = ["  Reviewer", "Date  Reviewed ", "Ticket", " PR ", "Complexity", "Reviewed By Claude AI"]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Review Logs"])
            w.writerow(header)
            w.writerow(["Ben", "Aug 1, 2026", "ABC-1", pr_cell("acme/widgets", 5), "Low", "Yes"])
        sheet = sc.read_sheet(path)
        self.assertEqual(len(sheet.rows), 1)
        row = sheet.rows[0]
        self.assertEqual((row.repo, row.pr_number, row.date, row.reviewed_by_claude),
                         ("acme/widgets", 5, D(2026, 8, 1), True))
        self.assertIsNone(row.actual_time)  # column absent entirely
        self.assertTrue(any("columns missing" in w for w in sheet.warnings))

    def test_no_header_is_input_error(self):
        path = self.write_csv([["a", "b"], ["c", "d"]], header=False)
        with self.assertRaises(sc.InputError):
            sc.read_sheet(path)

    def test_cli_exit_2_when_unreadable(self):
        code, out, err = run_cli("--csv", os.path.join(self.tmp, "missing.csv"))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("error:", err)
        code, _out, _err = run_cli("--csv", self.write_csv([["x"]], header=False))
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------
# Measures
# --------------------------------------------------------------------------

class MeasureTests(unittest.TestCase):
    def setUp(self):
        self.sheet = sc.read_sheet(FIXTURE)

    def card(self, **kw):
        kw.setdefault("today", D(2026, 11, 20))
        return sc.build_scorecard(self.sheet, **kw)

    def test_false_finding_rate_uses_only_rows_with_all_three_counts(self):
        fr = self.card()["measures"]["false_finding_rate"]
        # Rows with M, N and O filled: PR16 (1,2,1), PR17 (0,1,0), PR18 (0,0,1), PR19 (2,0,0).
        self.assertEqual(fr["rows_with_data"], 4)
        self.assertEqual(fr["rows"], 10)
        self.assertEqual((fr["ai_only_false"], fr["ai_findings"]), (2, 8))
        self.assertAlmostEqual(fr["value"], 0.25)
        self.assertFalse(fr["met"])

    def test_human_only_per_pr(self):
        ho = self.card()["measures"]["human_only_per_pr"]
        self.assertEqual((ho["human_only"], ho["rows_with_data"]), (3, 3))
        self.assertAlmostEqual(ho["value"], 1.0)

    def test_coverage(self):
        cv = self.card()["measures"]["coverage"]
        self.assertEqual((cv["reviewed"], cv["rows"]), (7, 10))
        self.assertAlmostEqual(cv["value"], 0.7)

    def test_repo_and_since_filters(self):
        card = self.card(repos=["ACME/widgets"], since=D(2026, 8, 1))
        self.assertEqual(card["selection"]["prs"], 5)
        self.assertEqual(card["selection"]["repos"], ["acme/widgets"])
        self.assertAlmostEqual(card["measures"]["false_finding_rate"]["value"], 1 / 7)

    def test_review_time_per_pr_sums_rows_and_skips_text(self):
        rt = self.card(repos=["acme/widgets"], since=D(2026, 8, 1))["measures"]["review_time"]
        # PR14 20, PR15 none, PR16 15, PR17 25, PR19 blank + 12 -> [20, 15, 25, 12]
        self.assertEqual(rt["prs_with_time"], 4)
        self.assertEqual(rt["median"], 17.5)
        self.assertEqual(rt["mean"], 18.0)
        self.assertIsNone(rt["baseline"])
        self.assertIsNone(rt["met"])

    def test_baseline_comparison(self):
        rt = self.card(repos=["acme/widgets"], since=D(2026, 8, 1),
                       baseline=(D(2026, 4, 1), D(2026, 5, 31)))["measures"]["review_time"]
        self.assertEqual(rt["baseline"]["median"], 30.0)  # PR11 30, PR13 text -> no time
        self.assertEqual(rt["baseline"]["prs_with_time"], 1)
        self.assertTrue(rt["met"])
        rt = self.card(since=D(2026, 8, 1), baseline=(D(2026, 10, 1), D(2026, 10, 31)))["measures"]["review_time"]
        # current (all repos, since Aug): [20, 15, 25, 10, 12] median 15; baseline Oct: [15, 25] median 20
        self.assertEqual((rt["median"], rt["baseline"]["median"]), (15.0, 20.0))
        self.assertTrue(rt["met"])
        rt = self.card(since=D(2026, 8, 1), baseline=(D(2026, 11, 19), D(2026, 11, 19)))["measures"]["review_time"]
        self.assertEqual(rt["baseline"]["median"], 11.0)
        self.assertFalse(rt["met"])

    def test_cost_projection_is_a_lower_bound(self):
        cost = self.card(cost_per_review=1.5, spend_limit=500)["measures"]["cost"]
        # Claude-reviewed PRs from Oct 21 to Nov 19: PR18, PR19.
        self.assertEqual(cost["reviews_in_window"], 2)
        self.assertEqual(cost["projected_spend"], 3.0)
        self.assertTrue(cost["projection_is_lower_bound"])
        # A lower bound under the limit proves nothing; over the limit it proves a breach.
        self.assertIsNone(cost["within_limit"])
        over = self.card(cost_per_review=300, spend_limit=500)["measures"]["cost"]
        self.assertEqual((over["within_limit"], over["within_limit_basis"]), (False, "lower_bound"))
        self.assertIsNone(self.card(cost_per_review=1.5)["measures"]["cost"]["within_limit"])

    def test_cost_uses_monthly_spend_when_given(self):
        cost = self.card(cost_per_review=1.5, spend_limit=500, monthly_spend=420)["measures"]["cost"]
        self.assertEqual((cost["within_limit"], cost["within_limit_basis"]), (True, "monthly_spend"))
        cost = self.card(cost_per_review=1.5, spend_limit=500, monthly_spend=501)["measures"]["cost"]
        self.assertFalse(cost["within_limit"])

    def test_cost_projection_ignores_repo_filter(self):
        # The spend limit is org-wide, so --repo must not shrink the projection.
        cost = self.card(repos=["acme/gears"], cost_per_review=1.5)["measures"]["cost"]
        self.assertEqual(cost["reviews_in_window"], 2)  # PR18 (gears) and PR19 (widgets)

    def test_human_only_sums_rows_of_one_pr(self):
        sheet = sc.Sheet(rows=[], warnings=[])
        for record, (number, human) in enumerate([(1, 2), (1, 1), (2, 0), (3, None)], start=2):
            sheet.rows.append(sc.Row(
                record=record, date=D(2026, 10, record), date_raw="", ticket="", repo="acme/widgets",
                pr_number=number, pr_title="", reviewed_by_claude=True, actual_time=None,
                severity="", both=None, ai_real=None, ai_false=None, human_only=human))
        ho = sc.build_scorecard(sheet)["measures"]["human_only_per_pr"]
        # PR1 = 2 + 1 = 3, PR2 = 0, PR3 not filled -> 3 misses over 2 PRs.
        self.assertEqual((ho["human_only"], ho["prs_with_data"], ho["prs"]), (3, 2, 3))
        self.assertAlmostEqual(ho["value"], 1.5)


class NoDataTests(TempDirCase):
    def test_empty_findings_columns_give_no_data_not_zero(self):
        rows = [log_row(n, D(2026, 10, n), counts=("", "", "", "")) for n in range(1, 16)]
        card = sc.build_scorecard(sc.read_sheet(self.write_csv(rows)), stage=2)
        fr = card["measures"]["false_finding_rate"]
        self.assertIsNone(fr["value"])
        self.assertEqual(fr["rows_with_data"], 0)
        self.assertIsNone(card["measures"]["human_only_per_pr"]["value"])
        items = {i["id"]: i["status"] for i in card["gate"]["items"]}
        self.assertEqual(items["false_finding_rate"], sc.NO_DATA)
        self.assertEqual(items["reviewed_and_logged"], sc.FAIL)
        self.assertEqual(items["eval"], sc.NO_DATA)
        self.assertNotEqual(card["gate"]["verdict"], sc.PASS)

    def test_zero_ai_findings_is_no_data(self):
        rows = [log_row(n, D(2026, 10, n), counts=("0", "0", "0", "0")) for n in range(1, 16)]
        card = sc.build_scorecard(sc.read_sheet(self.write_csv(rows)), stage=2)
        items = {i["id"]: i for i in card["gate"]["items"]}
        self.assertEqual(items["false_finding_rate"]["status"], sc.NO_DATA)
        self.assertIn("no AI findings", items["false_finding_rate"]["detail"])

    def test_stage3_missing_baseline_and_cost_are_no_data(self):
        rows = [log_row(n, D(2026, 10, 1) + dt.timedelta(days=n)) for n in range(1, 31)]
        card = sc.build_scorecard(sc.read_sheet(self.write_csv(rows)), stage=3)
        items = {i["id"]: i["status"] for i in card["gate"]["items"]}
        self.assertEqual(items["review_time"], sc.NO_DATA)
        self.assertEqual(items["cost"], sc.NO_DATA)
        self.assertEqual(card["gate"]["verdict"], sc.NO_DATA)

    def test_empty_selection(self):
        card = sc.build_scorecard(sc.read_sheet(FIXTURE), repos=["acme/nothing"], stage=2)
        self.assertEqual(card["selection"]["rows"], 0)
        statuses = {i["status"] for i in card["gate"]["items"]}
        self.assertNotIn(sc.PASS, statuses)
        self.assertEqual(card["gate"]["verdict"], sc.NO_DATA)


# --------------------------------------------------------------------------
# Gate windows
# --------------------------------------------------------------------------

class GateWindowTests(TempDirCase):
    def good_eval(self):
        return {"score": {"found": 9, "total": 10, "errors": 0}, "threshold": 8, "run_id": "r1"}

    def rows(self, count, start=D(2026, 9, 1), bad=()):
        out = []
        for n in range(1, count + 1):
            counts = ("", "", "", "") if n in bad else ("2", "2", "0", "0")
            out.append(log_row(n, start + dt.timedelta(days=n), counts=counts))
        return out

    def test_stage2_uses_last_15_prs(self):
        # PR 1 is unlogged but falls outside the window of the newest 15.
        sheet = sc.read_sheet(self.write_csv(self.rows(20, bad={1})))
        card = sc.build_scorecard(sheet, stage=2, eval_result=self.good_eval())
        self.assertEqual(card["selection"]["prs"], 15)
        self.assertEqual(card["selection"]["first_date"], "2026-09-07")  # PRs 6..20
        items = {i["id"]: i["status"] for i in card["gate"]["items"]}
        self.assertEqual(items["window"], sc.PASS)
        self.assertEqual(items["reviewed_and_logged"], sc.PASS)
        self.assertEqual(items["false_finding_rate"], sc.PASS)
        self.assertEqual(items["eval"], sc.PASS)
        self.assertEqual(items["unanswered_findings"], sc.MANUAL)
        self.assertEqual(items["team_says_helps"], sc.MANUAL)
        self.assertEqual(card["gate"]["verdict"], sc.PASS)

    def test_stage2_fails_when_a_window_pr_is_unlogged(self):
        sheet = sc.read_sheet(self.write_csv(self.rows(20, bad={18})))
        card = sc.build_scorecard(sheet, stage=2, eval_result=self.good_eval())
        item = [i for i in card["gate"]["items"] if i["id"] == "reviewed_and_logged"][0]
        self.assertEqual(item["status"], sc.FAIL)
        self.assertIn("acme/widgets#18", item["detail"])
        self.assertEqual(card["gate"]["verdict"], sc.FAIL)

    def test_stage2_not_claude_reviewed_fails(self):
        rows = self.rows(15)
        rows[-1][5] = "No"
        card = sc.build_scorecard(sc.read_sheet(self.write_csv(rows)), stage=2, eval_result=self.good_eval())
        item = [i for i in card["gate"]["items"] if i["id"] == "reviewed_and_logged"][0]
        self.assertEqual(item["status"], sc.FAIL)
        self.assertIn("not reviewed by Claude", item["detail"])

    def test_short_window_is_no_data(self):
        sheet = sc.read_sheet(self.write_csv(self.rows(20)))
        card = sc.build_scorecard(sheet, stage=3, eval_result=self.good_eval(),
                                  baseline=(D(2026, 1, 1), D(2026, 3, 31)))
        window = card["gate"]["items"][0]
        self.assertEqual((window["id"], window["status"]), ("window", sc.NO_DATA))
        self.assertIn("20 of 30", window["detail"])
        self.assertNotEqual(card["gate"]["verdict"], sc.PASS)

    def test_stage3_uses_last_30_prs_and_passes(self):
        baseline = [log_row(100 + n, D(2026, 3, n), claude="No", time="40", counts=("", "", "", ""))
                    for n in range(1, 11)]
        sheet = sc.read_sheet(self.write_csv(baseline + self.rows(35, bad={2})))
        card = sc.build_scorecard(sheet, stage=3, eval_result=self.good_eval(),
                                  baseline=(D(2026, 3, 1), D(2026, 3, 31)),
                                  cost_per_review=2.0, spend_limit=500, monthly_spend=300)
        self.assertEqual(card["selection"]["prs"], 30)
        items = {i["id"]: i["status"] for i in card["gate"]["items"]}
        self.assertEqual(items["window"], sc.PASS)
        self.assertEqual(items["review_time"], sc.PASS)  # 20 <= 40
        self.assertEqual(items["cost"], sc.PASS)
        self.assertEqual(card["gate"]["verdict"], sc.PASS)
        self.assertNotIn("team_says_helps", items)

    def test_stage3_review_time_above_baseline_fails(self):
        baseline = [log_row(100 + n, D(2026, 3, n), claude="No", time="10", counts=("", "", "", ""))
                    for n in range(1, 11)]
        sheet = sc.read_sheet(self.write_csv(baseline + self.rows(30)))
        card = sc.build_scorecard(sheet, stage=3, eval_result=self.good_eval(),
                                  baseline=(D(2026, 3, 1), D(2026, 3, 31)),
                                  cost_per_review=2.0, spend_limit=500)
        items = {i["id"]: i["status"] for i in card["gate"]["items"]}
        self.assertEqual(items["review_time"], sc.FAIL)
        self.assertEqual(card["gate"]["verdict"], sc.FAIL)

    def test_last_shapes_measures_but_not_the_gate_window(self):
        sheet = sc.read_sheet(self.write_csv(self.rows(20)))
        card = sc.build_scorecard(sheet, stage=2, last=10, eval_result=self.good_eval())
        self.assertEqual(card["selection"]["prs"], 10)
        self.assertEqual(card["gate"]["prs_in_window"], 15)
        self.assertEqual(card["gate"]["items"][0]["status"], sc.PASS)
        self.assertTrue(any("--last 10" in w for w in card["warnings"]))

    def test_larger_last_does_not_pull_old_prs_into_the_gate(self):
        # PR 1 is unlogged and older than the newest 15; --last 20 must not fail the gate.
        sheet = sc.read_sheet(self.write_csv(self.rows(20, bad={1})))
        card = sc.build_scorecard(sheet, stage=2, last=20, eval_result=self.good_eval())
        self.assertEqual(card["selection"]["prs"], 20)
        self.assertEqual(card["gate"]["prs_in_window"], 15)
        self.assertEqual(card["gate"]["verdict"], sc.PASS)

    def test_rows_without_pr_number_do_not_fill_the_window(self):
        rows = self.rows(14)
        na = log_row(99, D(2026, 9, 30))
        na[3] = "N/A"
        sheet = sc.read_sheet(self.write_csv(rows + [na]))
        card = sc.build_scorecard(sheet, stage=2, eval_result=self.good_eval())
        self.assertEqual(card["selection"]["prs"], 14)
        self.assertEqual(card["gate"]["items"][0]["status"], sc.NO_DATA)
        self.assertNotEqual(card["gate"]["verdict"], sc.PASS)
        self.assertTrue(any("no PR number" in w for w in card["warnings"]))

    def test_pr_without_url_is_not_counted_twice(self):
        rows = self.rows(5)
        bare = log_row(5, D(2026, 9, 7))
        bare[3] = "#5 - Synthetic change"  # same PR, no URL
        card = sc.build_scorecard(sc.read_sheet(self.write_csv(rows + [bare])))
        self.assertEqual(card["selection"]["prs"], 5)
        self.assertTrue(any("no PR URL" in w for w in card["warnings"]))
        # A sheet where no row has a URL still works (single repo, "#n" only).
        only_bare = []
        for n in range(1, 4):
            row = log_row(n, D(2026, 9, n))
            row[3] = "#{} - Synthetic change".format(n)
            only_bare.append(row)
        card = sc.build_scorecard(sc.read_sheet(self.write_csv(only_bare, name="bare.csv")))
        self.assertEqual(card["selection"]["prs"], 3)

    def stage3_card(self, current_times, baseline_times, **kw):
        baseline = [log_row(100 + n, D(2026, 3, n), claude="No", time=t, counts=("", "", "", ""))
                    for n, t in enumerate(baseline_times, start=1)]
        current = [log_row(n, D(2026, 9, 1) + dt.timedelta(days=n), counts=("2", "2", "0", "0"), time=t)
                   for n, t in enumerate(current_times, start=1)]
        sheet = sc.read_sheet(self.write_csv(baseline + current))
        kw.setdefault("monthly_spend", 300)
        card = sc.build_scorecard(sheet, stage=3, eval_result=self.good_eval(),
                                  baseline=(D(2026, 3, 1), D(2026, 3, 31)),
                                  cost_per_review=2.0, spend_limit=500, **kw)
        return {i["id"]: i for i in card["gate"]["items"]}, card

    def test_review_time_mostly_text_is_no_data(self):
        items, card = self.stage3_card(["10"] + ["No Claude AI review"] * 29, ["40"] * 5)
        rt = items["review_time"]
        self.assertEqual(rt["status"], sc.NO_DATA)
        self.assertIn("1 of 30 PRs have a time", rt["detail"])
        self.assertEqual(card["gate"]["verdict"], sc.NO_DATA)

    def test_review_time_tolerates_a_few_missing_times(self):
        items, _ = self.stage3_card(["10"] * 27 + ["  "] * 3, ["40"] * 5)
        self.assertEqual(items["review_time"]["status"], sc.PASS)
        items, _ = self.stage3_card(["10"] * 26 + ["  "] * 4, ["40"] * 5)
        self.assertEqual(items["review_time"]["status"], sc.NO_DATA)

    def test_review_time_needs_enough_baseline_prs(self):
        items, _ = self.stage3_card(["10"] * 30, ["40"])
        self.assertEqual(items["review_time"]["status"], sc.NO_DATA)
        self.assertIn("baseline PRs", items["review_time"]["detail"])

    def test_stage3_cost_without_monthly_spend_is_no_data(self):
        items, _ = self.stage3_card(["10"] * 30, ["40"] * 5, monthly_spend=None)
        self.assertEqual(items["cost"]["status"], sc.NO_DATA)
        self.assertIn("lower bound", items["cost"]["detail"])
        items, _ = self.stage3_card(["10"] * 30, ["40"] * 5, monthly_spend=600)
        self.assertEqual(items["cost"]["status"], sc.FAIL)

    def test_last_counts_prs_not_rows(self):
        rows = self.rows(5)
        rows.append(log_row(5, D(2026, 9, 6)))  # second reviewer row for PR 5
        card = sc.build_scorecard(sc.read_sheet(self.write_csv(rows)), last=2)
        self.assertEqual((card["selection"]["prs"], card["selection"]["rows"]), (2, 3))


# --------------------------------------------------------------------------
# Eval results pickup
# --------------------------------------------------------------------------

class EvalResultsTests(TempDirCase):
    def test_newest_run_in_directory_is_used(self):
        self.write_eval("results/widgets/old.json", found=6, started_at="2026-10-01T09:00:00Z", run_id="old")
        self.write_eval("results/widgets/new.json", found=9, started_at="2026-11-01T09:00:00Z", run_id="new")
        with open(os.path.join(self.tmp, "results", "widgets", "notes.json"), "w") as fh:
            json.dump({"hello": "world"}, fh)
        warnings = []
        result = sc.load_newest_eval(os.path.join(self.tmp, "results"), warnings)
        self.assertEqual(result["run_id"], "new")
        self.assertTrue(any("notes.json" in w for w in warnings))

    def test_cli_reports_catch_rate_and_gate(self):
        self.write_eval("results/widgets/a.json", found=9, started_at="2026-11-01T09:00:00Z", run_id="good")
        code, out, _err = run_cli("--csv", FIXTURE, "--eval-results", os.path.join(self.tmp, "results"),
                                  "--stage", "2", "--format", "json")
        self.assertEqual(code, 0)
        card = json.loads(out)
        cr = card["measures"]["catch_rate"]
        self.assertEqual((cr["found"], cr["total"], cr["threshold"], cr["passed"]), (9, 10, 8, True))
        self.assertAlmostEqual(cr["value"], 0.9)
        self.assertEqual(cr["run_id"], "good")
        item = [i for i in card["gate"]["items"] if i["id"] == "eval"][0]
        self.assertEqual(item["status"], sc.PASS)

    def test_failed_and_errored_runs_fail(self):
        low = self.write_eval("low.json", found=7)
        errored = self.write_eval("err.json", found=9, errors=1)
        for path in (low, errored):
            code, out, _ = run_cli("--csv", FIXTURE, "--eval-results", path, "--stage", "2", "--format", "json")
            self.assertEqual(code, 0)
            item = [i for i in json.loads(out)["gate"]["items"] if i["id"] == "eval"][0]
            self.assertEqual(item["status"], sc.FAIL, path)

    def test_run_with_no_cases_is_no_data(self):
        path = self.write_eval("none.json", found=0, total=0, errors=10)
        _code, out, _ = run_cli("--csv", FIXTURE, "--eval-results", path, "--stage", "2", "--format", "json")
        item = [i for i in json.loads(out)["gate"]["items"] if i["id"] == "eval"][0]
        self.assertEqual(item["status"], sc.NO_DATA)

    def test_empty_directory_is_no_data_and_bad_paths_exit_2(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        code, out, _ = run_cli("--csv", FIXTURE, "--eval-results", empty, "--format", "json")
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["measures"]["catch_rate"]["value"])
        code, _, _ = run_cli("--csv", FIXTURE, "--eval-results", os.path.join(self.tmp, "nope"))
        self.assertEqual(code, 2)
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        code, _, _ = run_cli("--csv", FIXTURE, "--eval-results", bad)
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

class OutputTests(unittest.TestCase):
    def test_json_shape(self):
        code, out, err = run_cli("--csv", FIXTURE, "--repo", "acme/widgets", "acme/gears",
                                 "--baseline-from", "2026-04-01", "--baseline-to", "2026-05-31",
                                 "--cost-per-review", "1.23", "--spend-limit", "500",
                                 "--monthly-spend", "250", "--stage", "3", "--format", "json")
        self.assertEqual(code, 0)
        self.assertIn("repeated header", err)
        card = json.loads(out)
        self.assertEqual(set(card), {"generated_on", "filters", "selection", "measures", "gate",
                                     "warnings", "footer"})
        self.assertEqual(set(card["measures"]), {
            "false_finding_rate", "catch_rate", "human_only_per_pr", "coverage",
            "review_time", "cost", "unanswered_findings_at_merge"})
        self.assertEqual(card["filters"]["repos"], ["acme/gears", "acme/widgets"])
        self.assertEqual(card["filters"]["last_prs"], 30)
        self.assertEqual(card["gate"]["stage"], 3)
        self.assertEqual(card["gate"]["window_prs"], 30)
        self.assertEqual(card["measures"]["cost"]["monthly_spend"], 250)
        cost_item = [i for i in card["gate"]["items"] if i["id"] == "cost"][0]
        self.assertEqual(cost_item["status"], sc.PASS)
        for item in card["gate"]["items"]:
            self.assertEqual(set(item), {"id", "name", "status", "detail"})
            self.assertIn(item["status"], (sc.PASS, sc.FAIL, sc.NO_DATA, sc.MANUAL))
        self.assertEqual(card["footer"], sc.FOOTER)
        self.assertNotIn("Ana", out)  # no reviewer names in the output
        self.assertNotIn("Cara", out)

    def test_text_output(self):
        code, out, _ = run_cli("--csv", FIXTURE, "--stage", "2")
        self.assertEqual(code, 0)
        self.assertIn("False finding rate", out)
        self.assertIn("Gate: Stage 2 (last 15 PRs):", out)
        self.assertIn("[MANUAL CHECK]", out)
        self.assertTrue(out.rstrip().endswith(sc.FOOTER))
        self.assertNotIn("Ben", out)

    def test_markdown_output(self):
        code, out, _ = run_cli("--csv", FIXTURE, "--format", "markdown")
        self.assertEqual(code, 0)
        self.assertIn("| Measure | Value | Target | Data |", out)
        self.assertIn("no baseline", out)
        self.assertIn(sc.FOOTER, out)
        self.assertNotIn("Gate:", out)

    def test_argument_errors_exit_2(self):
        self.assertEqual(run_cli("--csv", FIXTURE, "--baseline-from", "2026-01-01")[0], 2)
        self.assertEqual(run_cli("--csv", FIXTURE, "--since", "Apr 1")[0], 2)
        self.assertEqual(run_cli("--csv", FIXTURE, "--stage", "4")[0], 2)


if __name__ == "__main__":
    unittest.main()
