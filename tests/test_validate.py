"""Tests for tools/validate.py. Every test builds a synthetic repo in a temp dir."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PY = REPO_ROOT / "tools" / "validate.py"

_spec = importlib.util.spec_from_file_location("factory_validate", VALIDATE_PY)
validate_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(validate_mod)

try:
    import jsonschema  # type: ignore  # noqa: F401

    HAVE_JSONSCHEMA = hasattr(jsonschema, "Draft202012Validator")
except ImportError:
    HAVE_JSONSCHEMA = False

GOOD_CASE: Dict[str, Any] = {
    "id": "null-cart-total",
    "repo": "acme/widgets",
    "pr": 101,
    "known_problem": {
        "summary": "Cart total crashes when the cart has no items because the sum starts from None.",
        "files": ["src/cart.py"],
        "severity": "Important",
    },
    "source": "bug-reached-users",
    "reference": "WID-123",
    "added": "2026-11-01",
}

GOOD_FINDING: Dict[str, Any] = {
    "id": "F1",
    "severity": "Important",
    "file": "src/cart.py",
    "line": 12,
    "title": "Sum starts from None",
    "body": "An empty cart raises TypeError. Start the sum from 0.",
}

GOOD_RESULT: Dict[str, Any] = {
    "run_id": "20261119T120000Z-abc1234",
    "project": "widgets",
    "started_at": "2026-11-19T12:00:00Z",
    "standards": {
        "repo_path": "/tmp/widgets",
        "standards_ref": "deadbeef",
        "claude_md_sha": "abc",
        "review_md_sha": None,
    },
    "model": None,
    "threshold": 1,
    "score": {"found": 1, "total": 1, "errors": 0},
    "passed": True,
    "cases": [
        {
            "case_id": "null-cart-total",
            "base": "aaa",
            "head": "bbb",
            "found": True,
            "matched_finding_ids": ["F1"],
            "findings": [GOOD_FINDING],
        }
    ],
}

GOOD_SKILL = """---
name: review-pr
description: Review a pull request against CLAUDE.md and REVIEW.md.
argument-hint: "[pr-number]"
allowed-tools:
  - Bash(gh pr diff *)
---

Review the change.
"""


class RepoBuilder:
    """A minimal valid onion-factory layout in a temp dir, using the repo's real schemas."""

    def __init__(self, root: Path) -> None:
        self.root = root
        shutil.copytree(REPO_ROOT / "schemas", root / "schemas")
        self.write_json(".claude-plugin/marketplace.json", {
            "name": "onion-factory",
            "description": "Test marketplace",
            "owner": {"name": "Test Owner"},
            "plugins": [{"name": "factory", "source": "./plugins/factory", "description": "x"}],
        })
        self.write_json("plugins/factory/.claude-plugin/plugin.json", {
            "name": "factory", "version": "0.1.0", "description": "x", "author": {"name": "Test Owner"},
        })
        self.write_text("plugins/factory/skills/review-pr/SKILL.md", GOOD_SKILL)
        self.write_json("evals/review/cases/widgets/null-cart-total.json", GOOD_CASE)
        self.write_json("evals/review/results/widgets/20261119T120000Z-abc1234.json", GOOD_RESULT)

    def write_text(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, rel: str, data: Any) -> Path:
        return self.write_text(rel, json.dumps(data, indent=2))

    def read_json(self, rel: str) -> Any:
        return json.loads((self.root / rel).read_text(encoding="utf-8"))


class ValidateTestBase(unittest.TestCase):
    use_jsonschema = False

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = RepoBuilder(Path(self._tmp.name))

    def run_validate(self) -> List[str]:
        report, mode = validate_mod.validate(self.repo.root, use_jsonschema=self.use_jsonschema)
        expected = "jsonschema" if self.use_jsonschema else "fallback"
        self.assertTrue(mode.startswith(expected), mode)
        return report.errors

    def assertValid(self) -> None:
        errors = self.run_validate()
        self.assertEqual(errors, [], "\n".join(errors))

    def assertInvalid(self, *fragments: str) -> List[str]:
        errors = self.run_validate()
        self.assertTrue(errors, "expected errors, got none")
        joined = "\n".join(errors)
        for fragment in fragments:
            self.assertIn(fragment, joined)
        return errors


class DataChecks:
    """Checks that must behave the same in both modes. Mixed into the concrete test classes."""

    # -- good inputs ---------------------------------------------------------------------
    def test_minimal_repo_is_valid(self) -> None:
        self.assertValid()

    def test_case_with_base_and_head_is_valid(self) -> None:
        case = copy.deepcopy(GOOD_CASE)
        del case["pr"]
        case.update({"id": "range-case", "base": "aaa", "head": "bbb"})
        self.repo.write_json("evals/review/cases/widgets/range-case.json", case)
        self.assertValid()

    def test_missing_eval_folders_are_fine(self) -> None:
        shutil.rmtree(self.repo.root / "evals")
        self.assertValid()

    # -- schemas -------------------------------------------------------------------------
    def test_schema_with_invalid_json(self) -> None:
        self.repo.write_text("schemas/broken.schema.json", "{ not json")
        self.assertInvalid("schemas/broken.schema.json", "invalid JSON")

    def test_schema_with_wrong_draft(self) -> None:
        self.repo.write_json("schemas/old.schema.json", {
            "$schema": "http://json-schema.org/draft-07/schema#", "type": "object"})
        self.assertInvalid("schemas/old.schema.json", "$schema must be")

    def test_schema_with_invalid_keyword_value(self) -> None:
        self.repo.write_json("schemas/bad.schema.json", {"$schema": validate_mod.DRAFT_2020_12, "required": "id"})
        self.assertInvalid("schemas/bad.schema.json", "not a valid Draft 2020-12 schema")

    def test_schema_with_dangling_ref(self) -> None:
        self.repo.write_json("schemas/dangling.schema.json", {
            "$schema": validate_mod.DRAFT_2020_12, "$ref": "missing.schema.json"})
        self.assertInvalid("schemas/dangling.schema.json", "does not resolve")

    def test_column_order_must_match_properties(self) -> None:
        rel = "schemas/review-log-row.schema.json"
        schema = self.repo.read_json(rel)
        schema["x-column-order"] = list(reversed(schema["x-column-order"]))
        self.repo.write_json(rel, schema)
        self.assertInvalid(rel, "x-column-order")

    def test_no_schemas_at_all(self) -> None:
        shutil.rmtree(self.repo.root / "schemas")
        self.assertInvalid("no schemas found")

    # -- eval cases ----------------------------------------------------------------------
    def test_case_id_must_match_file_name(self) -> None:
        self.repo.write_json("evals/review/cases/widgets/other-name.json", GOOD_CASE)
        self.assertInvalid("other-name.json", "file name")

    def test_case_missing_required_field(self) -> None:
        case = copy.deepcopy(GOOD_CASE)
        del case["known_problem"]
        self.repo.write_json("evals/review/cases/widgets/null-cart-total.json", case)
        self.assertInvalid("null-cart-total.json", "known_problem")

    def test_case_bad_source_enum(self) -> None:
        case = copy.deepcopy(GOOD_CASE)
        case["source"] = "rumour"
        self.repo.write_json("evals/review/cases/widgets/null-cart-total.json", case)
        self.assertInvalid("null-cart-total.json", "rumour")

    def test_case_with_both_pr_and_range_fails_one_of(self) -> None:
        case = copy.deepcopy(GOOD_CASE)
        case.update({"base": "aaa", "head": "bbb"})
        self.repo.write_json("evals/review/cases/widgets/null-cart-total.json", case)
        self.assertInvalid("null-cart-total.json")

    def test_case_with_unknown_property(self) -> None:
        case = copy.deepcopy(GOOD_CASE)
        case["notes"] = "extra"
        self.repo.write_json("evals/review/cases/widgets/null-cart-total.json", case)
        self.assertInvalid("null-cart-total.json", "notes")

    def test_case_with_invalid_json(self) -> None:
        self.repo.write_text("evals/review/cases/widgets/null-cart-total.json", "{")
        self.assertInvalid("null-cart-total.json", "invalid JSON")

    # -- eval results --------------------------------------------------------------------
    def test_result_with_bad_finding_severity_via_ref(self) -> None:
        rel = "evals/review/results/widgets/20261119T120000Z-abc1234.json"
        result = copy.deepcopy(GOOD_RESULT)
        result["cases"][0]["findings"][0]["severity"] = "Minor"
        self.repo.write_json(rel, result)
        self.assertInvalid("20261119T120000Z-abc1234.json", "Minor")

    def test_result_with_bad_finding_id_via_ref(self) -> None:
        rel = "evals/review/results/widgets/20261119T120000Z-abc1234.json"
        result = copy.deepcopy(GOOD_RESULT)
        result["cases"][0]["findings"][0]["id"] = "finding-1"
        self.repo.write_json(rel, result)
        self.assertInvalid("20261119T120000Z-abc1234.json", "finding-1")

    def test_result_passed_must_match_score(self) -> None:
        rel = "evals/review/results/widgets/20261119T120000Z-abc1234.json"
        result = copy.deepcopy(GOOD_RESULT)
        result["threshold"] = 8
        self.repo.write_json(rel, result)
        self.assertInvalid("20261119T120000Z-abc1234.json", "\"passed\" is true")

    def test_result_missing_required_field(self) -> None:
        rel = "evals/review/results/widgets/20261119T120000Z-abc1234.json"
        result = copy.deepcopy(GOOD_RESULT)
        del result["score"]
        self.repo.write_json(rel, result)
        self.assertInvalid("20261119T120000Z-abc1234.json", "score")


class FallbackModeTest(DataChecks, ValidateTestBase):
    use_jsonschema = False

    def _warnings(self) -> List[str]:
        report, _ = validate_mod.validate(self.repo.root, use_jsonschema=False)
        return report.warnings

    def test_repo_schemas_use_only_checked_keywords(self) -> None:
        self.assertEqual([w for w in self._warnings() if "does not check keyword" in w], [])

    def test_unsupported_keyword_is_reported_as_warning(self) -> None:
        self.repo.write_json("schemas/extra.schema.json", {
            "$schema": validate_mod.DRAFT_2020_12,
            "type": "object",
            "properties": {
                # A property NAMED like a keyword is not a keyword.
                "not": {"type": "string"},
                "tags": {"type": "array", "uniqueItems": True, "items": {"not": {"const": ""}}},
            },
            "x-note": "extension keywords are ignored",
        })
        self.assertValid()  # warnings only
        joined = "\n".join(self._warnings())
        self.assertIn("schemas/extra.schema.json", joined)
        self.assertIn("$.properties.tags: 'uniqueItems'", joined)
        self.assertIn("$.properties.tags.items: 'not'", joined)
        self.assertNotIn("$.properties: 'not'", joined)
        self.assertNotIn("x-note", joined)


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema with Draft 2020-12 support is not installed")
class JsonschemaModeTest(DataChecks, ValidateTestBase):
    use_jsonschema = True

    def test_format_is_checked(self) -> None:
        case = copy.deepcopy(GOOD_CASE)
        case["added"] = "19 Nov 2026"
        self.repo.write_json("evals/review/cases/widgets/null-cart-total.json", case)
        self.assertInvalid("null-cart-total.json", "19 Nov 2026")


class PluginLayoutTest(ValidateTestBase):
    use_jsonschema = False

    def _edit_marketplace(self, **changes: Any) -> None:
        data = self.repo.read_json(".claude-plugin/marketplace.json")
        data.update(changes)
        self.repo.write_json(".claude-plugin/marketplace.json", data)

    def _edit_plugin(self, **changes: Any) -> None:
        rel = "plugins/factory/.claude-plugin/plugin.json"
        data = self.repo.read_json(rel)
        data.update(changes)
        self.repo.write_json(rel, data)

    def test_marketplace_missing(self) -> None:
        (self.repo.root / ".claude-plugin" / "marketplace.json").unlink()
        self.assertInvalid("marketplace.json", "missing")

    def test_marketplace_missing_owner(self) -> None:
        data = self.repo.read_json(".claude-plugin/marketplace.json")
        del data["owner"]
        self.repo.write_json(".claude-plugin/marketplace.json", data)
        self.assertInvalid("owner")

    def test_marketplace_bad_name(self) -> None:
        self._edit_marketplace(name="onion factory")
        self.assertInvalid("may use only")

    def test_marketplace_source_must_exist(self) -> None:
        self._edit_marketplace(plugins=[{"name": "factory", "source": "./plugins/nope"}])
        self.assertInvalid("./plugins/nope", "does not exist")

    def test_marketplace_source_must_be_dot_relative(self) -> None:
        self._edit_marketplace(plugins=[{"name": "factory", "source": "plugins/factory"}])
        self.assertInvalid("must start with './'")

    def test_marketplace_source_rejects_parent_dir(self) -> None:
        self._edit_marketplace(plugins=[{"name": "factory", "source": "./plugins/../plugins/factory"}])
        self.assertInvalid("'..'")

    def test_marketplace_entry_needs_source(self) -> None:
        self._edit_marketplace(plugins=[{"name": "factory"}])
        self.assertInvalid("\"source\" is required")

    def test_marketplace_object_source_is_accepted(self) -> None:
        self._edit_marketplace(plugins=[
            {"name": "factory", "source": "./plugins/factory"},
            {"name": "remote", "source": {"source": "github", "repo": "acme/remote-plugin"}},
        ])
        self.assertValid()

    def test_marketplace_bare_name_source_with_plugin_root(self) -> None:
        self._edit_marketplace(
            metadata={"pluginRoot": "./plugins"},
            plugins=[{"name": "factory", "source": "factory"}],
        )
        self.assertValid()

    def test_marketplace_bare_name_source_must_exist_under_plugin_root(self) -> None:
        self._edit_marketplace(
            metadata={"pluginRoot": "./plugins"},
            plugins=[{"name": "factory", "source": "nope"}],
        )
        self.assertInvalid("'nope'", "does not exist")

    def test_marketplace_bare_name_source_checks_manifest_name(self) -> None:
        (self.repo.root / "plugins/fact").mkdir()
        self.repo.write_json("plugins/fact/.claude-plugin/plugin.json", {"name": "other"})
        self._edit_marketplace(
            metadata={"pluginRoot": "./plugins"},
            plugins=[{"name": "fact", "source": "fact"}],
        )
        self.assertInvalid("differs from plugin.json name")

    def test_marketplace_bare_name_source_needs_plugin_root(self) -> None:
        self._edit_marketplace(plugins=[{"name": "factory", "source": "factory"}])
        self.assertInvalid("must start with './'", "metadata.pluginRoot")

    def test_marketplace_plugin_root_rejects_parent_dir(self) -> None:
        self._edit_marketplace(metadata={"pluginRoot": "../elsewhere"})
        self.assertInvalid("metadata.pluginRoot", "'..'")

    def test_marketplace_entry_name_must_match_manifest(self) -> None:
        self._edit_marketplace(plugins=[{"name": "fact", "source": "./plugins/factory"}])
        self.assertInvalid("differs from plugin.json name")

    def test_marketplace_without_description_only_warns(self) -> None:
        data = self.repo.read_json(".claude-plugin/marketplace.json")
        del data["description"]
        self.repo.write_json(".claude-plugin/marketplace.json", data)
        report, _ = validate_mod.validate(self.repo.root, use_jsonschema=False)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("description" in w for w in report.warnings))

    def test_plugin_manifest_missing(self) -> None:
        (self.repo.root / "plugins/factory/.claude-plugin/plugin.json").unlink()
        self.assertInvalid("plugin.json", "missing plugin manifest")

    def test_plugin_name_must_be_kebab_case(self) -> None:
        self._edit_plugin(name="Factory:Tools")
        self.assertInvalid("kebab-case")

    def test_plugin_author_needs_name(self) -> None:
        self._edit_plugin(author="Test Owner")
        self.assertInvalid("\"author\"")

    def test_plugin_manifest_paths_stay_inside(self) -> None:
        self._edit_plugin(skills=["../elsewhere"])
        self.assertInvalid("\"skills\" path")

    def test_skills_inside_claude_plugin_dir(self) -> None:
        self.repo.write_text("plugins/factory/.claude-plugin/skills/x/SKILL.md", GOOD_SKILL)
        self.assertInvalid("belongs at the plugin root")

    def test_skill_without_frontmatter(self) -> None:
        self.repo.write_text("plugins/factory/skills/review-pr/SKILL.md", "# Review\n")
        self.assertInvalid("SKILL.md", "no YAML frontmatter")

    def test_skill_without_description(self) -> None:
        self.repo.write_text("plugins/factory/skills/review-pr/SKILL.md", "---\nname: review-pr\n---\nBody\n")
        self.assertInvalid("description")

    def test_skill_name_must_match_folder(self) -> None:
        self.repo.write_text(
            "plugins/factory/skills/review-pr/SKILL.md",
            GOOD_SKILL.replace("name: review-pr", "name: review"),
        )
        self.assertInvalid("differs from its folder")

    def test_skill_folder_without_skill_md(self) -> None:
        (self.repo.root / "plugins/factory/skills/log-review").mkdir(parents=True)
        self.assertInvalid("log-review/SKILL.md", "missing SKILL.md")

    def test_skill_with_folded_description(self) -> None:
        self.repo.write_text(
            "plugins/factory/skills/review-pr/SKILL.md",
            "---\nname: 'review-pr'\ndescription: >\n  Review a pull request\n  against REVIEW.md.\n---\nBody\n",
        )
        self.assertValid()


class FrontmatterParserTest(unittest.TestCase):
    def test_parses_scalars_quotes_and_blocks(self) -> None:
        fields = validate_mod.parse_frontmatter(
            '---\nname: "log-review"\ndescription: Log one row: tab separated.\n'
            "when_to_use: |\n  After merge.\n  Once per PR.\n# comment\n---\nbody\n"
        )
        self.assertEqual(fields["name"], "log-review")
        self.assertEqual(fields["description"], "Log one row: tab separated.")
        self.assertEqual(fields["when_to_use"], "After merge. Once per PR.")

    def test_unterminated_frontmatter(self) -> None:
        self.assertIsNone(validate_mod.parse_frontmatter("---\nname: x\n"))

    def test_no_frontmatter(self) -> None:
        self.assertIsNone(validate_mod.parse_frontmatter("name: x\n"))


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = RepoBuilder(Path(self._tmp.name))

    def _run(self, *args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, str(VALIDATE_PY), "--root", str(self.repo.root), *args],
            capture_output=True, text=True, check=False,
        )

    def test_exit_zero_and_mode_line_on_valid_repo(self) -> None:
        proc = self._run("--no-jsonschema")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("mode=fallback", proc.stdout)
        self.assertIn("OK", proc.stdout)

    def test_exit_one_with_message_on_error(self) -> None:
        self.repo.write_json("evals/review/cases/widgets/wrong.json", GOOD_CASE)
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("ERROR   evals/review/cases/widgets/wrong.json", proc.stdout)
        self.assertIn("FAILED", proc.stdout)

    def test_exit_two_on_missing_root(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            code = validate_mod.main(["--root", str(self.repo.root / "nope")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
