#!/usr/bin/env python3
"""Run a project's review eval set and score it against the Stage 1 gate.

For every case in evals/review/cases/<project>/ the runner:

1. resolves the case to a base and head commit (a merged PR resolves to
   base = first parent of its merge commit, head = the merge commit);
2. checks the head out into a temporary detached git worktree;
3. lays today's standards (CLAUDE.md, REVIEW.md, .claude/rules/ from
   --standards-ref) over that worktree, so old changes are judged by the
   current REVIEW.md;
4. runs ``/factory:review-pr <base> --json`` headless with the claude CLI
   (read-only tools, never posts to GitHub);
5. asks a second headless claude call, with no tools, whether any finding is
   the known problem;
6. removes the worktree.

A full run is written to evals/review/results/<project>/<run_id>.json (see
schemas/eval-result.schema.json); a partial run (--only, a non-default --cases
folder, or --threshold below 8) goes to a temp folder unless --out names
another place outside results/. The exit code is 0 on PASS, 1 on FAIL, 2 when
the configuration is invalid or any case errored (including a review whose
base/head are not the case's commits, which is how the skill reports a fatal
error).

Every real run spends Claude tokens: two claude calls per case (one when the
review reports no findings). Use --dry-run to check the set for free.

Python 3.9, standard library only. ``jsonschema`` is used when importable;
otherwise a small built-in validator covers the keywords our schemas use.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
SCHEMA_DIR = REPO_ROOT / "schemas"
DEFAULT_PLUGIN_DIR = REPO_ROOT / "plugins" / "factory"
# Full runs only: the scorecard reads the newest file under results/<project>/
# as the catch-rate gate result, so partial or debug runs never go here.
RESULTS_DIR = SCRIPT_DIR / "results"
# Default output folder for partial runs (--only, or --threshold below the
# default) when --out is not given. Outside the repo on purpose.
PARTIAL_RESULTS_DIR = Path(tempfile.gettempdir()) / "factory-eval-partial"
DEFAULT_THRESHOLD = 8

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

# Files laid over each case's worktree from --standards-ref.
STANDARDS_FILES = ("CLAUDE.md", "REVIEW.md")
STANDARDS_DIRS = (".claude/rules",)
STANDARDS_PATHS = STANDARDS_FILES + STANDARDS_DIRS

GIT_TIMEOUT = 300
GH_TIMEOUT = 120


# ---------------------------------------------------------------------------
# claude CLI invocation. Every claude flag the runner uses is in this section.
# Flags were checked against `claude --help` of Claude Code 2.1.319 and the
# headless docs (https://code.claude.com/docs/en/headless.md).
# ---------------------------------------------------------------------------

CLAUDE_BIN = "claude"

# The review-pr skill's base-ref mode: diffs `git merge-base <base> HEAD`..HEAD
# (committed changes only) and, with --json, ends with one review-result
# object. Never add --comment here: eval runs must not post to GitHub.
REVIEW_PROMPT_TEMPLATE = "/factory:review-pr {base} --json"

# Pre-approved for the review call (permission rule syntax). Everything else
# is refused because the call runs with --permission-mode dontAsk.
REVIEW_ALLOWED_TOOLS = (
    "Read",
    "Grep",
    "Glob",
    "Bash(git rev-parse *)",
    "Bash(git branch --show-current)",
    "Bash(git symbolic-ref --short refs/remotes/origin/HEAD)",
    "Bash(git remote get-url *)",
    "Bash(git merge-base *)",
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(git show *)",
    "Bash(git grep *)",
    "Bash(git ls-files *)",
    "Bash(git cat-file *)",
    "Bash(git status *)",
)

# Denied even if the skill's allowed-tools, the user's settings or anything
# else would allow them (deny rules win over allow rules). Base-ref mode needs
# no gh call (the repo name falls back to `git remote get-url origin`), so all
# of gh is denied rather than a list of its write subcommands.
#
# What this enforces: the review call cannot edit files, fetch the web, run
# `gh`, `curl` or `wget`, or run the git commands below; no MCP server is
# loaded (--strict-mcp-config with no --mcp-config); and only user settings
# are read (REVIEW_SETTING_SOURCES), so the case head's .claude/settings.json
# (its allow rules and hooks) does not apply. What it does not cover: a
# user-level allow rule for some other command that can reach GitHub (for
# example a wrapper script), and hooks in the user's own settings.
REVIEW_DISALLOWED_TOOLS = (
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Bash(gh *)",
    "Bash(curl *)",
    "Bash(wget *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git checkout *)",
    "Bash(git switch *)",
    "Bash(git reset *)",
    "Bash(git restore *)",
    "Bash(git update-index *)",
)

# --setting-sources for the review call: user settings only, never the
# project or local settings of the (old) commit under review.
REVIEW_SETTING_SOURCES = "user"

# --json-schema makes the CLI validate the final answer and return it as
# `structured_output` in the JSON envelope (docs: headless.md). When the field
# is missing the runner falls back to parsing the `result` text, so turning
# this off only loses the CLI-side validation.
USE_JSON_SCHEMA = True

# Not used: --max-turns is documented in the CLI reference but absent from the
# 2.1.319 --help text, so the runner bounds each call with --timeout instead.


def build_review_cmd(base: str, plugin_dir: Path, model: Optional[str], schema_json: Optional[str]) -> List[str]:
    """The headless review call. Runs with cwd = the case worktree."""
    cmd = [
        CLAUDE_BIN,
        "-p",
        REVIEW_PROMPT_TEMPLATE.format(base=base),
        "--plugin-dir",
        str(plugin_dir),
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ",".join(REVIEW_ALLOWED_TOOLS),
        "--disallowedTools",
        ",".join(REVIEW_DISALLOWED_TOOLS),
        "--setting-sources",
        REVIEW_SETTING_SOURCES,
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if USE_JSON_SCHEMA and schema_json:
        cmd += ["--json-schema", schema_json]
    if model:
        cmd += ["--model", model]
    return cmd


def build_judge_cmd(prompt: str, model: Optional[str], schema_json: Optional[str]) -> List[str]:
    """The judge call: no tools at all (`--tools ""`), strict JSON answer."""
    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--tools",
        "",
        "--strict-mcp-config",
        "--output-format",
        "json",
        "--no-session-persistence",
    ]
    if USE_JSON_SCHEMA and schema_json:
        cmd += ["--json-schema", schema_json]
    if model:
        cmd += ["--model", model]
    return cmd


JUDGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["found", "matched_finding_ids", "reasoning"],
    "properties": {
        "found": {"type": "boolean"},
        "matched_finding_ids": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
}

JUDGE_PROMPT_TEMPLATE = """You are grading an AI code reviewer against a problem the team already knows was in this change.

Known problem (case {case_id}, repo {repo}):
- Summary: {summary}
- Files: {files}
- Expected severity: {severity}

Findings the reviewer reported (JSON):
{findings_json}

Decide whether any finding identifies the known problem.
- A finding matches when it describes the same underlying defect: the same wrong behaviour or root cause. Different wording, a nearby line, or a different suggested fix still match.
- A finding about a different issue does not match, even in the same file or function.
- A vague finding ("consider validating input", "might be null") matches only if it names the specific failure of the known problem.
- Ignore severity when deciding the match.
- Judge only from the text above. Do not use tools.

Reply with ONLY one JSON object, no prose and no code fences:
{{"found": true or false, "matched_finding_ids": ["F1", ...], "reasoning": "one or two sentences"}}
matched_finding_ids lists every matching finding id and is empty when found is false."""


# ---------------------------------------------------------------------------
# Subprocess seam. Every external command goes through a Runner, so tests can
# inject a fake one.
# ---------------------------------------------------------------------------


class CommandResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


# runner(cmd, cwd=None, timeout=None) -> CommandResult. May raise
# subprocess.TimeoutExpired or OSError.
Runner = Callable[..., CommandResult]


def subprocess_runner(cmd: Sequence[str], cwd: Optional[str] = None, timeout: Optional[float] = None) -> CommandResult:
    proc = subprocess.run(
        list(cmd),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return CommandResult(
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


class EvalError(Exception):
    """A problem that fails one case (or the configuration), never the process."""


class CommandError(EvalError):
    pass


def describe_cmd(cmd: Sequence[str], limit: int = 160) -> str:
    text = shlex.join(list(cmd))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def run_checked(
    runner: Runner,
    cmd: Sequence[str],
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """Run a command and return stdout; raise CommandError on any failure."""
    try:
        res = runner(list(cmd), cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CommandError("timed out after {}s: {}".format(timeout, describe_cmd(cmd)))
    except OSError as exc:
        raise CommandError("could not run {}: {}".format(cmd[0], exc))
    if res.returncode != 0:
        raise CommandError(
            "exit {} from {}: {}".format(res.returncode, describe_cmd(cmd), _tail(res.stderr or res.stdout))
        )
    return res.stdout


def git(runner: Runner, repo: Path, *args: str, timeout: float = GIT_TIMEOUT) -> str:
    return run_checked(runner, ["git", "-C", str(repo)] + list(args), timeout=timeout).strip()


def git_ok(runner: Runner, repo: Path, *args: str) -> bool:
    try:
        res = runner(["git", "-C", str(repo)] + list(args), cwd=None, timeout=GIT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return res.returncode == 0


# ---------------------------------------------------------------------------
# JSON Schema validation: jsonschema when available, else a built-in subset.
# ---------------------------------------------------------------------------


def load_schema(name: str, schema_dir: Path = SCHEMA_DIR) -> Dict[str, Any]:
    """Load a schema with every relative "$ref": "x.schema.json" inlined."""

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.endswith(".json") and "#" not in ref:
                target = inline(json.loads((schema_dir / ref).read_text(encoding="utf-8")))
                target.pop("$schema", None)
                target.pop("$id", None)
                return target
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(json.loads((schema_dir / name).read_text(encoding="utf-8")))


def schema_for_cli(schema: Dict[str, Any]) -> str:
    """Compact JSON for --json-schema, without $schema/$id/x- annotations."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: strip(v)
                for k, v in node.items()
                if k not in ("$schema", "$id") and not k.startswith("x-")
            }
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return json.dumps(strip(schema), separators=(",", ":"))


_TYPE_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def builtin_validate(instance: Any, schema: Dict[str, Any], path: str = "$") -> List[str]:
    """Validate against the JSON Schema subset our schemas use.

    Supported: type, enum, const, required, properties, additionalProperties,
    items, pattern, minLength, maxLength, minimum, oneOf. "format" is an
    annotation and is not checked (as in jsonschema's default).
    """
    errors: List[str] = []
    if "$ref" in schema:
        raise ValueError("unresolved $ref at {}; load schemas with load_schema()".format(path))

    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if not builtin_validate(instance, sub, path))
        if matches != 1:
            errors.append("{}: must match exactly one of {} (matched {})".format(path, json.dumps(schema["oneOf"]), matches))

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPE_CHECKS[t](instance) for t in types):
            errors.append("{}: expected {}, got {}".format(path, " or ".join(types), _json_type(instance)))
            return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append("{}: {!r} is not one of {}".format(path, instance, schema["enum"]))
    if "const" in schema and instance != schema["const"]:
        errors.append("{}: must be {!r}".format(path, schema["const"]))

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("{}: shorter than {} characters".format(path, schema["minLength"]))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append("{}: longer than {} characters".format(path, schema["maxLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append("{}: {!r} does not match {}".format(path, instance, schema["pattern"]))

    if _TYPE_CHECKS["number"](instance) and "minimum" in schema and instance < schema["minimum"]:
        errors.append("{}: {} is less than {}".format(path, instance, schema["minimum"]))

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append("{}: missing required property {!r}".format(path, key))
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            sub_path = "{}.{}".format(path, key)
            if key in props:
                errors.extend(builtin_validate(value, props[key], sub_path))
            elif extra is False:
                errors.append("{}: unexpected property {!r}".format(path, key))
            elif isinstance(extra, dict):
                errors.extend(builtin_validate(value, extra, sub_path))

    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(instance):
            errors.extend(builtin_validate(item, schema["items"], "{}[{}]".format(path, i)))

    return errors


def _json_type(value: Any) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _TYPE_CHECKS[name](value):
            return name
    return type(value).__name__


def validate(instance: Any, schema: Dict[str, Any]) -> List[str]:
    """Return a list of error messages (empty when valid)."""
    try:
        import jsonschema  # type: ignore

        validator_cls = jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema)
    except Exception:  # not installed, or too old for draft 2020-12
        return builtin_validate(instance, schema)
    out = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        loc = "$" + "".join("[{}]".format(p) if isinstance(p, int) else ".{}".format(p) for p in err.absolute_path)
        out.append("{}: {}".format(loc, err.message))
    return out


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def validate_case(case: Any, file_stem: Optional[str], schema: Dict[str, Any]) -> List[str]:
    errors = validate(case, schema)
    if file_stem is not None and isinstance(case, dict) and case.get("id") != file_stem:
        errors.append("$.id: {!r} must equal the file name {!r}".format(case.get("id"), file_stem + ".json"))
    return errors


def load_cases(cases_dir: Path, schema: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Load every <id>.json in cases_dir. Returns (cases, errors)."""
    cases: List[Dict[str, Any]] = []
    errors: List[str] = []
    if not cases_dir.is_dir():
        return cases, ["cases directory not found: {}".format(cases_dir)]
    for path in sorted(cases_dir.glob("*.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append("{}: not valid JSON: {}".format(path.name, exc))
            continue
        problems = validate_case(case, path.stem, schema)
        if problems:
            errors.extend("{}: {}".format(path.name, p) for p in problems)
        else:
            cases.append(case)
    if not cases and not errors:
        errors.append("no cases (*.json) in {}".format(cases_dir))
    return cases, errors


# ---------------------------------------------------------------------------
# Commits, worktrees, standards
# ---------------------------------------------------------------------------


def ensure_commit(runner: Runner, repo: Path, rev: str) -> str:
    """Return the full SHA of rev, fetching it from origin when missing."""
    if not git_ok(runner, repo, "cat-file", "-e", rev + "^{commit}"):
        try:
            git(runner, repo, "fetch", "--quiet", "origin", rev)
        except CommandError as exc:
            raise EvalError("commit {} not found locally and fetch failed: {}".format(rev, exc))
        if not git_ok(runner, repo, "cat-file", "-e", rev + "^{commit}"):
            raise EvalError("commit {} still missing after fetching from origin (use a full SHA)".format(rev))
    return git(runner, repo, "rev-parse", "--verify", rev + "^{commit}")


def resolve_case_commits(runner: Runner, case: Dict[str, Any], repo: Path) -> Tuple[str, str]:
    """Return (base, head) full SHAs for a case."""
    if "pr" in case:
        cmd = [
            "gh", "pr", "view", str(case["pr"]),
            "-R", case["repo"],
            "--json", "number,state,mergeCommit,baseRefName,headRefOid",
        ]
        out = run_checked(runner, cmd, timeout=GH_TIMEOUT)
        try:
            data = json.loads(out)
        except ValueError:
            raise EvalError("gh pr view returned non-JSON output: {}".format(_tail(out, 200)))
        if data.get("state") != "MERGED":
            raise EvalError(
                "PR {}#{} is {}, not MERGED; use base/head for this case".format(case["repo"], case["pr"], data.get("state"))
            )
        oid = (data.get("mergeCommit") or {}).get("oid")
        if not oid:
            raise EvalError("PR {}#{} has no merge commit; use base/head for this case".format(case["repo"], case["pr"]))
        head = ensure_commit(runner, repo, oid)
        try:
            base = git(runner, repo, "rev-parse", "--verify", head + "^1^{commit}")
        except CommandError:
            raise EvalError("first parent of merge commit {} is missing (shallow clone? run git fetch --unshallow)".format(head[:12]))
        return base, head
    return ensure_commit(runner, repo, case["base"]), ensure_commit(runner, repo, case["head"])


@dataclasses.dataclass
class Standards:
    ref: str  # as given on the command line
    commit: str  # resolved SHA
    files: Dict[str, str]  # repo-relative path -> blob SHA

    @property
    def claude_md_sha(self) -> Optional[str]:
        return self.files.get("CLAUDE.md")

    @property
    def review_md_sha(self) -> Optional[str]:
        return self.files.get("REVIEW.md")


REGULAR_MODES = ("100644", "100755")
SYMLINK_MODE = "120000"
MAX_LINK_HOPS = 8


class TreeEntry(NamedTuple):
    mode: str
    type: str
    sha: str
    path: str


def ls_tree(runner: Runner, repo: Path, commit: str, paths: Sequence[str], recursive: bool) -> List[TreeEntry]:
    args = ["ls-tree", "-z", "--full-tree"] + (["-r"] if recursive else []) + [commit, "--"] + list(paths)
    out = run_checked(runner, ["git", "-C", str(repo)] + args, timeout=GIT_TIMEOUT)
    entries: List[TreeEntry] = []
    for record in out.split("\0"):
        meta, sep, path = record.partition("\t")
        parts = meta.split()
        if sep and len(parts) == 3:
            entries.append(TreeEntry(parts[0], parts[1], parts[2], path))
    return entries


def _follow_link(
    runner: Runner, repo: Path, commit: str, link_path: str, link_blob: str, dest: str, hops: int = 0
) -> Optional[List[Tuple[str, str]]]:
    """Resolve a symlink stored at link_path in commit.

    Returns [(dest path, blob SHA)] for the regular files it leads to (one for a
    file, every file below it for a directory), or None when the target is
    absolute, outside the repo, missing, a submodule, or too many hops away.
    """
    if hops >= MAX_LINK_HOPS:
        return None
    target = run_checked(runner, ["git", "-C", str(repo), "cat-file", "blob", link_blob], timeout=GIT_TIMEOUT)
    target = target.rstrip("\n")
    if not target or target.startswith("/"):
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(link_path), target))
    if resolved in (".", "..") or resolved.startswith("../"):
        return None
    match = [e for e in ls_tree(runner, repo, commit, [resolved], recursive=False) if e.path == resolved]
    if not match:
        return None
    entry = match[0]
    if entry.type == "blob" and entry.mode in REGULAR_MODES:
        return [(dest, entry.sha)]
    if entry.type == "blob" and entry.mode == SYMLINK_MODE:
        return _follow_link(runner, repo, commit, resolved, entry.sha, dest, hops + 1)
    if entry.type != "tree":
        return None
    out: List[Tuple[str, str]] = []
    for sub in ls_tree(runner, repo, commit, [resolved + "/"], recursive=True):
        if not sub.path.startswith(resolved + "/") or sub.type != "blob":
            continue
        sub_dest = dest + sub.path[len(resolved):]
        if sub.mode in REGULAR_MODES:
            out.append((sub_dest, sub.sha))
        elif sub.mode == SYMLINK_MODE:
            out.extend(_follow_link(runner, repo, commit, sub.path, sub.sha, sub_dest, hops + 1) or [])
    return out


def resolve_standards(runner: Runner, repo: Path, ref: str) -> Standards:
    """Find CLAUDE.md, REVIEW.md and .claude/rules/ at ref.

    A symlink (for example CLAUDE.md -> AGENTS.md) is resolved inside the repo
    at the same commit: the target's content is laid over the link's path and
    the target's blob SHA is recorded. A link that cannot be resolved is left
    out with a warning. Submodules are skipped.
    """
    try:
        commit = git(runner, repo, "rev-parse", "--verify", ref + "^{commit}")
    except CommandError:
        hint = " (run: git -C {} remote set-head origin --auto)".format(repo) if ref == "origin/HEAD" else ""
        raise EvalError("standards ref {!r} not found in {}{}".format(ref, repo, hint))
    files: Dict[str, str] = {}
    for entry in ls_tree(runner, repo, commit, STANDARDS_PATHS, recursive=True):
        if entry.type != "blob":
            continue
        if entry.mode in REGULAR_MODES:
            files[entry.path] = entry.sha
        elif entry.mode == SYMLINK_MODE:
            resolved = _follow_link(runner, repo, commit, entry.path, entry.sha, entry.path)
            if resolved is None:
                warn("{} at {} is a symlink that does not resolve to a file or folder inside the repo; "
                     "it is left out of the standards".format(entry.path, ref))
                continue
            for dest, blob in resolved:
                files.setdefault(dest, blob)
    return Standards(ref=ref, commit=commit, files=files)


def create_worktree(runner: Runner, repo: Path, head: str, case_id: str) -> Tuple[Path, Path]:
    parent = Path(tempfile.mkdtemp(prefix="factory-eval-{}-".format(case_id)))
    worktree = parent / "wt"
    try:
        git(runner, repo, "worktree", "add", "--detach", "--quiet", str(worktree), head)
    except Exception:
        shutil.rmtree(str(parent), ignore_errors=True)
        raise
    return parent, worktree


def remove_worktree(runner: Runner, repo: Path, parent: Path, worktree: Path) -> None:
    try:
        git(runner, repo, "worktree", "remove", "--force", str(worktree))
    except EvalError:
        pass
    shutil.rmtree(str(parent), ignore_errors=True)
    try:
        git(runner, repo, "worktree", "prune")
    except EvalError:
        pass


def overlay_standards(runner: Runner, repo: Path, standards: Standards, worktree: Path) -> None:
    """Replace the worktree's CLAUDE.md, REVIEW.md and .claude/rules/ with the
    standards ref's versions. Files absent at the standards ref are removed, so
    the case sees exactly today's standards. The tracked paths touched are
    marked skip-worktree, so `git diff`/`git status` in the worktree do not show
    the overlay as part of the change."""
    tracked = [p for p in git(runner, worktree, "ls-files", "--", *STANDARDS_PATHS).splitlines() if p]
    for name in STANDARDS_FILES:
        target = worktree / name
        if target.is_symlink() or target.is_file():
            target.unlink()
    for name in STANDARDS_DIRS:
        target = worktree / name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(str(target))
    for rel_path, blob in sorted(standards.files.items()):
        content = run_checked(runner, ["git", "-C", str(repo), "cat-file", "blob", blob], timeout=GIT_TIMEOUT)
        dest = worktree / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(str(dest), "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    if tracked:
        git(runner, worktree, "update-index", "--skip-worktree", "--", *tracked)


# ---------------------------------------------------------------------------
# Parsing claude output
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\n(.*?)```", re.DOTALL)


def extract_json(text: str, predicate: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Dict[str, Any]:
    """Find a JSON object in model output.

    Tries, in order: the whole text, fenced code blocks (last first), then every
    balanced object starting at a '{' (last first). Returns the first dict that
    satisfies predicate. Raises ValueError when none does.
    """
    accept = predicate or (lambda _d: True)
    candidates: List[str] = []
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("empty output")
    candidates.append(stripped)
    candidates.extend(m.group(1).strip() for m in reversed(list(_FENCE_RE.finditer(stripped))))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict) and accept(obj):
            return obj
    decoder = json.JSONDecoder()
    top_level: List[Dict[str, Any]] = []
    nested: List[Dict[str, Any]] = []
    covered_until = -1
    for match in re.finditer(r"\{", stripped):
        try:
            obj, end = decoder.raw_decode(stripped, match.start())
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        is_top = match.start() >= covered_until
        if is_top:
            covered_until = end
        if accept(obj):
            (top_level if is_top else nested).append(obj)
    # The model's answer is normally the last top-level object in the text.
    if top_level:
        return top_level[-1]
    if nested:
        return nested[-1]
    raise ValueError("no JSON object found in output: {}".format(_tail(stripped, 200)))


class CliOutput(NamedTuple):
    structured: Optional[Any]
    text: str
    cost_usd: float


def parse_cli_output(stdout: str) -> CliOutput:
    """Parse `claude -p --output-format json` stdout (one result envelope)."""
    envelope: Any = None
    try:
        envelope = json.loads(stdout)
    except ValueError:
        for line in reversed((stdout or "").strip().splitlines()):
            try:
                envelope = json.loads(line)
                break
            except ValueError:
                continue
    if isinstance(envelope, list):
        results = [e for e in envelope if isinstance(e, dict) and e.get("type") == "result"]
        envelope = results[-1] if results else None
    if not isinstance(envelope, dict):
        raise EvalError("claude did not return a JSON envelope: {}".format(_tail(stdout, 200)))
    text = envelope.get("result") if isinstance(envelope.get("result"), str) else ""
    subtype = envelope.get("subtype")
    if envelope.get("is_error") or (subtype not in (None, "success")):
        raise EvalError("claude run failed ({}): {}".format(subtype or "is_error", _tail(text, 300)))
    cost = envelope.get("total_cost_usd")
    return CliOutput(
        structured=envelope.get("structured_output"),
        text=text,
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0,
    )


def extract_review_result(cli: CliOutput, review_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return the review-result object; findings are validated strictly."""
    def is_review(d: Dict[str, Any]) -> bool:
        return isinstance(d.get("findings"), list)

    if isinstance(cli.structured, dict) and is_review(cli.structured):
        review = cli.structured
    else:
        try:
            review = extract_json(cli.text, is_review)
        except ValueError as exc:
            raise EvalError("could not read the review result: {}".format(exc))
    finding_schema = review_schema["properties"]["findings"]["items"]
    problems: List[str] = []
    for i, finding in enumerate(review["findings"]):
        if isinstance(finding, dict) and isinstance(finding.get("title"), str) and len(finding["title"]) > 120:
            finding["title"] = finding["title"][:117] + "..."
        problems.extend("findings[{}]: {}".format(i, e) for e in validate(finding, finding_schema))
    if problems:
        raise EvalError("review findings do not match finding.schema.json: " + "; ".join(problems[:5]))
    rest = validate(review, review_schema)
    if rest:
        warn("review result does not fully match review-result.schema.json (findings are valid, continuing): "
             + "; ".join(rest[:3]))
    return review


_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def _sha_matches(value: Any, full_shas: Iterable[str]) -> bool:
    """True when value is a full or abbreviated (7+ chars) form of one of full_shas."""
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    if not _SHA_RE.fullmatch(text):
        return False
    return any(sha.lower().startswith(text) for sha in full_shas)


def check_review_target(runner: Runner, repo: Path, review: Dict[str, Any], base: str, head: str) -> None:
    """Raise EvalError unless the review ran on this case's commits.

    The review-pr skill reports a fatal error (bad ref, a git command refused,
    ...) as a schema-valid object with empty findings, the error in `summary`
    and `base`/`head` set to the ref text or `unknown`. Without this check such
    a run would score as "not found" instead of as an error.

    Accepted: head == the case head; base == the case base, or
    `git merge-base <base> <head>` (what base-ref mode diffs from). Abbreviated
    SHAs of 7+ characters are accepted.
    """
    problems = []
    if not _sha_matches(review.get("head"), [head]):
        problems.append("head {!r}, expected {}".format(review.get("head"), head))
    if not _sha_matches(review.get("base"), [base]):
        expected = [base]
        try:
            expected.append(git(runner, repo, "merge-base", base, head))
        except EvalError:
            pass
        if not _sha_matches(review.get("base"), expected):
            problems.append("base {!r}, expected {}".format(review.get("base"), " or ".join(dict.fromkeys(expected))))
    if problems:
        summary = review.get("summary")
        raise EvalError(
            "the review did not run on this case's commits ({}); review summary: {}".format(
                "; ".join(problems), _tail(summary if isinstance(summary, str) else json.dumps(summary), 300)
            )
        )


class Verdict(NamedTuple):
    found: bool
    matched_finding_ids: List[str]
    reasoning: str


def parse_judge(output: Any, finding_ids: Sequence[str]) -> Verdict:
    """Turn the judge's answer (dict or text) into a checked Verdict.

    found is only true when the judge names at least one id that exists in
    this review; unknown ids are dropped.
    """
    if isinstance(output, dict):
        obj = output
    else:
        try:
            obj = extract_json(str(output or ""), lambda d: "found" in d)
        except ValueError as exc:
            raise EvalError("could not read the judge's answer: {}".format(exc))
    found = obj.get("found")
    if not isinstance(found, bool):
        raise EvalError("judge answer has non-boolean found: {!r}".format(found))
    raw_ids = obj.get("matched_finding_ids", [])
    if not isinstance(raw_ids, list) or not all(isinstance(i, str) for i in raw_ids):
        raise EvalError("judge answer has invalid matched_finding_ids: {!r}".format(raw_ids))
    reasoning = obj.get("reasoning", "")
    reasoning = reasoning if isinstance(reasoning, str) else json.dumps(reasoning)
    known = set(finding_ids)
    matched: List[str] = []
    for fid in raw_ids:
        if fid in known and fid not in matched:
            matched.append(fid)
    dropped = [fid for fid in raw_ids if fid not in known]
    notes = []
    if dropped:
        notes.append("ignored unknown finding ids {}".format(dropped))
    if found and not matched:
        found = False
        notes.append("judge said found but named no finding from this review; counted as not found")
    if not found and matched:
        matched = []
        notes.append("judge said not found; matched ids cleared")
    if notes:
        reasoning = (reasoning + " [runner: " + "; ".join(notes) + "]").strip()
    return Verdict(found, matched, reasoning)


def build_judge_prompt(case: Dict[str, Any], findings: List[Dict[str, Any]]) -> str:
    problem = case["known_problem"]
    return JUDGE_PROMPT_TEMPLATE.format(
        case_id=case["id"],
        repo=case["repo"],
        summary=problem["summary"],
        files=", ".join(problem.get("files") or []) or "(not given)",
        severity=problem.get("severity", "Important"),
        findings_json=json.dumps(findings, indent=2, ensure_ascii=False),
    )


# ---------------------------------------------------------------------------
# Running cases
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Config:
    project: str
    repo_path: Path
    cases_dir: Path
    out_dir: Path
    standards_ref: str
    threshold: int
    only: List[str]
    model: Optional[str]
    plugin_dir: Path
    dry_run: bool
    keep_worktrees: bool
    timeout: float
    fetch: bool
    out_explicit: bool = False  # --out was given


@dataclasses.dataclass
class Context:
    config: Config
    runner: Runner
    standards: Standards
    review_schema: Dict[str, Any]
    review_schema_cli: str
    judge_schema_cli: str
    cost_usd: float = 0.0


def run_case(ctx: Context, case: Dict[str, Any]) -> Dict[str, Any]:
    """Run one case. Never raises (except KeyboardInterrupt/SystemExit)."""
    cfg = ctx.config
    started = time.monotonic()
    result: Dict[str, Any] = {
        "case_id": case.get("id", "?"),
        "base": "",
        "head": "",
        "found": None,
        "findings": [],
        "error": None,
    }
    parent: Optional[Path] = None
    worktree: Optional[Path] = None
    try:
        base, head = resolve_case_commits(ctx.runner, case, cfg.repo_path)
        result["base"], result["head"] = base, head
        parent, worktree = create_worktree(ctx.runner, cfg.repo_path, head, case["id"])
        overlay_standards(ctx.runner, cfg.repo_path, ctx.standards, worktree)

        review_cmd = build_review_cmd(base, cfg.plugin_dir, cfg.model, ctx.review_schema_cli)
        review_out = parse_cli_output(run_checked(ctx.runner, review_cmd, cwd=str(worktree), timeout=cfg.timeout))
        ctx.cost_usd += review_out.cost_usd
        review = extract_review_result(review_out, ctx.review_schema)
        check_review_target(ctx.runner, cfg.repo_path, review, base, head)
        findings = review["findings"]
        result["findings"] = findings

        if not findings:
            verdict = Verdict(False, [], "The review reported no findings.")
        else:
            judge_cmd = build_judge_cmd(build_judge_prompt(case, findings), cfg.model, ctx.judge_schema_cli)
            judge_out = parse_cli_output(run_checked(ctx.runner, judge_cmd, cwd=str(parent), timeout=cfg.timeout))
            ctx.cost_usd += judge_out.cost_usd
            answer = judge_out.structured if isinstance(judge_out.structured, dict) else judge_out.text
            verdict = parse_judge(answer, [f["id"] for f in findings])
        result["found"] = verdict.found
        result["matched_finding_ids"] = verdict.matched_finding_ids
        result["judge_reasoning"] = verdict.reasoning
    except Exception as exc:  # one case's failure never stops the run
        result["found"] = None
        result["error"] = str(exc) if isinstance(exc, EvalError) else "{}: {}".format(type(exc).__name__, exc)
    finally:
        if worktree is not None and parent is not None:
            if cfg.keep_worktrees:
                print("  kept worktree for {}: {}".format(result["case_id"], worktree))
            else:
                remove_worktree(ctx.runner, cfg.repo_path, parent, worktree)
        result["duration_s"] = round(time.monotonic() - started, 1)
    return result


def score_cases(cases: Sequence[Dict[str, Any]], threshold: int) -> Tuple[Dict[str, int], bool]:
    errors = sum(1 for c in cases if c.get("error"))
    found = sum(1 for c in cases if c.get("found") is True and not c.get("error"))
    score = {"found": found, "total": len(cases) - errors, "errors": errors}
    return score, found >= threshold and errors == 0


def exit_code_for(score: Dict[str, int], passed: bool) -> int:
    if score["errors"]:
        return EXIT_ERROR
    return EXIT_PASS if passed else EXIT_FAIL


def verdict_line(score: Dict[str, int], passed: bool, threshold: int, n_cases: int) -> str:
    line = "{} {}/{} (threshold {})".format("PASS" if passed else "FAIL", score["found"], n_cases, threshold)
    if score["errors"]:
        line += ", {} case(s) errored".format(score["errors"])
    return line


def make_run_id(started: _dt.datetime, review_md_sha: Optional[str]) -> str:
    return "{}-{}".format(started.strftime("%Y%m%dT%H%M%SZ"), review_md_sha[:7] if review_md_sha else "none")


def build_results(
    cfg: Config, standards: Standards, started: _dt.datetime, cases: List[Dict[str, Any]]
) -> Dict[str, Any]:
    score, passed = score_cases(cases, cfg.threshold)
    return {
        "run_id": make_run_id(started, standards.review_md_sha),
        "project": cfg.project,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "standards": {
            "repo_path": str(cfg.repo_path),
            "standards_ref": standards.commit,
            "claude_md_sha": standards.claude_md_sha,
            "review_md_sha": standards.review_md_sha,
        },
        "model": cfg.model,
        "threshold": cfg.threshold,
        "score": score,
        "passed": passed,
        "cases": cases,
    }


def format_table(cases: Sequence[Dict[str, Any]]) -> str:
    rows = [("case", "found", "matched", "findings", "secs", "note")]
    for c in cases:
        found = "ERROR" if c.get("error") else ("yes" if c.get("found") else "no")
        note = c.get("error") or ""
        note = note if len(note) <= 70 else note[:67] + "..."
        rows.append(
            (
                str(c.get("case_id")),
                found,
                ",".join(c.get("matched_finding_ids") or []) or "-",
                str(len(c.get("findings") or [])),
                "{:.0f}".format(c.get("duration_s", 0)),
                note,
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    lines = []
    for r in rows:
        lines.append("  ".join(r[i].ljust(widths[i]) for i in range(5)) + ("  " + r[5] if r[5] else ""))
    return "\n".join(line.rstrip() for line in lines)


def warn(message: str) -> None:
    print("warning: " + message, file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_evals.py",
        description="Run a project's review eval set (spends Claude tokens: two claude calls per case).",
    )
    p.add_argument("--project", required=True, help="eval set name: evals/review/cases/<project>")
    p.add_argument("--repo-path", required=True, help="local clone of the project repo")
    p.add_argument("--cases", help="cases directory (default: evals/review/cases/<project>)")
    p.add_argument("--out", help="results directory (default: evals/review/results/<project> for a full run; "
                                 "partial runs default to a temp folder and may not write under results/)")
    p.add_argument("--standards-ref", default="origin/HEAD",
                   help="ref whose CLAUDE.md, REVIEW.md and .claude/rules/ are laid over every case (default: origin/HEAD)")
    p.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                   help="cases that must be found to pass (default: {})".format(DEFAULT_THRESHOLD))
    p.add_argument("--only", nargs="+", default=[], metavar="CASE_ID", help="run only these case ids")
    p.add_argument("--model", help="claude model alias or id for both calls (default: the CLI's default)")
    p.add_argument("--plugin-dir", default=str(DEFAULT_PLUGIN_DIR),
                   help="the factory plugin directory (default: this repo's plugins/factory)")
    p.add_argument("--dry-run", action="store_true", help="resolve and validate everything, print commands, call no model")
    p.add_argument("--keep-worktrees", action="store_true", help="leave each case's temporary worktree on disk")
    p.add_argument("--timeout", type=float, default=900, help="seconds allowed per claude call (default: 900)")
    p.add_argument("--no-fetch", action="store_true", help="do not run `git fetch origin` before resolving the standards ref")
    args = p.parse_args(argv)
    if args.threshold < 0:
        p.error("--threshold must be >= 0")
    if args.timeout <= 0:
        p.error("--timeout must be > 0")
    return args


def make_config(args: argparse.Namespace) -> Config:
    return Config(
        project=args.project,
        repo_path=Path(args.repo_path).expanduser().resolve(),
        cases_dir=Path(args.cases).expanduser().resolve() if args.cases else SCRIPT_DIR / "cases" / args.project,
        out_dir=Path(args.out).expanduser().resolve() if args.out else RESULTS_DIR / args.project,
        standards_ref=args.standards_ref,
        threshold=args.threshold,
        only=list(args.only),
        model=args.model,
        plugin_dir=Path(args.plugin_dir).expanduser().resolve(),
        dry_run=args.dry_run,
        keep_worktrees=args.keep_worktrees,
        timeout=args.timeout,
        fetch=not args.no_fetch,
        out_explicit=bool(args.out),
    )


def partial_reasons(cfg: Config) -> List[str]:
    """Why this run is not a full gate run (empty for a full run)."""
    reasons = []
    if cfg.only:
        reasons.append("--only")
    default_cases = SCRIPT_DIR / "cases" / cfg.project
    if cfg.cases_dir.resolve() != default_cases.resolve():
        reasons.append("--cases is not {}".format(default_cases))
    if cfg.threshold < DEFAULT_THRESHOLD:
        reasons.append("--threshold {} is below the default {}".format(cfg.threshold, DEFAULT_THRESHOLD))
    return reasons


def _is_within(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except ValueError:
        return False
    return True


def _config_errors(cfg: Config, runner: Runner) -> List[str]:
    errors = []
    if not cfg.repo_path.is_dir() or not git_ok(runner, cfg.repo_path, "rev-parse", "--git-dir"):
        errors.append("--repo-path is not a git repository: {}".format(cfg.repo_path))
    if not (cfg.plugin_dir / ".claude-plugin" / "plugin.json").is_file():
        errors.append("--plugin-dir has no .claude-plugin/plugin.json: {}".format(cfg.plugin_dir))
    return errors


def _origin_slug(runner: Runner, repo: Path) -> Optional[str]:
    try:
        url = git(runner, repo, "remote", "get-url", "origin")
    except EvalError:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1).lower() if m else None


def dry_run(ctx: Context, cases: List[Dict[str, Any]]) -> int:
    cfg = ctx.config
    print("# dry run: no claude calls, no worktrees, no results file")
    print("REVIEW_SCHEMA={}".format(shlex.quote(ctx.review_schema_cli)))
    print("JUDGE_SCHEMA={}".format(shlex.quote(ctx.judge_schema_cli)))
    errors = 0

    def show(cmd: List[str]) -> str:
        text = shlex.join(cmd)
        text = text.replace(shlex.quote(ctx.review_schema_cli), '"$REVIEW_SCHEMA"')
        return text.replace(shlex.quote(ctx.judge_schema_cli), '"$JUDGE_SCHEMA"')

    for case in cases:
        print("\n## {}".format(case["id"]))
        try:
            base, head = resolve_case_commits(ctx.runner, case, cfg.repo_path)
        except EvalError as exc:
            errors += 1
            print("ERROR: {}".format(exc))
            continue
        # Placeholder: real runs use a fresh tempfile.mkdtemp() folder per case.
        tmp = "<tmpdir>/factory-eval-{}".format(case["id"])
        wt = tmp + "/wt"
        print("base={} head={}".format(base, head))
        print(shlex.join(["git", "-C", str(cfg.repo_path), "worktree", "add", "--detach", "--quiet", wt, head]))
        print("# overlay from {}: {}".format(ctx.standards.commit[:12], ", ".join(sorted(ctx.standards.files)) or "(no standards files)"))
        print("(cd {} && {})".format(wt, show(build_review_cmd(base, cfg.plugin_dir, cfg.model, ctx.review_schema_cli))))
        judge = build_judge_cmd("<judge prompt: known problem + the review's findings>", cfg.model, ctx.judge_schema_cli)
        print("(cd {} && {})".format(tmp, show(judge)))
        print(shlex.join(["git", "-C", str(cfg.repo_path), "worktree", "remove", "--force", wt]))
    print("\n{} case(s) resolved, {} error(s)".format(len(cases) - errors, errors))
    return EXIT_ERROR if errors else EXIT_PASS


def main(argv: Optional[Sequence[str]] = None, runner: Optional[Runner] = None) -> int:
    args = parse_args(argv)
    cfg = make_config(args)
    run = runner or subprocess_runner

    problems = _config_errors(cfg, run)
    case_schema = load_schema("eval-case.schema.json")
    cases, case_errors = load_cases(cfg.cases_dir, case_schema)
    problems.extend(case_errors)
    if cfg.only:
        ids = {c["id"] for c in cases}
        unknown = [i for i in cfg.only if i not in ids]
        if unknown and not case_errors:
            problems.append("--only names unknown case id(s): {}".format(", ".join(unknown)))
        cases = [c for c in cases if c["id"] in cfg.only]
    partial = partial_reasons(cfg)
    if partial and not cfg.dry_run:
        if not cfg.out_explicit:
            cfg.out_dir = PARTIAL_RESULTS_DIR / cfg.project
            warn("partial run ({}): results go to {}, not to {}".format(
                ", ".join(partial), cfg.out_dir, RESULTS_DIR / cfg.project))
        elif _is_within(cfg.out_dir, RESULTS_DIR):
            problems.append(
                "partial run ({}) may not write under {}: the scorecard would read it as the gate result; "
                "pass --out outside that folder, or run the whole set at the default threshold".format(
                    ", ".join(partial), RESULTS_DIR))
    if problems:
        for p in problems:
            print("config error: " + p, file=sys.stderr)
        return EXIT_ERROR

    if cfg.threshold > len(cases):
        warn("threshold {} is higher than the number of cases ({}); the run cannot pass".format(cfg.threshold, len(cases)))
    slug = _origin_slug(run, cfg.repo_path)
    for case in cases:
        if slug and case["repo"].lower() != slug:
            warn("case {} is for {} but --repo-path's origin is {}".format(case["id"], case["repo"], slug))

    if cfg.fetch:
        try:
            git(run, cfg.repo_path, "fetch", "--quiet", "origin")
        except EvalError as exc:
            warn("git fetch origin failed, using local refs: {}".format(exc))
    try:
        standards = resolve_standards(run, cfg.repo_path, cfg.standards_ref)
    except EvalError as exc:
        print("config error: {}".format(exc), file=sys.stderr)
        return EXIT_ERROR
    if standards.review_md_sha is None:
        warn("no REVIEW.md at {}; the review falls back to the factory defaults".format(cfg.standards_ref))

    review_schema = load_schema("review-result.schema.json")
    ctx = Context(
        config=cfg,
        runner=run,
        standards=standards,
        review_schema=review_schema,
        review_schema_cli=schema_for_cli(review_schema),
        judge_schema_cli=schema_for_cli(JUDGE_SCHEMA),
    )

    if cfg.dry_run:
        return dry_run(ctx, cases)

    started = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    print("Eval {}: {} case(s), standards {} ({}), REVIEW.md {}".format(
        cfg.project, len(cases), cfg.standards_ref, standards.commit[:12],
        (standards.review_md_sha or "missing")[:12]))
    results: List[Dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        print("[{}/{}] {} ...".format(i, len(cases), case["id"]), flush=True)
        results.append(run_case(ctx, case))

    doc = build_results(cfg, standards, started, results)
    schema_errors = validate(doc, load_schema("eval-result.schema.json"))
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.out_dir / "{}.json".format(doc["run_id"])
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print()
    print(format_table(results))
    if ctx.cost_usd:
        print("claude cost: ${:.2f}".format(ctx.cost_usd))
    print("results: {}".format(out_path))
    print(verdict_line(doc["score"], doc["passed"], cfg.threshold, len(results)))
    if schema_errors:
        for e in schema_errors[:10]:
            print("results schema error: " + e, file=sys.stderr)
        return EXIT_ERROR
    return exit_code_for(doc["score"], doc["passed"])


if __name__ == "__main__":
    sys.exit(main())
