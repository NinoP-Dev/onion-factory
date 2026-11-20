"""Tests for logrow.py. All fixtures are synthetic (acme/widgets, fake people).

Run: python3 -m unittest discover -s plugins/factory/skills/log-review/references -p 'test_*.py' -v
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import logrow  # noqa: E402

REPO_SCHEMA = HERE.parents[4] / "schemas" / "review-log-row.schema.json"
HEAD = "c" * 40
FIRST = "a" * 40


def comment(cid, body, review_id=10, path="src/app.py", line=12, start=None, commit=FIRST,
            reply_to=None, login="pat-dev", user_type="User", created="2026-11-18T10:00:00Z"):
    return {
        "id": cid, "body": body, "pull_request_review_id": review_id, "path": path,
        "original_line": line, "line": line, "original_start_line": start, "start_line": start,
        "side": "RIGHT", "original_commit_id": commit, "commit_id": commit,
        "in_reply_to_id": reply_to, "user": {"login": login, "type": user_type},
        "html_url": "https://github.com/acme/widgets/pull/7#discussion_r%d" % cid, "created_at": created,
    }


def review(rid, body, submitted="2026-11-18T10:00:00Z", commit=FIRST):
    return {"id": rid, "body": body, "submitted_at": submitted, "commit_id": commit,
            "html_url": "https://github.com/acme/widgets/pull/7#pullrequestreview-%d" % rid}


F2 = "<!-- factory:finding id=F2 severity=Nit -->\nRename `tmp`"
F1 = "<!-- factory:finding id=F1 severity=Important -->\n**Important: Null user crashes checkout**\n\nbody"
SUMMARY = ("<!-- factory:review head=%s findings=3 -->\n**factory:review-pr**: 3 findings\n\nSummary.\n\n"
           "### Findings outside the diff\n<!-- factory:finding id=F3 severity=Pre-existing -->\n"
           "**F3 Pre-existing** `src/db.py:88` Retry loop never backs off\nbody" % FIRST)


def base_facts(**over):
    facts = {
        "repo": "acme/widgets", "number": 7, "title": "WID-12 Fix checkout\nfor guests",
        "url": "https://github.com/acme/widgets/pull/7", "head_ref": "wid-12-checkout",
        "head_sha": HEAD, "merged_at": "2026-11-19T15:00:00Z", "date_reviewed": "Nov 19, 2026",
        "date_source": "merged_at", "tickets": ["WID-12"], "ai_reviewed": True, "warnings": [],
        "findings": [
            {"key": "F1", "severity": "Important", "fixed": True, "thread": {"resolved": True}},
            {"key": "F2", "severity": "Nit", "fixed": False, "thread": {"resolved": True}},
            {"key": "F3", "severity": "Pre-existing", "fixed": None, "thread": {"resolved": None}},
        ],
    }
    facts.update(over)
    return facts


ANSWERS = {"reviewer": "Pat", "minutes": 25, "complexity": "medium",
           "false_ids": ["F2"], "both_found": 1, "human_only": 2}


class HelpersTest(unittest.TestCase):
    def test_sheet_date_is_zero_padded(self):
        self.assertEqual(logrow.sheet_date(dt.date(2026, 11, 5)), "Nov 05, 2026")
        self.assertEqual(logrow.sheet_date(dt.date(2026, 9, 23)), "Sep 23, 2026")

    def test_jira_keys_from_title_and_branch(self):
        self.assertEqual(logrow.jira_keys("WID-12 / OPS-3: fix UTF-8 bug", "feature/wid-12-x"),
                         ["WID-12", "OPS-3"])
        self.assertEqual(logrow.jira_keys("No key here", "main"), [])
        self.assertEqual(logrow.jira_keys("Checkout fix", "feature/wid-12-checkout"), ["WID-12"])
        self.assertEqual(logrow.jira_keys("Fix WID-12.", None), ["WID-12"])

    def test_jira_keys_ignore_versions_and_package_names(self):
        self.assertEqual(logrow.jira_keys("Bump lodash-4 for node-18", "dependabot/npm_and_yarn/axios-1.6.0"), [])
        self.assertEqual(logrow.jira_keys("Upgrade to python-3.12", "deps/python-3.12"), [])
        self.assertEqual(logrow.jira_keys("Bump NODE-18.2 image", None), [])

    def test_parse_pr_ref(self):
        self.assertEqual(logrow.parse_pr_ref("https://github.com/acme/widgets/pull/7/files", None),
                         ("acme/widgets", 7))
        self.assertEqual(logrow.parse_pr_ref("#7", "acme/widgets"), ("acme/widgets", 7))
        with self.assertRaises(logrow.UsageError):
            logrow.parse_pr_ref("seven", None)

    def test_patch_touches(self):
        patch = "@@ -10,5 +10,5 @@ def f():\n a\n b\n-c\n+C\n d\n e"
        self.assertTrue(logrow.patch_touches(patch, 12, 12))
        self.assertFalse(logrow.patch_touches(patch, 30, 31))
        insert = "@@ -20,2 +20,3 @@\n x\n+new\n y"
        self.assertTrue(logrow.patch_touches(insert, 21, 21))
        self.assertFalse(logrow.patch_touches(None, 1, 1))

    def test_load_answers_inline_json(self):
        self.assertEqual(logrow.load_answers('{"reviewer": "Pat"}'), {"reviewer": "Pat"})
        with self.assertRaises(logrow.AnswerError):
            logrow.load_answers("[1]")

    def test_json_stream_concatenated_pages(self):
        self.assertEqual(logrow.json_stream('[1,2]\n[3]\n'), [1, 2, 3])
        self.assertEqual(logrow.json_stream(''), [])


class FindingExtractionTest(unittest.TestCase):
    def test_single_run_inline_and_body_findings(self):
        comments = [comment(1, F1), comment(2, F2, line=40, created="2026-11-18T10:00:01Z"),
                    comment(3, "> " + F1 + "\nFixed", reply_to=1),
                    comment(4, "plain human comment")]
        runs, findings = logrow.factory_findings(comments, [review(10, SUMMARY)])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["declared_findings"], 3)
        self.assertEqual([f["key"] for f in findings], ["F1", "F2", "F3"])
        self.assertEqual(findings[0]["title"], "Null user crashes checkout")
        self.assertEqual(findings[2]["source"], "factory-review-body")
        self.assertEqual(findings[2]["path"], "src/db.py")
        self.assertEqual(findings[2]["line"], 88)
        self.assertEqual(findings[2]["title"], "Retry loop never backs off")

    def test_multiple_runs_get_prefixed_keys(self):
        comments = [comment(1, F1, review_id=10), comment(2, F1, review_id=11, created="2026-11-19T10:00:00Z")]
        reviews = [review(10, "<!-- factory:review head=%s findings=1 -->" % FIRST),
                   review(11, "<!-- factory:review head=%s findings=1 -->" % HEAD, submitted="2026-11-19T10:00:00Z")]
        runs, findings = logrow.factory_findings(comments, reviews)
        self.assertEqual([f["key"] for f in findings], ["R1.F1", "R2.F1"])

    def test_repeat_candidates_across_runs(self):
        comments = [comment(1, F1, review_id=10), comment(2, F1, review_id=11, line=13, commit=HEAD,
                                                          created="2026-11-19T10:00:00Z"),
                    comment(3, F2, review_id=11, line=40, commit=HEAD, created="2026-11-19T10:00:01Z")]
        reviews = [review(10, "<!-- factory:review head=%s findings=1 -->" % FIRST),
                   review(11, "<!-- factory:review head=%s findings=2 -->" % HEAD, submitted="2026-11-19T10:00:00Z")]
        runs, findings = logrow.factory_findings(comments, reviews)
        self.assertTrue(logrow.needs_repeat_check(runs, findings))
        self.assertEqual(logrow.repeat_candidates(findings), [{"key": "R2.F1", "repeats": "R1.F1"}])
        one_run, single = logrow.factory_findings([comment(1, F1)], [review(10, SUMMARY)])
        self.assertFalse(logrow.needs_repeat_check(one_run, single))

    def test_managed_comments_on_several_pushes_need_repeat_check(self):
        a = comment(5, "\U0001F534 **Important** Off-by-one in pager", login="claude[bot]", user_type="Bot")
        b = comment(6, "\U0001F534 **Important** Off-by-one in pager", login="claude[bot]", user_type="Bot",
                    commit=HEAD, created="2026-11-19T10:00:00Z")
        found = logrow.managed_findings([a, b])
        self.assertTrue(logrow.needs_repeat_check([], found))
        self.assertEqual(logrow.repeat_candidates(found), [{"key": "M2", "repeats": "M1"}])

    def test_managed_comments_and_check_run_counts(self):
        bot = comment(5, "\U0001F534 **Important** Off-by-one in pager", login="claude[bot]", user_type="Bot")
        human = comment(6, "\U0001F534 looks wrong", login="pat-dev")
        found = logrow.managed_findings([bot, human])
        self.assertEqual([(f["key"], f["severity"]) for f in found], [("M1", "Important")])
        self.assertEqual(found[0]["title"], "Off-by-one in pager")
        counts = logrow.parse_bughunter('text\n<!-- bughunter-severity: {"nit":1,"normal":2,"pre_existing":0} -->')
        self.assertEqual(counts, {"Important": 2, "Nit": 1, "Pre-existing": 0})
        self.assertEqual(len(logrow.findings_from_counts(counts)), 3)

    def test_threads_and_fixed_detection(self):
        findings = [{"key": "F1", "comment_id": 1, "path": "src/app.py", "line": 12, "start_line": 12,
                     "side": "RIGHT", "commit": FIRST},
                    {"key": "F2", "comment_id": 2, "path": "src/app.py", "line": 80, "start_line": 80,
                     "side": "RIGHT", "commit": FIRST},
                    {"key": "F3", "comment_id": None, "path": None, "line": None, "commit": FIRST}]
        threads = [{"isResolved": True, "isOutdated": True, "comments": {"totalCount": 2, "nodes": [{"databaseId": 1}]}},
                   {"isResolved": False, "isOutdated": False, "comments": {"totalCount": 1, "nodes": [{"databaseId": 2}]}}]
        logrow.attach_threads(findings, threads)
        self.assertEqual([f["thread"]["resolved"] for f in findings], [True, False, None])
        compare = {"files": [{"filename": "src/app.py", "status": "modified",
                              "patch": "@@ -10,5 +10,5 @@\n a\n b\n-c\n+C\n d\n e"}]}
        logrow.attach_fixed(findings, HEAD, {FIRST: compare})
        self.assertEqual([f["fixed"] for f in findings], [True, False, None])


class BuildTest(unittest.TestCase):
    def build(self, facts=None, answers=None):
        return logrow.build(facts or base_facts(), dict(ANSWERS, **(answers or {})), today=dt.date(2026, 11, 20))

    def test_arithmetic_and_row(self):
        res = self.build()
        row = res["row"]
        self.assertEqual(row["Findings: Both Found"], 1)
        self.assertEqual(row["Findings: AI Only, Real"], 1)   # 3 total - 1 false - 1 both
        self.assertEqual(row["Findings: AI Only, False"], 1)
        self.assertEqual(row["Findings: Human Only"], 2)
        self.assertEqual(row["Recommendation"], "YES")
        self.assertEqual(row["Accepted Recommendation"], "YES")
        self.assertEqual(row["Recommendation Severity"], "Important")
        self.assertEqual(row["Complexity"], "Medium")
        self.assertEqual(row["PR"], "#7 - WID-12 Fix checkout for guests (https://github.com/acme/widgets/pull/7)")
        self.assertIn("1 + 1 + 1 = 3 = AI total 3", "\n".join(res["arithmetic"]))
        self.assertEqual(logrow.validate_row(row, logrow.EMBEDDED_SCHEMA), [])

    def test_tsv_has_sixteen_cells_and_no_newlines(self):
        res = self.build(answers={"notes": "line one\nline\ttwo"})
        tsv = logrow.to_tsv(res["row"], logrow.COLUMNS)
        self.assertEqual(len(tsv.split("\t")), 16)
        self.assertNotIn("\n", tsv)
        self.assertEqual(tsv.split("\t")[9], "")  # Est. Time left empty

    def test_both_found_cannot_exceed_real_findings(self):
        with self.assertRaises(logrow.AnswerError):
            self.build(answers={"both_found": 3})

    def test_unknown_false_id_rejected(self):
        with self.assertRaises(logrow.AnswerError):
            self.build(answers={"false_ids": ["F9"]})

    def test_false_ids_accept_string_and_none(self):
        res = self.build(answers={"false_ids": "none", "both_found": 0})
        self.assertEqual(res["row"]["Findings: AI Only, False"], 0)
        self.assertEqual(res["row"]["Findings: AI Only, Real"], 3)

    def test_duplicates_counted_once(self):
        res = self.build(answers={"duplicate_ids": ["F3"], "false_ids": [], "both_found": 0})
        self.assertEqual(res["row"]["Findings: AI Only, Real"], 2)

    def test_accepted_ignores_false_findings_and_honours_override(self):
        facts = base_facts()
        facts["findings"][0]["fixed"] = False
        facts["findings"][1]["fixed"] = True  # F2 is judged false
        self.assertEqual(self.build(facts)["row"]["Accepted Recommendation"], "NO")
        self.assertEqual(self.build(facts, {"fixed_ids": ["F1"]})["row"]["Accepted Recommendation"], "YES")

    def test_no_ai_review_leaves_findings_empty(self):
        facts = base_facts(ai_reviewed=False, findings=[])
        res = self.build(facts, {"false_ids": [], "both_found": 0, "human_only": 0})
        row = res["row"]
        self.assertEqual(row["Reviewed By Claude AI"], "No")
        self.assertEqual(row["Recommendation"], "NO")
        self.assertIsNone(row["Findings: Both Found"])
        self.assertIsNone(row["Findings: Human Only"])
        self.assertEqual(res["eval_cases"], [])

    def test_reviewed_requires_human_only_and_both_found(self):
        with self.assertRaises(logrow.AnswerError):
            logrow.build(base_facts(), {k: v for k, v in ANSWERS.items() if k != "human_only"})
        with self.assertRaises(logrow.AnswerError):
            logrow.build(base_facts(), {k: v for k, v in ANSWERS.items() if k != "both_found"})
        # No AI finding judged real: both_found can only be 0, so it may be left out.
        res = logrow.build(base_facts(findings=[]), {"reviewer": "Pat", "minutes": 5, "complexity": "low",
                                                     "human_only": 1})
        self.assertEqual(res["row"]["Findings: Both Found"], 0)
        self.assertEqual(res["row"]["Findings: Human Only"], 1)

    def test_claude_review_elsewhere_without_counts_leaves_findings_empty(self):
        facts = base_facts(ai_reviewed=False, findings=[])
        res = logrow.build(facts, {"reviewer": "Sam", "minutes": 20, "complexity": "low",
                                   "reviewed_by_claude": "yes"})
        row = res["row"]
        self.assertEqual(row["Reviewed By Claude AI"], "Yes")
        for col in ("Findings: Both Found", "Findings: AI Only, Real", "Findings: AI Only, False",
                    "Findings: Human Only"):
            self.assertIsNone(row[col])
        self.assertNotIn("found no issues", row["Notes"])
        self.assertIn("findings not counted", row["Notes"])
        self.assertEqual(logrow.validate_row(row, logrow.EMBEDDED_SCHEMA), [])

    def test_claude_review_elsewhere_counts_all_or_none(self):
        facts = base_facts(ai_reviewed=False, findings=[])
        base = {"reviewer": "Sam", "minutes": 20, "complexity": "low", "reviewed_by_claude": "yes"}
        with self.assertRaises(logrow.AnswerError):
            logrow.build(facts, dict(base, human_only=1))
        res = logrow.build(facts, dict(base, ai_findings=3, false_count=1, both_found=1, human_only=0,
                                       severity="nit", accepted="yes"))
        row = res["row"]
        self.assertEqual([row["Findings: Both Found"], row["Findings: AI Only, Real"],
                          row["Findings: AI Only, False"], row["Findings: Human Only"]], [1, 1, 1, 0])
        self.assertEqual((row["Recommendation"], row["Accepted Recommendation"], row["Recommendation Severity"]),
                         ("YES", "YES", "Nit"))

    def test_reviewed_with_no_findings_writes_zeros(self):
        facts = base_facts(findings=[])
        res = self.build(facts, {"false_ids": [], "both_found": 0, "human_only": 0})
        self.assertEqual(res["row"]["Findings: AI Only, Real"], 0)
        self.assertEqual(res["row"]["Recommendation Severity"], "")

    def test_eval_case_skeletons(self):
        res = self.build(answers={"miss_summaries": ["Refund total ignores tax when coupon applied"]})
        cases = res["eval_cases"]
        self.assertEqual(len(cases), 2)
        self.assertEqual(cases[0]["path"], "evals/review/cases/widgets/widgets-pr7-miss1.json")
        self.assertEqual(cases[0]["case"]["source"], "human-only-finding")
        self.assertEqual(cases[0]["case"]["added"], "2026-11-20")
        self.assertTrue(cases[1]["case"]["known_problem"]["summary"].startswith("TODO"))

    def test_short_miss_summary_falls_back_to_todo(self):
        res = self.build(answers={"human_only": 1, "miss_summaries": ["XSS bug"]})
        case = res["eval_cases"][0]
        self.assertTrue(case["case"]["known_problem"]["summary"].startswith("TODO"))
        self.assertTrue(case["valid"])
        self.assertTrue(any("too short" in w for w in res["warnings"]))
        self.assertIn("onion-factory", case["path_base"])
        self.assertEqual(logrow.eval_case_errors(case["case"]), [])

    def test_ticket_defaults_to_na(self):
        res = self.build(base_facts(tickets=[]))
        self.assertEqual(res["row"]["Ticket"], "N/A")


@unittest.skipUnless(REPO_SCHEMA.is_file(), "repo schema not present")
class SchemaContractTest(unittest.TestCase):
    def test_embedded_schema_matches_repo_schema(self):
        schema = json.loads(REPO_SCHEMA.read_text())
        self.assertEqual(schema["x-column-order"], logrow.COLUMNS)
        stripped = {k: {kk: vv for kk, vv in v.items() if kk != "description"}
                    for k, v in schema["properties"].items()}
        self.assertEqual(stripped, logrow.EMBEDDED_SCHEMA["properties"])
        self.assertEqual(schema["required"], logrow.EMBEDDED_SCHEMA["required"])

    def test_eval_case_constraints_match_repo_schema(self):
        schema = json.loads((REPO_SCHEMA.parent / "eval-case.schema.json").read_text())
        props = schema["properties"]
        self.assertEqual(props["id"]["pattern"], logrow.EVAL_ID_PATTERN)
        self.assertEqual(props["repo"]["pattern"], logrow.EVAL_REPO_PATTERN)
        self.assertEqual(props["known_problem"]["properties"]["summary"]["minLength"], logrow.EVAL_SUMMARY_MIN)
        self.assertEqual(props["known_problem"]["properties"]["severity"]["enum"], ["Important", "Nit"])
        self.assertEqual(props["source"]["enum"], ["bug-reached-users", "human-only-finding", "other"])
        res = logrow.build(base_facts(), dict(ANSWERS, miss_summaries=["Refund total ignores tax with a coupon"]),
                           today=dt.date(2026, 11, 20))
        try:
            import jsonschema  # type: ignore
        except ImportError:
            jsonschema = None
        for c in res["eval_cases"]:
            self.assertEqual(logrow.eval_case_errors(c["case"]), [])
            if jsonschema is not None:
                jsonschema.validate(c["case"], schema)

    def test_built_row_valid_against_repo_schema(self):
        schema = json.loads(REPO_SCHEMA.read_text())
        row = logrow.build(base_facts(), dict(ANSWERS), today=dt.date(2026, 11, 20))["row"]
        self.assertEqual(logrow.validate_row(row, schema), [])
        bad = dict(row, **{"Date Reviewed": "Nov 5 2026"})
        self.assertTrue(logrow.validate_row(bad, schema))


class GatherTest(unittest.TestCase):
    """gather() against canned gh responses (no network)."""

    def setUp(self):
        self.calls = []
        pr = {"number": 7, "title": "WID-12 Fix checkout", "url": "https://github.com/acme/widgets/pull/7",
              "headRefName": "wid-12-checkout", "headRefOid": HEAD, "state": "MERGED", "isDraft": False,
              "mergedAt": "2026-11-19T15:00:00Z", "updatedAt": "2026-11-19T15:00:00Z",
              "additions": 40, "deletions": 5, "changedFiles": 2,
              "commits": [{"oid": FIRST}, {"oid": HEAD}], "latestReviews": []}
        comments = [comment(1, F1), comment(2, F2, line=40, created="2026-11-18T10:00:01Z")]
        threads = {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"isResolved": True, "comments": {"totalCount": 2, "nodes": [{"databaseId": 1}]}},
                      {"isResolved": False, "comments": {"totalCount": 1, "nodes": [{"databaseId": 2}]}}]}}}}}
        compare = {"files": [{"filename": "src/app.py", "status": "modified",
                              "patch": "@@ -10,5 +10,5 @@\n a\n b\n-c\n+C\n d\n e"}]}
        self.reviews = [review(10, "<!-- factory:review head=%s findings=2 -->\nSummary" % FIRST)]

        def fake_gh(args, check=True):
            self.calls.append(args)
            joined = " ".join(args)
            if args[:2] == ["pr", "view"]:
                return json.dumps(pr)
            if "pulls/7/comments" in joined:
                return json.dumps(comments)
            if "pulls/7/reviews" in joined:
                return json.dumps(self.reviews)
            if args[:2] == ["api", "graphql"]:
                return json.dumps(threads)
            if "/compare/" in joined:
                return json.dumps(compare)
            if "check-runs" in joined:
                return json.dumps({"check_runs": []})
            if args[:2] == ["api", "user"]:
                return "Pat Example\n"
            raise AssertionError("unexpected gh call: %s" % joined)

        self._orig = logrow.gh
        logrow.gh = fake_gh

    def tearDown(self):
        logrow.gh = self._orig

    def test_gather_end_to_end(self):
        facts = logrow.gather("https://github.com/acme/widgets/pull/7", None)
        self.assertEqual(facts["repo"], "acme/widgets")
        self.assertEqual(facts["tickets"], ["WID-12"])
        self.assertTrue(facts["ai_reviewed"])
        self.assertEqual([f["key"] for f in facts["findings"]], ["F1", "F2"])
        self.assertEqual(facts["findings"][0]["fixed"], True)
        self.assertEqual(facts["findings"][1]["thread"]["resolved"], False)
        self.assertTrue(facts["warnings"][0].startswith("UNANSWERED FINDINGS: thread(s) F2"))
        self.assertEqual(facts["reviewer_suggestion"], "Pat")
        self.assertEqual(facts["complexity_suggestion"], "Low")
        self.assertFalse(facts["managed"]["detected"])
        for call in self.calls:  # read-only: no -X and no mutation
            self.assertNotIn("-X", call)
            self.assertFalse(any("mutation" in a for a in call))

    def test_gather_warns_about_findings_without_a_thread(self):
        self.reviews = [review(10, SUMMARY)]  # F3 lives only in the review body
        facts = logrow.gather("https://github.com/acme/widgets/pull/7", None)
        self.assertTrue(facts["warnings"][0].startswith("UNANSWERED FINDINGS: thread(s) F2"))
        self.assertTrue(facts["warnings"][1].startswith("UNCHECKED FINDINGS: F3"))
        self.assertFalse(facts["repeat_check_needed"])
        self.assertEqual(facts["ticket_source"], "title")


if __name__ == "__main__":
    unittest.main()
