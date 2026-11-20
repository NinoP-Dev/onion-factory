"""Tests for evals/review/run_evals.py.

No network and no real claude/gh: a FakeRunner answers claude and gh, and
passes git through to a throwaway repository built in each test.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("run_evals", REPO_ROOT / "evals" / "review" / "run_evals.py")
rp = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules["run_evals"] = rp  # dataclasses look the module up while the class is built
_SPEC.loader.exec_module(rp)  # type: ignore

CASE_SCHEMA = rp.load_schema("eval-case.schema.json")
REVIEW_SCHEMA = rp.load_schema("review-result.schema.json")
RESULT_SCHEMA = rp.load_schema("eval-result.schema.json")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout.decode().strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(root: Path) -> Dict[str, Any]:
    """A repo with a merged feature branch and a later standards commit.

    main:   c0 (old standards, app v1) -- merge(feature: app v2) -- standards (new standards)
    """
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sh(repo, "checkout", "-q", "-b", "main")
    sh(repo, "config", "user.name", "Test Author")
    sh(repo, "config", "user.email", "author@example.com")
    sh(repo, "config", "commit.gpgsign", "false")
    write(repo / "CLAUDE.md", "old claude\n")
    write(repo / "REVIEW.md", "old review\n")
    write(repo / ".claude/rules/old.md", "old rule\n")
    write(repo / "app.py", "def total(x):\n    return x\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "initial")
    c0 = sh(repo, "rev-parse", "HEAD")
    sh(repo, "checkout", "-q", "-b", "feature")
    write(repo / "app.py", "def total(x):\n    return x * 2\n")
    sh(repo, "commit", "-q", "-am", "WID-1 double it")
    feature = sh(repo, "rev-parse", "HEAD")
    sh(repo, "checkout", "-q", "main")
    sh(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge feature")
    merge = sh(repo, "rev-parse", "HEAD")
    write(repo / "CLAUDE.md", "new claude\n")
    write(repo / "REVIEW.md", "## Always check\n- new review\n")
    (repo / ".claude/rules/old.md").unlink()
    write(repo / ".claude/rules/api.md", "new rule\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "update standards")
    standards = sh(repo, "rev-parse", "HEAD")
    return {"repo": repo, "c0": c0, "feature": feature, "merge": merge, "standards": standards}


class FakeRunner:
    """Scripted claude/gh; real git. Records every call."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.handlers: List[tuple] = []

    def on(self, predicate: Callable[[List[str]], bool], handler: Callable[..., Any]) -> "FakeRunner":
        self.handlers.append((predicate, handler))
        return self

    def __call__(self, cmd, cwd=None, timeout=None):
        cmd = list(cmd)
        self.calls.append((cmd, cwd))
        for predicate, handler in self.handlers:
            if predicate(cmd):
                return handler(cmd, cwd)
        if cmd[0] == "git":
            return rp.subprocess_runner(cmd, cwd, timeout)
        raise AssertionError("unexpected command: {!r}".format(cmd))

    def claude_calls(self) -> List[List[str]]:
        return [c for c, _ in self.calls if c[0] == "claude"]


def is_review(cmd: List[str]) -> bool:
    return cmd[0] == "claude" and "--plugin-dir" in cmd


def is_judge(cmd: List[str]) -> bool:
    return cmd[0] == "claude" and "--plugin-dir" not in cmd


def ok(stdout: str) -> Any:
    return rp.CommandResult(0, stdout, "")


def envelope(result: str = "", structured: Any = None, is_error: bool = False, subtype: str = "success") -> str:
    env: Dict[str, Any] = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "session_id": "s",
        "total_cost_usd": 0.05,
    }
    if structured is not None:
        env["structured_output"] = structured
    return json.dumps(env)


def finding(fid: str = "F1", severity: str = "Important", title: str = "total doubles the amount") -> Dict[str, Any]:
    return {"id": fid, "severity": severity, "file": "app.py", "line": 2, "title": title, "body": "x * 2 doubles."}


def review_result(
    findings: List[Dict[str, Any]],
    base: str = "a" * 40,
    head: str = "b" * 40,
    summary: str = "Doubles totals. One Important bug.",
) -> Dict[str, Any]:
    return {
        "repo": "acme/widgets",
        "pr": None,
        "ticket": None,
        "base": base,
        "head": head,
        "standards": {"claude_md": True, "review_md": True, "rules": []},
        "findings": findings,
        "summary": summary,
        "posted": False,
    }


def good_case(**overrides: Any) -> Dict[str, Any]:
    case: Dict[str, Any] = {
        "id": "double-total",
        "repo": "acme/widgets",
        "pr": 7,
        "known_problem": {"summary": "total() doubles every amount", "files": ["app.py"], "severity": "Important"},
        "source": "bug-reached-users",
        "reference": "WID-1",
        "added": "2026-11-01",
    }
    case.update(overrides)
    return case


def make_ctx(repo: Path, runner: FakeRunner, tmp: Path, ref: str = "main", keep: bool = False):
    plugin_dir = tmp / "plugin"
    write(plugin_dir / ".claude-plugin" / "plugin.json", '{"name": "factory"}')
    cfg = rp.Config(
        project="t",
        repo_path=repo,
        cases_dir=tmp / "cases",
        out_dir=tmp / "out",
        standards_ref=ref,
        threshold=8,
        only=[],
        model=None,
        plugin_dir=plugin_dir,
        dry_run=False,
        keep_worktrees=keep,
        timeout=30,
        fetch=False,
    )
    standards = rp.resolve_standards(runner, repo, ref)
    return rp.Context(
        config=cfg,
        runner=runner,
        standards=standards,
        review_schema=REVIEW_SCHEMA,
        review_schema_cli=rp.schema_for_cli(REVIEW_SCHEMA),
        judge_schema_cli=rp.schema_for_cli(rp.JUDGE_SCHEMA),
    )


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test-run-evals-")).resolve()

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)


# ---------------------------------------------------------------------------
# case validation
# ---------------------------------------------------------------------------


class CaseValidationTest(TempDirTest):
    def test_good_pr_case(self):
        self.assertEqual(rp.validate_case(good_case(), "double-total", CASE_SCHEMA), [])

    def test_good_base_head_case(self):
        case = good_case()
        del case["pr"]
        case.update(base="abc1234", head="def5678")
        self.assertEqual(rp.validate_case(case, "double-total", CASE_SCHEMA), [])

    def test_bad_shapes(self):
        def without(key):
            c = good_case()
            del c[key]
            return c

        base_only = good_case(base="abc1234")
        del base_only["pr"]
        neither = good_case()
        del neither["pr"]
        bad = {
            "missing id": without("id"),
            "missing repo": without("repo"),
            "missing known_problem": without("known_problem"),
            "missing source": without("source"),
            "pr and base/head": good_case(base="abc", head="def"),
            "base without head": base_only,
            "neither pr nor base/head": neither,
            "pr as string": good_case(pr="7"),
            "pr zero": good_case(pr=0),
            "pr as bool": good_case(pr=True),
            "bad id": good_case(id="Double_Total"),
            "repo without owner": good_case(repo="widgets"),
            "short summary": good_case(known_problem={"summary": "bad"}),
            "bad severity": good_case(known_problem={"summary": "total() doubles amounts", "severity": "Minor"}),
            "extra known_problem key": good_case(known_problem={"summary": "total() doubles amounts", "why": "x"}),
            "bad source": good_case(source="hunch"),
            "extra field": good_case(notes="hello"),
            "not an object": ["double-total"],
        }
        for name, case in bad.items():
            with self.subTest(name):
                stem = case.get("id", "double-total") if isinstance(case, dict) else "double-total"
                self.assertNotEqual(rp.validate_case(case, stem, CASE_SCHEMA), [], name)

    def test_id_must_match_file_name(self):
        errors = rp.validate_case(good_case(), "other-name", CASE_SCHEMA)
        self.assertTrue(any("file name" in e for e in errors), errors)

    def test_load_cases_reports_every_bad_file(self):
        cases = self.tmp / "cases"
        write(cases / "double-total.json", json.dumps(good_case()))
        write(cases / "broken.json", "{not json")
        write(cases / "bad-source.json", json.dumps(good_case(id="bad-source", source="hunch")))
        write(cases / "README.md", "ignored")
        loaded, errors = rp.load_cases(cases, CASE_SCHEMA)
        self.assertEqual([c["id"] for c in loaded], ["double-total"])
        self.assertEqual(len([e for e in errors if e.startswith("broken.json")]), 1)
        self.assertTrue(any(e.startswith("bad-source.json") for e in errors))

    def test_load_cases_empty_or_missing_dir(self):
        self.assertTrue(rp.load_cases(self.tmp / "nope", CASE_SCHEMA)[1])
        (self.tmp / "empty").mkdir()
        self.assertTrue(rp.load_cases(self.tmp / "empty", CASE_SCHEMA)[1])

    def test_example_case_is_valid(self):
        loaded, errors = rp.load_cases(REPO_ROOT / "evals" / "review" / "cases" / "example", CASE_SCHEMA)
        self.assertEqual(errors, [])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["repo"], "acme/widgets")

    def test_builtin_validator_types(self):
        schema = {"type": "integer", "minimum": 1}
        self.assertTrue(rp.builtin_validate(True, schema))
        self.assertTrue(rp.builtin_validate(0, schema))
        self.assertEqual(rp.builtin_validate(3, schema), [])
        self.assertEqual(rp.builtin_validate(None, {"type": ["string", "null"]}), [])
        self.assertEqual(rp.builtin_validate(good_case(), CASE_SCHEMA), [])
        self.assertTrue(rp.builtin_validate(good_case(base="a", head="b"), CASE_SCHEMA))


# ---------------------------------------------------------------------------
# commits, standards, worktrees
# ---------------------------------------------------------------------------


class GitTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.r = make_repo(self.tmp)
        self.repo = self.r["repo"]

    def gh_merged(self, oid: Optional[str], state: str = "MERGED") -> FakeRunner:
        payload = {"number": 7, "state": state, "mergeCommit": {"oid": oid} if oid else None,
                   "baseRefName": "main", "headRefOid": self.r["feature"]}
        return FakeRunner().on(lambda c: c[:3] == ["gh", "pr", "view"], lambda c, cwd: ok(json.dumps(payload)))

    def test_pr_resolves_to_first_parent_and_merge(self):
        runner = self.gh_merged(self.r["merge"])
        base, head = rp.resolve_case_commits(runner, good_case(), self.repo)
        self.assertEqual((base, head), (self.r["c0"], self.r["merge"]))
        gh = [c for c, _ in runner.calls if c[0] == "gh"][0]
        self.assertIn("-R", gh)
        self.assertIn("acme/widgets", gh)

    def test_unmerged_pr_is_an_error(self):
        with self.assertRaises(rp.EvalError):
            rp.resolve_case_commits(self.gh_merged(None, state="OPEN"), good_case(), self.repo)
        with self.assertRaises(rp.EvalError):
            rp.resolve_case_commits(self.gh_merged(None), good_case(), self.repo)

    def test_base_head_resolve_to_full_shas(self):
        case = good_case()
        del case["pr"]
        case.update(base=self.r["c0"][:10], head="feature")
        base, head = rp.resolve_case_commits(FakeRunner(), case, self.repo)
        self.assertEqual((base, head), (self.r["c0"], self.r["feature"]))

    def test_missing_commit_is_fetched_from_origin(self):
        sha = "f" * 40
        state = {"fetched": False}

        def cat_file(cmd, cwd):
            return rp.CommandResult(0 if state["fetched"] else 1, "", "")

        def fetch(cmd, cwd):
            state["fetched"] = True
            return ok("")

        runner = (FakeRunner()
                  .on(lambda c: "cat-file" in c and "-e" in c, cat_file)
                  .on(lambda c: "fetch" in c, fetch)
                  .on(lambda c: "rev-parse" in c, lambda c, cwd: ok(sha + "\n")))
        self.assertEqual(rp.ensure_commit(runner, self.repo, sha), sha)
        self.assertIn(["git", "-C", str(self.repo), "fetch", "--quiet", "origin", sha], [c for c, _ in runner.calls])

    def test_missing_commit_fetch_failure(self):
        runner = (FakeRunner()
                  .on(lambda c: "cat-file" in c, lambda c, cwd: rp.CommandResult(1, "", ""))
                  .on(lambda c: "fetch" in c, lambda c, cwd: rp.CommandResult(128, "", "fatal: no such remote")))
        with self.assertRaises(rp.EvalError):
            rp.ensure_commit(runner, self.repo, "f" * 40)

    def test_resolve_standards_records_blob_shas(self):
        st = rp.resolve_standards(FakeRunner(), self.repo, "main")
        self.assertEqual(st.commit, self.r["standards"])
        self.assertEqual(st.review_md_sha, sh(self.repo, "rev-parse", "main:REVIEW.md"))
        self.assertEqual(st.claude_md_sha, sh(self.repo, "rev-parse", "main:CLAUDE.md"))
        self.assertEqual(sorted(st.files), [".claude/rules/api.md", "CLAUDE.md", "REVIEW.md"])

    def test_resolve_standards_bad_ref(self):
        with self.assertRaises(rp.EvalError):
            rp.resolve_standards(FakeRunner(), self.repo, "origin/HEAD")

    def test_overlay_copies_files_from_ref(self):
        runner = FakeRunner()
        st = rp.resolve_standards(runner, self.repo, "main")
        parent, wt = rp.create_worktree(runner, self.repo, self.r["merge"], "double-total")
        try:
            self.assertEqual((wt / "CLAUDE.md").read_text(), "old claude\n")
            rp.overlay_standards(runner, self.repo, st, wt)
            self.assertEqual((wt / "CLAUDE.md").read_text(), "new claude\n")
            self.assertEqual((wt / "REVIEW.md").read_text(), "## Always check\n- new review\n")
            self.assertEqual((wt / ".claude/rules/api.md").read_text(), "new rule\n")
            self.assertFalse((wt / ".claude/rules/old.md").exists())
            self.assertEqual((wt / "app.py").read_text(), "def total(x):\n    return x * 2\n")
            self.assertEqual(sh(wt, "rev-parse", "HEAD"), self.r["merge"])
            # The overlay is not part of the change under review.
            self.assertEqual(sh(wt, "diff", "HEAD"), "")
            self.assertEqual(sh(wt, "diff", "--name-only", self.r["c0"]), "app.py")
        finally:
            rp.remove_worktree(runner, self.repo, parent, wt)
        self.assertFalse(parent.exists())
        self.assertEqual(sh(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_overlay_removes_files_missing_at_ref(self):
        runner = FakeRunner()
        st = rp.resolve_standards(runner, self.repo, self.r["c0"])  # has old files only
        st.files.pop("REVIEW.md")
        parent, wt = rp.create_worktree(runner, self.repo, self.r["merge"], "x")
        try:
            rp.overlay_standards(runner, self.repo, st, wt)
            self.assertFalse((wt / "REVIEW.md").exists())
            self.assertEqual((wt / ".claude/rules/old.md").read_text(), "old rule\n")
        finally:
            rp.remove_worktree(runner, self.repo, parent, wt)

    def commit_symlinked_standards(self) -> str:
        """CLAUDE.md -> AGENTS.md, .claude/rules -> docs/rules (with a nested link),
        REVIEW.md -> a path outside the repo."""
        repo = self.repo
        write(repo / "AGENTS.md", "agents guide\n")
        write(repo / "docs/rules/api.md", "linked rule\n")
        write(repo / "docs/shared.md", "shared rule\n")
        os.symlink("../shared.md", str(repo / "docs/rules/shared.md"))
        (repo / "CLAUDE.md").unlink()
        os.symlink("AGENTS.md", str(repo / "CLAUDE.md"))
        (repo / "REVIEW.md").unlink()
        os.symlink("../outside/REVIEW.md", str(repo / "REVIEW.md"))
        shutil.rmtree(str(repo / ".claude/rules"))
        os.symlink("../docs/rules", str(repo / ".claude/rules"))
        sh(repo, "add", "-A")
        sh(repo, "commit", "-q", "-m", "symlinked standards")
        return sh(repo, "rev-parse", "HEAD")

    def test_symlinked_standards_are_resolved(self):
        self.commit_symlinked_standards()
        runner = FakeRunner()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st = rp.resolve_standards(runner, self.repo, "main")
        self.assertEqual(st.claude_md_sha, sh(self.repo, "rev-parse", "main:AGENTS.md"))
        self.assertIsNone(st.review_md_sha)
        self.assertIn("REVIEW.md", err.getvalue())
        self.assertEqual(st.files[".claude/rules/api.md"], sh(self.repo, "rev-parse", "main:docs/rules/api.md"))
        self.assertEqual(st.files[".claude/rules/shared.md"], sh(self.repo, "rev-parse", "main:docs/shared.md"))
        self.assertEqual(sorted(st.files), [".claude/rules/api.md", ".claude/rules/shared.md", "CLAUDE.md"])
        parent, wt = rp.create_worktree(runner, self.repo, self.r["merge"], "links")
        try:
            rp.overlay_standards(runner, self.repo, st, wt)
            self.assertFalse((wt / "CLAUDE.md").is_symlink())
            self.assertEqual((wt / "CLAUDE.md").read_text(), "agents guide\n")
            self.assertEqual((wt / ".claude/rules/api.md").read_text(), "linked rule\n")
            self.assertEqual((wt / ".claude/rules/shared.md").read_text(), "shared rule\n")
            self.assertFalse((wt / "REVIEW.md").exists())
        finally:
            rp.remove_worktree(runner, self.repo, parent, wt)

    def test_symlink_loop_is_left_out(self):
        repo = self.repo
        (repo / "CLAUDE.md").unlink()
        os.symlink("LOOP.md", str(repo / "CLAUDE.md"))
        os.symlink("CLAUDE.md", str(repo / "LOOP.md"))
        sh(repo, "add", "-A")
        sh(repo, "commit", "-q", "-m", "loop")
        with contextlib.redirect_stderr(io.StringIO()):
            st = rp.resolve_standards(FakeRunner(), repo, "main")
        self.assertIsNone(st.claude_md_sha)
        self.assertIn("REVIEW.md", st.files)

    def test_review_target_check(self):
        runner = FakeRunner()
        c0, merge, feature = self.r["c0"], self.r["merge"], self.r["feature"]
        rp.check_review_target(runner, self.repo, review_result([], base=c0, head=merge), c0, merge)
        rp.check_review_target(runner, self.repo, review_result([], base=c0[:7], head=merge[:12].upper()), c0, merge)
        # Base-ref mode diffs from merge-base(base, HEAD); that is accepted too.
        rp.check_review_target(runner, self.repo, review_result([], base=c0, head=c0), feature, c0)
        bad = {
            "unknown": review_result([], base="unknown", head="unknown", summary="base commit abc not found locally"),
            "wrong head": review_result([], base=c0, head=feature),
            "wrong base": review_result([], base=feature, head=merge),
            "ref text": review_result([], base="main", head=merge),
            "too short": review_result([], base=c0[:6], head=merge),
        }
        for name, review in bad.items():
            with self.subTest(name):
                with self.assertRaises(rp.EvalError) as ctx:
                    rp.check_review_target(runner, self.repo, review, c0, merge)
                self.assertIn(review["summary"], str(ctx.exception))


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


class ExtractJsonTest(unittest.TestCase):
    OBJ = {"findings": [{"id": "F1", "title": "a {brace} in a string"}], "summary": "s"}

    def test_plain(self):
        self.assertEqual(rp.extract_json(json.dumps(self.OBJ)), self.OBJ)

    def test_fenced_with_and_without_language(self):
        for fence in ("```json\n{}\n```", "```\n{}\n```"):
            text = "Here is the result:\n" + fence.format(json.dumps(self.OBJ, indent=2)) + "\nDone."
            self.assertEqual(rp.extract_json(text), self.OBJ)

    def test_unfenced_inside_prose(self):
        text = "Review complete. " + json.dumps(self.OBJ) + " That's all."
        self.assertEqual(rp.extract_json(text), self.OBJ)

    def test_predicate_skips_other_objects(self):
        text = 'Example: {"found": false} and then ' + json.dumps(self.OBJ)
        got = rp.extract_json(text, lambda d: "findings" in d)
        self.assertEqual(got, self.OBJ)
        self.assertEqual(rp.extract_json(text, lambda d: "found" in d), {"found": False})

    def test_last_top_level_object_wins(self):
        text = '{"findings": [], "summary": "draft"}\nfinal:\n{"findings": [], "summary": "final"}'
        self.assertEqual(rp.extract_json(text)["summary"], "final")

    def test_garbage(self):
        for text in ("", "   ", "no json here", "{broken", "[1, 2, 3]"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    rp.extract_json(text)


class CliOutputTest(unittest.TestCase):
    def test_structured_output_preferred(self):
        res = rp.extract_review_result(
            rp.parse_cli_output(envelope(result="ignored", structured=review_result([finding()]))), REVIEW_SCHEMA
        )
        self.assertEqual(res["findings"][0]["id"], "F1")

    def test_falls_back_to_result_text(self):
        text = "```json\n" + json.dumps(review_result([finding()])) + "\n```"
        cli = rp.parse_cli_output(envelope(result=text))
        self.assertIsNone(cli.structured)
        self.assertEqual(cli.cost_usd, 0.05)
        self.assertEqual(rp.extract_review_result(cli, REVIEW_SCHEMA)["findings"][0]["file"], "app.py")

    def test_error_envelope(self):
        with self.assertRaises(rp.EvalError):
            rp.parse_cli_output(envelope(result="boom", is_error=True))
        with self.assertRaises(rp.EvalError):
            rp.parse_cli_output(envelope(result="", subtype="error_max_turns"))

    def test_non_json_stdout(self):
        with self.assertRaises(rp.EvalError):
            rp.parse_cli_output("Error: something went wrong")

    def test_invalid_findings_rejected(self):
        bad = finding()
        bad["severity"] = "Minor"
        with self.assertRaises(rp.EvalError):
            rp.extract_review_result(rp.parse_cli_output(envelope(structured=review_result([bad]))), REVIEW_SCHEMA)

    def test_long_title_truncated(self):
        long = finding(title="x" * 200)
        res = rp.extract_review_result(rp.parse_cli_output(envelope(structured=review_result([long]))), REVIEW_SCHEMA)
        self.assertEqual(len(res["findings"][0]["title"]), 120)


class JudgeTest(unittest.TestCase):
    IDS = ["F1", "F2"]

    def test_dict_answer(self):
        v = rp.parse_judge({"found": True, "matched_finding_ids": ["F2"], "reasoning": "same bug"}, self.IDS)
        self.assertEqual(v, rp.Verdict(True, ["F2"], "same bug"))

    def test_text_answer_fenced(self):
        text = '```json\n{"found": false, "matched_finding_ids": [], "reasoning": "different issue"}\n```'
        self.assertEqual(rp.parse_judge(text, self.IDS), rp.Verdict(False, [], "different issue"))

    def test_unknown_ids_dropped_and_found_requires_a_real_id(self):
        v = rp.parse_judge({"found": True, "matched_finding_ids": ["F9"], "reasoning": "r"}, self.IDS)
        self.assertFalse(v.found)
        self.assertEqual(v.matched_finding_ids, [])
        self.assertIn("runner:", v.reasoning)
        v = rp.parse_judge({"found": True, "matched_finding_ids": ["F1", "F9", "F1"], "reasoning": "r"}, self.IDS)
        self.assertEqual((v.found, v.matched_finding_ids), (True, ["F1"]))

    def test_not_found_clears_ids(self):
        v = rp.parse_judge({"found": False, "matched_finding_ids": ["F1"], "reasoning": "r"}, self.IDS)
        self.assertEqual((v.found, v.matched_finding_ids), (False, []))

    def test_bad_answers(self):
        for answer in ({"found": "yes", "matched_finding_ids": []},
                       {"found": True, "matched_finding_ids": "F1"},
                       {"matched_finding_ids": []},
                       "I think it was found."):
            with self.subTest(answer=answer):
                with self.assertRaises(rp.EvalError):
                    rp.parse_judge(answer, self.IDS)

    def test_prompt_contains_problem_and_findings(self):
        prompt = rp.build_judge_prompt(good_case(), [finding()])
        self.assertIn("total() doubles every amount", prompt)
        self.assertIn('"F1"', prompt)
        self.assertIn("app.py", prompt)


# ---------------------------------------------------------------------------
# commands, scoring
# ---------------------------------------------------------------------------


class CommandTest(unittest.TestCase):
    def test_review_cmd_is_read_only_and_never_posts(self):
        cmd = rp.build_review_cmd("abc123", Path("/p/factory"), "opus", "{}")
        self.assertEqual(cmd[:3], ["claude", "-p", "/factory:review-pr abc123 --json"])
        self.assertNotIn("--comment", " ".join(cmd))
        self.assertEqual(cmd[cmd.index("--plugin-dir") + 1], "/p/factory")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
        for rule in ("Bash(gh *)", "Bash(curl *)", "Edit", "Write"):
            self.assertIn(rule, denied)
        allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
        self.assertNotIn("Edit", allowed)
        self.assertFalse([a for a in allowed if a.startswith("Bash(gh")], allowed)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")

    def test_judge_cmd_has_no_tools(self):
        cmd = rp.build_judge_cmd("prompt", None, "{}")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--plugin-dir", cmd)
        self.assertNotIn("--model", cmd)

    def test_cli_schema_is_compact_and_has_no_refs(self):
        text = rp.schema_for_cli(REVIEW_SCHEMA)
        self.assertNotIn("$ref", text)
        self.assertNotIn("$schema", text)
        self.assertIn('"severity"', text)


class ScoreTest(unittest.TestCase):
    def cases(self, found: int, missed: int, errors: int) -> List[Dict[str, Any]]:
        out = [{"found": True, "error": None}] * found + [{"found": False, "error": None}] * missed
        return out + [{"found": None, "error": "boom"}] * errors

    def test_pass(self):
        score, passed = rp.score_cases(self.cases(8, 2, 0), 8)
        self.assertEqual(score, {"found": 8, "total": 10, "errors": 0})
        self.assertTrue(passed)
        self.assertEqual(rp.exit_code_for(score, passed), 0)
        self.assertEqual(rp.verdict_line(score, passed, 8, 10), "PASS 8/10 (threshold 8)")

    def test_fail(self):
        score, passed = rp.score_cases(self.cases(7, 3, 0), 8)
        self.assertFalse(passed)
        self.assertEqual(rp.exit_code_for(score, passed), 1)
        self.assertTrue(rp.verdict_line(score, passed, 8, 10).startswith("FAIL 7/10"))

    def test_errors_fail_and_exit_2_even_above_threshold(self):
        score, passed = rp.score_cases(self.cases(9, 0, 1), 8)
        self.assertEqual(score, {"found": 9, "total": 9, "errors": 1})
        self.assertFalse(passed)
        self.assertEqual(rp.exit_code_for(score, passed), 2)

    def test_run_id(self):
        import datetime as dt

        t = dt.datetime(2026, 11, 19, 8, 5, 9, tzinfo=dt.timezone.utc)
        self.assertEqual(rp.make_run_id(t, "abcdef0123"), "20261119T080509Z-abcdef0")
        self.assertEqual(rp.make_run_id(t, None), "20261119T080509Z-none")


# ---------------------------------------------------------------------------
# whole runs
# ---------------------------------------------------------------------------


class RunTest(GitTest):
    def setUp(self) -> None:
        super().setUp()
        self.cases_dir = self.tmp / "cases"
        self.out_dir = self.tmp / "out"
        self.plugin_dir = self.tmp / "plugin"
        write(self.plugin_dir / ".claude-plugin" / "plugin.json", '{"name": "factory"}')
        write(self.cases_dir / "a-pr.json", json.dumps(good_case(id="a-pr")))
        bh = good_case(id="b-range", base=self.r["c0"], head=self.r["merge"])
        del bh["pr"]
        write(self.cases_dir / "b-range.json", json.dumps(bh))
        bh2 = dict(bh, id="c-range")
        write(self.cases_dir / "c-range.json", json.dumps(bh2))

    def runner(self, reviews: List[Any], judges: List[Any]) -> FakeRunner:
        reviews, judges = list(reviews), list(judges)
        payload = {"number": 7, "state": "MERGED", "mergeCommit": {"oid": self.r["merge"]}}

        def pop(queue):
            def handler(cmd, cwd):
                item = queue.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item if isinstance(item, rp.CommandResult) else ok(item)
            return handler

        return (FakeRunner()
                .on(lambda c: c[:3] == ["gh", "pr", "view"], lambda c, cwd: ok(json.dumps(payload)))
                .on(is_review, pop(reviews))
                .on(is_judge, pop(judges)))

    def main(self, runner: FakeRunner, *extra: str) -> tuple:
        argv = ["--project", "t", "--repo-path", str(self.repo), "--cases", str(self.cases_dir),
                "--out", str(self.out_dir), "--standards-ref", "main", "--plugin-dir", str(self.plugin_dir),
                "--no-fetch", "--threshold", "2", "--timeout", "30"] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rp.main(argv, runner=runner)
        return code, out.getvalue(), err.getvalue()

    def rr(self, findings: List[Dict[str, Any]], **kw: Any) -> Dict[str, Any]:
        """A review result for this repo's cases (all resolve to c0..merge)."""
        kw.setdefault("base", self.r["c0"])
        kw.setdefault("head", self.r["merge"])
        return review_result(findings, **kw)

    def results_doc(self) -> Dict[str, Any]:
        files = list(self.out_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def assert_no_worktrees_left(self):
        self.assertEqual(sh(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def found(self, fid="F1"):
        return envelope(structured={"found": True, "matched_finding_ids": [fid], "reasoning": "same bug"})

    def missed(self):
        return envelope(result='{"found": false, "matched_finding_ids": [], "reasoning": "other issue"}')

    def test_pass_run_writes_valid_results(self):
        review = envelope(structured=self.rr([finding(), finding("F2", "Nit", "naming")]))
        empty = envelope(result=json.dumps(self.rr([])))
        runner = self.runner([review, review, empty], [self.found(), self.found("F2")])
        code, out, _ = self.main(runner)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS 2/3 (threshold 2)", out)
        doc = self.results_doc()
        self.assertEqual(rp.validate(doc, RESULT_SCHEMA), [])
        self.assertEqual(rp.builtin_validate(doc, RESULT_SCHEMA), [])
        review_sha = sh(self.repo, "rev-parse", "main:REVIEW.md")
        self.assertRegex(doc["run_id"], r"^\d{8}T\d{6}Z-" + review_sha[:7] + "$")
        self.assertEqual(doc["standards"]["review_md_sha"], review_sha)
        self.assertEqual(doc["standards"]["standards_ref"], self.r["standards"])
        self.assertEqual(doc["score"], {"found": 2, "total": 3, "errors": 0})
        self.assertTrue(doc["passed"])
        self.assertIsNone(doc["model"])
        self.assertEqual([c["case_id"] for c in doc["cases"]], ["a-pr", "b-range", "c-range"])
        self.assertEqual(doc["cases"][0]["base"], self.r["c0"])
        self.assertEqual(doc["cases"][0]["head"], self.r["merge"])
        self.assertEqual(doc["cases"][0]["matched_finding_ids"], ["F1"])
        self.assertEqual(doc["cases"][2]["judge_reasoning"], "The review reported no findings.")
        # Two claude calls per case with findings, one when there are none.
        self.assertEqual(len(runner.claude_calls()), 5)
        self.assert_no_worktrees_left()

    def test_exit_codes_follow_threshold(self):
        review = envelope(structured=self.rr([finding()]))
        for threshold, expected in (("2", 0), ("3", 1)):
            with self.subTest(threshold=threshold):
                shutil.rmtree(str(self.out_dir), ignore_errors=True)
                runner = self.runner([review] * 3, [self.found(), self.found(), self.missed()])
                code, out, _ = self.main(runner, "--threshold", threshold)
                self.assertEqual(code, expected, out)
                self.assertIn(("PASS" if expected == 0 else "FAIL") + " 2/3 (threshold {})".format(threshold), out)

    def test_review_sees_overlaid_standards(self):
        seen = {}

        def review(cmd, cwd):
            seen["claude_md"] = (Path(cwd) / "CLAUDE.md").read_text()
            seen["head"] = sh(Path(cwd), "rev-parse", "HEAD")
            return ok(envelope(structured=self.rr([])))

        runner = FakeRunner().on(is_review, review)
        code, out, _ = self.main(runner, "--only", "b-range")
        self.assertEqual(code, 1, out)
        self.assertEqual(seen, {"claude_md": "new claude\n", "head": self.r["merge"]})

    def test_errors_are_isolated(self):
        good = envelope(structured=self.rr([finding()]))
        runner = self.runner(
            [envelope(result="I could not finish the review."), subprocess.TimeoutExpired("claude", 30), good],
            [self.found()],
        )
        code, out, _ = self.main(runner)
        self.assertEqual(code, 2, out)
        doc = self.results_doc()
        self.assertEqual(rp.validate(doc, RESULT_SCHEMA), [])
        a, b, c = doc["cases"]
        self.assertIsNone(a["found"])
        self.assertIn("could not read the review result", a["error"])
        self.assertIsNone(b["found"])
        self.assertIn("timed out", b["error"])
        self.assertTrue(c["found"])
        self.assertIsNone(c["error"])
        self.assertEqual(doc["score"], {"found": 1, "total": 1, "errors": 2})
        self.assertFalse(doc["passed"])
        self.assertIn("ERROR", out)
        self.assert_no_worktrees_left()

    def test_case_resolution_error_is_isolated(self):
        runner = (FakeRunner()
                  .on(lambda c: c[:3] == ["gh", "pr", "view"], lambda c, cwd: rp.CommandResult(1, "", "HTTP 404"))
                  .on(is_review, lambda c, cwd: ok(envelope(structured=review_result([])))))
        code, _, _ = self.main(runner)
        self.assertEqual(code, 2)
        doc = self.results_doc()
        self.assertIn("HTTP 404", doc["cases"][0]["error"])
        self.assertEqual(doc["cases"][0]["base"], "")
        self.assertEqual(rp.validate(doc, RESULT_SCHEMA), [])

    def test_failed_review_is_an_error_not_a_miss(self):
        fatal = envelope(structured=review_result(
            [], base="unknown", head="unknown", summary="base commit abc not found locally"))
        good = envelope(structured=self.rr([finding()]))
        runner = self.runner([fatal, good, good], [self.found(), self.found()])
        code, out, _ = self.main(runner)
        self.assertEqual(code, 2, out)
        doc = self.results_doc()
        a = doc["cases"][0]
        self.assertIsNone(a["found"])
        self.assertIn("did not run on this case's commits", a["error"])
        self.assertIn("base commit abc not found locally", a["error"])
        self.assertEqual(doc["score"], {"found": 2, "total": 2, "errors": 1})
        self.assertFalse(doc["passed"])
        # No judge call for the failed review.
        self.assertEqual(len([c for c in runner.claude_calls() if is_judge(c)]), 2)

    def main_no_out(self, runner: FakeRunner, *extra: str) -> tuple:
        argv = ["--project", "t", "--repo-path", str(self.repo), "--cases", str(self.cases_dir),
                "--standards-ref", "main", "--plugin-dir", str(self.plugin_dir),
                "--no-fetch", "--timeout", "30"] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rp.main(argv, runner=runner)
        return code, out.getvalue(), err.getvalue()

    @contextlib.contextmanager
    def fake_results_dirs(self):
        saved = (rp.RESULTS_DIR, rp.PARTIAL_RESULTS_DIR)
        rp.RESULTS_DIR, rp.PARTIAL_RESULTS_DIR = self.tmp / "results", self.tmp / "partial"
        try:
            yield rp.RESULTS_DIR, rp.PARTIAL_RESULTS_DIR
        finally:
            rp.RESULTS_DIR, rp.PARTIAL_RESULTS_DIR = saved

    def test_partial_run_defaults_to_partial_folder(self):
        with self.fake_results_dirs() as (results, partial):
            runner = FakeRunner().on(is_review, lambda c, cwd: ok(envelope(structured=self.rr([]))))
            code, out, err = self.main_no_out(runner, "--only", "b-range", "--threshold", "1")
            self.assertEqual(code, 1, out + err)
            self.assertEqual(len(list((partial / "t").glob("*.json"))), 1)
            self.assertFalse(results.exists())
            self.assertIn("partial run", err)

    def test_partial_run_may_not_write_to_results(self):
        with self.fake_results_dirs() as (results, _partial):
            runner = self.runner([], [])
            code, _, err = self.main(runner, "--only", "b-range", "--out", str(results / "t"))
            self.assertEqual(code, 2)
            self.assertIn("partial run", err)
            self.assertEqual(runner.claude_calls(), [])
            self.assertFalse(results.exists())

    def test_partial_reasons(self):
        cfg = rp.make_config(rp.parse_args(["--project", "widgets", "--repo-path", str(self.repo)]))
        self.assertEqual(rp.partial_reasons(cfg), [])
        self.assertEqual(cfg.out_dir, rp.RESULTS_DIR / "widgets")
        self.assertFalse(cfg.out_explicit)
        for extra in (["--only", "x"], ["--threshold", "1"], ["--cases", str(self.cases_dir)]):
            with self.subTest(extra=extra):
                cfg = rp.make_config(rp.parse_args(["--project", "widgets", "--repo-path", str(self.repo)] + extra))
                self.assertEqual(len(rp.partial_reasons(cfg)), 1)
        cfg = rp.make_config(rp.parse_args(["--project", "widgets", "--repo-path", str(self.repo), "--threshold", "9"]))
        self.assertEqual(rp.partial_reasons(cfg), [])

    def test_keep_worktrees(self):
        runner = FakeRunner().on(is_review, lambda c, cwd: ok(envelope(structured=self.rr([]))))
        code, out, _ = self.main(runner, "--only", "b-range", "--keep-worktrees")
        self.assertEqual(code, 1)
        kept = re.search(r"kept worktree for b-range: (\S+)", out).group(1)
        self.assertTrue(Path(kept, "app.py").exists())
        rp.remove_worktree(FakeRunner(), self.repo, Path(kept).parent, Path(kept))

    def test_dry_run_calls_no_model(self):
        runner = self.runner([], [])
        code, out, _ = self.main(runner, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(runner.claude_calls(), [])
        self.assertFalse(self.out_dir.exists())
        self.assertIn("/factory:review-pr {} --json".format(self.r["c0"]), out)
        self.assertIn('--json-schema "$REVIEW_SCHEMA"', out)
        self.assertNotIn("--comment", out)
        self.assert_no_worktrees_left()

    def test_invalid_config_exits_2(self):
        write(self.cases_dir / "bad.json", json.dumps({"id": "bad"}))
        code, _, err = self.main(self.runner([], []))
        self.assertEqual(code, 2)
        self.assertIn("bad.json", err)

    def test_unknown_only_id_exits_2(self):
        code, _, err = self.main(self.runner([], []), "--only", "nope")
        self.assertEqual(code, 2)
        self.assertIn("nope", err)

    def test_missing_plugin_or_bad_ref_exits_2(self):
        code, _, err = self.main(self.runner([], []), "--plugin-dir", str(self.tmp / "missing"))
        self.assertEqual(code, 2)
        self.assertIn("plugin.json", err)
        code, _, err = self.main(self.runner([], []), "--standards-ref", "no-such-ref")
        self.assertEqual(code, 2)
        self.assertIn("no-such-ref", err)


if __name__ == "__main__":
    unittest.main()
