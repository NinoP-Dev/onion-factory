#!/usr/bin/env python3
"""Validate the onion-factory repo's contracts and plugin layout.

Checks, relative to --root (default: the repo this file lives in):

  schemas/*.json                    valid JSON; a valid Draft 2020-12 schema
  evals/review/cases/**/*.json      valid against eval-case.schema.json; "id" equals the file name
  evals/review/results/**/*.json    valid against eval-result.schema.json; "passed" is consistent
  .claude-plugin/marketplace.json   required fields; every relative plugin source exists ('./' paths,
                                    or bare names under metadata.pluginRoot)
  plugins/*/.claude-plugin/plugin.json
                                    required fields; kebab-case name; manifest paths stay inside
  plugins/*/skills/*/SKILL.md       frontmatter with name (equal to the folder) and description

Two modes:

  jsonschema  the `jsonschema` package is installed: full Draft 2020-12 validation, with the
              relative "$ref": "finding.schema.json" resolved from the local schemas/ folder.
  fallback    no `jsonschema`: a built-in validator for the keyword subset the repo's schemas
              use (type, enum, const, required, properties, additionalProperties, items,
              pattern, min/max length and value, oneOf/anyOf/allOf, local $ref). `format` is
              not checked in this mode, and any other keyword a schema uses (not, if/then/else,
              uniqueItems, ...) is reported as a WARNING naming the keyword it skipped.

Runtime: Python 3.9+, standard library only (jsonschema is optional).
Exit status: 0 when no errors (warnings allowed), 1 when any error, 2 on bad usage.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

# Plugin and marketplace naming rules (Claude Code plugin docs, manifest and marketplace references).
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MARKETPLACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
EVAL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Manifest keys whose string values are paths inside the plugin.
MANIFEST_PATH_KEYS = ("skills", "commands", "agents", "outputStyles", "hooks", "mcpServers", "lspServers")
# Skill listing truncates description + when_to_use at this many characters.
SKILL_DESCRIPTION_LIMIT = 1536


# --------------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------------
class Report:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.checked = 0

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def error(self, path: Path, message: str) -> None:
        self.errors.append("ERROR   {}: {}".format(self._rel(path), message))

    def warn(self, path: Path, message: str) -> None:
        self.warnings.append("WARNING {}: {}".format(self._rel(path), message))

    @property
    def ok(self) -> bool:
        return not self.errors


def _load_json(path: Path, report: Report) -> Tuple[bool, Any]:
    report.checked += 1
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.error(path, "cannot read: {}".format(exc))
        return False, None
    try:
        return True, json.loads(text)
    except json.JSONDecodeError as exc:
        report.error(path, "invalid JSON: {} (line {}, column {})".format(exc.msg, exc.lineno, exc.colno))
        return False, None


def _json_path(parts: Any) -> str:
    out = "$"
    for part in parts:
        out += "[{}]".format(part) if isinstance(part, int) else ".{}".format(part)
    return out


# --------------------------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------------------------
# A validator takes (instance, schema_file_name) and returns a list of "path: message" strings.
InstanceValidator = Callable[[Any, str], List[str]]


def _import_jsonschema() -> Optional[Any]:
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return None
    if not hasattr(jsonschema, "Draft202012Validator"):
        return None  # jsonschema < 4.0 has no Draft 2020-12 support
    return jsonschema


def _jsonschema_version(jsonschema: Any) -> str:
    try:
        from importlib.metadata import version  # Python 3.8+

        return version("jsonschema")
    except Exception:  # noqa: BLE001 - version is informational only
        return getattr(jsonschema, "__version__", "unknown")


class JsonschemaBackend:
    """Full validation with the jsonschema package, refs resolved from the local schemas/."""

    name = "jsonschema"

    def __init__(self, jsonschema: Any, schemas: Dict[str, Tuple[Path, Dict[str, Any]]]) -> None:
        self.jsonschema = jsonschema
        self.schemas = schemas
        self.cls = jsonschema.Draft202012Validator
        self.format_checker = jsonschema.FormatChecker()
        # Every schema is reachable by its $id and by its file URI, so a relative $ref such as
        # "finding.schema.json" resolves against either base without any network access.
        self.uris: Dict[str, str] = {}
        store: Dict[str, Dict[str, Any]] = {}
        for file_name, (path, schema) in schemas.items():
            file_uri = path.resolve().as_uri()
            base = schema.get("$id") if isinstance(schema.get("$id"), str) else file_uri
            self.uris[file_name] = base
            store[base] = schema
            store[file_uri] = schema
        self.store = store
        self.registry = self._make_registry(store)

    @staticmethod
    def _make_registry(store: Dict[str, Dict[str, Any]]) -> Optional[Any]:
        # jsonschema >= 4.18 resolves refs with the `referencing` library; older versions use
        # RefResolver (see _validator).
        try:
            from referencing import Registry, Resource  # type: ignore
            from referencing.jsonschema import DRAFT202012  # type: ignore
        except ImportError:
            return None
        resources = [
            (uri, Resource.from_contents(schema, default_specification=DRAFT202012))
            for uri, schema in store.items()
        ]
        return Registry().with_resources(resources)

    def version_label(self) -> str:
        return "jsonschema {}".format(_jsonschema_version(self.jsonschema))

    def check_schema(self, schema: Any) -> List[str]:
        try:
            self.cls.check_schema(schema)
        except self.jsonschema.exceptions.SchemaError as exc:
            return ["{}: {}".format(_json_path(exc.absolute_path), exc.message)]
        return []

    def _validator(self, file_name: str) -> Any:
        schema = self.schemas[file_name][1]
        if self.registry is not None:
            return self.cls(schema, registry=self.registry, format_checker=self.format_checker)
        resolver = self.jsonschema.RefResolver(
            base_uri=self.uris[file_name], referrer=schema, store=dict(self.store)
        )
        return self.cls(schema, resolver=resolver, format_checker=self.format_checker)

    def validate(self, instance: Any, file_name: str) -> List[str]:
        try:
            errors = sorted(
                self._validator(file_name).iter_errors(instance),
                key=lambda e: [str(p) for p in e.absolute_path],
            )
        except Exception as exc:  # noqa: BLE001 - unresolvable $ref and similar schema problems
            return ["$: could not validate against {}: {}".format(file_name, exc)]
        return ["{}: {}".format(_json_path(e.absolute_path), e.message) for e in errors]


class FallbackBackend:
    """Standard-library validator for the keyword subset the repo's schemas use."""

    name = "fallback"

    def __init__(self, schemas: Dict[str, Tuple[Path, Dict[str, Any]]]) -> None:
        self.schemas = schemas

    @staticmethod
    def version_label() -> str:
        return "jsonschema not installed; built-in subset validator, `format` not checked"

    @staticmethod
    def check_schema(schema: Any) -> List[str]:
        if not isinstance(schema, dict):
            return ["$: a schema must be a JSON object"]
        problems: List[str] = []

        def walk(node: Any, path: List[Any]) -> None:
            if isinstance(node, dict):
                for key in ("required", "enum"):
                    if key in node and not isinstance(node[key], list):
                        problems.append("{}: '{}' must be an array".format(_json_path(path + [key]), key))
                if "pattern" in node:
                    try:
                        re.compile(node["pattern"])
                    except (re.error, TypeError) as exc:
                        problems.append("{}: bad pattern: {}".format(_json_path(path + ["pattern"]), exc))
                if "properties" in node and not isinstance(node["properties"], dict):
                    problems.append("{}: 'properties' must be an object".format(_json_path(path)))
                for key, value in node.items():
                    if key in ("enum", "const", "default", "examples", "x-column-order"):
                        continue
                    walk(value, path + [key])
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, path + [index])

        walk(schema, [])
        return problems

    # Keywords _check implements, and keywords that are annotations or containers only.
    SUPPORTED_KEYWORDS = frozenset((
        "type", "const", "enum", "minLength", "maxLength", "pattern", "minimum", "maximum",
        "required", "properties", "additionalProperties", "minItems", "items",
        "allOf", "anyOf", "oneOf", "$ref",
    ))
    ANNOTATION_KEYWORDS = frozenset((
        "$schema", "$id", "$comment", "$defs", "definitions", "title", "description", "format",
        "default", "examples", "deprecated", "readOnly", "writeOnly", "contentMediaType",
        "contentEncoding",
    ))

    @classmethod
    def unsupported_keywords(cls, schema: Any) -> List[str]:
        """Keywords in a schema that this fallback does NOT check, as "path: keyword" strings.

        Walks only subschema positions, so property names under "properties" are not mistaken
        for keywords. Extension keywords ("x-...") are ignored.
        """
        found: List[str] = []

        def walk(node: Any, path: List[Any]) -> None:
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if key in cls.SUPPORTED_KEYWORDS or key in cls.ANNOTATION_KEYWORDS or key.startswith("x-"):
                    pass
                else:
                    found.append("{}: '{}'".format(_json_path(path), key))
                # Descend into every place a subschema can sit, supported keyword or not.
                if key in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas") \
                        and isinstance(value, dict):
                    for name, sub in value.items():
                        walk(sub, path + [key, name])
                elif key in ("allOf", "anyOf", "oneOf", "prefixItems") and isinstance(value, list):
                    for index, sub in enumerate(value):
                        walk(sub, path + [key, index])
                elif key in ("items", "additionalProperties", "not", "if", "then", "else", "contains",
                             "propertyNames", "unevaluatedItems", "unevaluatedProperties",
                             "additionalItems"):
                    walk(value, path + [key])

        walk(schema, [])
        return found

    def validate(self, instance: Any, file_name: str) -> List[str]:
        schema = self.schemas[file_name][1]
        return self._check(instance, schema, schema, [])

    # -- subset implementation --------------------------------------------------------------
    def _resolve(self, ref: str, root: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        doc_part, _, fragment = ref.partition("#")
        target_root = root
        if doc_part:
            name = doc_part.rstrip("/").rsplit("/", 1)[-1]
            if name not in self.schemas:
                raise KeyError("cannot resolve $ref {!r}".format(ref))
            target_root = self.schemas[name][1]
        node: Any = target_root
        for token in [t for t in fragment.split("/") if t]:
            token = token.replace("~1", "/").replace("~0", "~")
            node = node[int(token)] if isinstance(node, list) else node[token]
        return node, target_root

    @staticmethod
    def _is_type(value: Any, name: str) -> bool:
        if name == "null":
            return value is None
        if name == "boolean":
            return isinstance(value, bool)
        if name == "integer":
            if isinstance(value, bool):
                return False
            return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
        if name == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if name == "string":
            return isinstance(value, str)
        if name == "array":
            return isinstance(value, list)
        if name == "object":
            return isinstance(value, dict)
        return False

    @staticmethod
    def _equal(a: Any, b: Any) -> bool:
        # JSON equality: true is not 1.
        if isinstance(a, bool) or isinstance(b, bool):
            return isinstance(a, bool) and isinstance(b, bool) and a == b
        return a == b

    def _check(self, inst: Any, schema: Any, root: Dict[str, Any], path: List[Any]) -> List[str]:
        if schema is True or schema == {}:
            return []
        if schema is False:
            return ["{}: not allowed here".format(_json_path(path))]
        errors: List[str] = []
        here = _json_path(path)

        if "$ref" in schema:
            try:
                target, target_root = self._resolve(schema["$ref"], root)
            except (KeyError, IndexError, ValueError) as exc:
                return ["{}: {}".format(here, exc)]
            errors += self._check(inst, target, target_root, path)

        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(self._is_type(inst, t) for t in types):
                return errors + ["{}: {!r} is not of type {}".format(here, inst, " or ".join(types))]

        if "const" in schema and not self._equal(inst, schema["const"]):
            errors.append("{}: {!r} is not {!r}".format(here, inst, schema["const"]))
        if "enum" in schema and not any(self._equal(inst, v) for v in schema["enum"]):
            errors.append("{}: {!r} is not one of {}".format(here, inst, schema["enum"]))

        if isinstance(inst, str):
            if "minLength" in schema and len(inst) < schema["minLength"]:
                errors.append("{}: {!r} is shorter than {}".format(here, inst, schema["minLength"]))
            if "maxLength" in schema and len(inst) > schema["maxLength"]:
                errors.append("{}: string is longer than {}".format(here, schema["maxLength"]))
            if "pattern" in schema and re.search(schema["pattern"], inst) is None:
                errors.append("{}: {!r} does not match {!r}".format(here, inst, schema["pattern"]))

        if self._is_type(inst, "number"):
            if "minimum" in schema and inst < schema["minimum"]:
                errors.append("{}: {} is less than the minimum of {}".format(here, inst, schema["minimum"]))
            if "maximum" in schema and inst > schema["maximum"]:
                errors.append("{}: {} is more than the maximum of {}".format(here, inst, schema["maximum"]))

        if isinstance(inst, dict):
            for key in schema.get("required", []):
                if key not in inst:
                    errors.append("{}: {!r} is a required property".format(here, key))
            properties = schema.get("properties", {})
            for key, value in inst.items():
                if key in properties:
                    errors += self._check(value, properties[key], root, path + [key])
                elif "additionalProperties" in schema:
                    extra = schema["additionalProperties"]
                    if extra is False:
                        errors.append("{}: additional property {!r} is not allowed".format(here, key))
                    elif isinstance(extra, dict):
                        errors += self._check(value, extra, root, path + [key])

        if isinstance(inst, list):
            if "minItems" in schema and len(inst) < schema["minItems"]:
                errors.append("{}: fewer than {} items".format(here, schema["minItems"]))
            if isinstance(schema.get("items"), dict):
                for index, item in enumerate(inst):
                    errors += self._check(item, schema["items"], root, path + [index])

        for sub in schema.get("allOf", []):
            errors += self._check(inst, sub, root, path)
        if "anyOf" in schema and not any(not self._check(inst, s, root, path) for s in schema["anyOf"]):
            errors.append("{}: does not match any of the allowed shapes (anyOf)".format(here))
        if "oneOf" in schema:
            matches = sum(1 for s in schema["oneOf"] if not self._check(inst, s, root, path))
            if matches != 1:
                errors.append(
                    "{}: must match exactly one of the allowed shapes (oneOf), matched {}".format(here, matches)
                )
        return errors


# --------------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------------
def check_schemas(root: Path, report: Report) -> Dict[str, Tuple[Path, Dict[str, Any]]]:
    """Load schemas/*.json. Returns {file name: (path, schema)} for the ones that parsed."""
    schema_dir = root / "schemas"
    loaded: Dict[str, Tuple[Path, Dict[str, Any]]] = {}
    files = sorted(schema_dir.glob("*.json")) if schema_dir.is_dir() else []
    if not files:
        report.error(schema_dir, "no schemas found (expected schemas/*.json)")
        return loaded
    for path in files:
        ok, data = _load_json(path, report)
        if not ok:
            continue
        if not isinstance(data, dict):
            report.error(path, "a schema must be a JSON object")
            continue
        if data.get("$schema") != DRAFT_2020_12:
            report.error(path, "$schema must be {!r}, found {!r}".format(DRAFT_2020_12, data.get("$schema")))
        loaded[path.name] = (path, data)
    return loaded


def check_schema_contents(
    backend: Any, schemas: Dict[str, Tuple[Path, Dict[str, Any]]], report: Report
) -> None:
    for name, (path, schema) in schemas.items():
        for problem in backend.check_schema(schema):
            report.error(path, "not a valid Draft 2020-12 schema: {}".format(problem))
        # Fallback mode only: name each keyword it cannot check, so an OK result is not
        # mistaken for full validation. Install jsonschema to check them.
        unsupported = getattr(backend, "unsupported_keywords", None)
        if callable(unsupported):
            for item in unsupported(schema):
                report.warn(path, "fallback validator does not check keyword {}; data using this "
                                  "schema is only partly validated (install jsonschema)".format(item))
        # Every relative $ref must name a schema file that exists next to this one.
        for ref in _collect_refs(schema):
            doc_part = ref.partition("#")[0]
            if doc_part and "://" not in doc_part:
                target = doc_part.rstrip("/").rsplit("/", 1)[-1]
                if target not in schemas:
                    report.error(path, "$ref {!r} does not resolve to a file in schemas/".format(ref))
        # x-column-order (review-log-row) must list exactly the properties, in order.
        order = schema.get("x-column-order")
        if order is not None:
            props = list(schema.get("properties", {}).keys())
            if order != props:
                report.error(path, "x-column-order does not match the property names in order")


def _collect_refs(node: Any) -> List[str]:
    refs: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                refs.append(value)
            elif key not in ("enum", "const", "default", "examples"):
                refs += _collect_refs(value)
    elif isinstance(node, list):
        for value in node:
            refs += _collect_refs(value)
    return refs


def _validate_with(
    backend: Any, schemas: Dict[str, Tuple[Path, Dict[str, Any]]], schema_name: str,
    path: Path, data: Any, report: Report,
) -> bool:
    if schema_name not in schemas:
        report.error(path, "cannot validate: schemas/{} is missing or invalid".format(schema_name))
        return False
    problems = backend.validate(data, schema_name)
    for problem in problems:
        report.error(path, "does not match {}: {}".format(schema_name, problem))
    return not problems


def check_eval_cases(root: Path, backend: Any, schemas: Dict[str, Tuple[Path, Dict[str, Any]]],
                     report: Report) -> None:
    cases_dir = root / "evals" / "review" / "cases"
    if not cases_dir.is_dir():
        return
    for path in sorted(cases_dir.rglob("*.json")):
        ok, data = _load_json(path, report)
        if not ok:
            continue
        _validate_with(backend, schemas, "eval-case.schema.json", path, data, report)
        if isinstance(data, dict):
            case_id = data.get("id")
            if case_id != path.stem:
                report.error(path, "\"id\" is {!r} but the file name says {!r}".format(case_id, path.stem))
        if path.parent.parent != cases_dir:
            report.warn(path, "eval cases belong at evals/review/cases/<project>/<id>.json")


def check_eval_results(root: Path, backend: Any, schemas: Dict[str, Tuple[Path, Dict[str, Any]]],
                       report: Report) -> None:
    results_dir = root / "evals" / "review" / "results"
    if not results_dir.is_dir():
        return
    for path in sorted(results_dir.rglob("*.json")):
        ok, data = _load_json(path, report)
        if not ok:
            continue
        valid = _validate_with(backend, schemas, "eval-result.schema.json", path, data, report)
        if not (valid and isinstance(data, dict)):
            continue
        score = data["score"]
        expected = score["found"] >= data["threshold"] and score["errors"] == 0
        if data["passed"] != expected:
            report.error(
                path,
                "\"passed\" is {} but found={} threshold={} errors={} means {}".format(
                    json.dumps(data["passed"]), score["found"], data["threshold"], score["errors"],
                    json.dumps(expected),
                ),
            )
        if score["found"] > score["total"]:
            report.error(path, "score.found ({}) is more than score.total ({})".format(score["found"], score["total"]))
        if data["run_id"] != path.stem:
            report.warn(path, "run_id {!r} differs from the file name {!r}".format(data["run_id"], path.stem))
        if path.parent.name != data["project"]:
            report.warn(path, "project {!r} differs from its folder {!r}".format(data["project"], path.parent.name))


def _is_bare_name(value: str) -> bool:
    """A plugin source like "factory": no path separator, not '.' or '..'."""
    return bool(value) and "/" not in value and "\\" not in value and value not in (".", "..")


def _check_relative_path(value: str) -> Optional[str]:
    if not value.startswith("./"):
        return "must start with './'"
    if ".." in Path(value).parts:
        return "must not contain '..'"
    return None


def check_marketplace(root: Path, report: Report) -> None:
    path = root / ".claude-plugin" / "marketplace.json"
    if not path.is_file():
        report.error(path, "missing marketplace manifest")
        return
    ok, data = _load_json(path, report)
    if not ok:
        return
    if not isinstance(data, dict):
        report.error(path, "must be a JSON object")
        return

    name = data.get("name")
    if not isinstance(name, str) or not name:
        report.error(path, "\"name\" is required")
    elif not MARKETPLACE_NAME_RE.match(name) or ".." in name:
        report.error(path, "\"name\" {!r} may use only letters, digits, '.', '_' and '-'".format(name))

    owner = data.get("owner")
    if not isinstance(owner, dict) or not isinstance(owner.get("name"), str) or not owner.get("name"):
        report.error(path, "\"owner\" must be an object with a non-empty \"name\"")

    if not data.get("description") and not (data.get("metadata") or {}).get("description"):
        report.warn(path, "no \"description\" (claude plugin validate warns about this)")

    # metadata.pluginRoot: base folder for bare-name plugin sources ("source": "factory").
    plugin_root: Optional[str] = None
    metadata = data.get("metadata")
    raw_root = metadata.get("pluginRoot") if isinstance(metadata, dict) else None
    if raw_root is not None:
        if not isinstance(raw_root, str) or not raw_root:
            report.error(path, "metadata.pluginRoot must be a non-empty string")
        elif Path(raw_root).is_absolute() or ".." in Path(raw_root).parts:
            report.error(path, "metadata.pluginRoot {!r} must be a relative path without '..'".format(raw_root))
        elif not (root / raw_root).is_dir():
            report.error(path, "metadata.pluginRoot {!r} does not exist".format(raw_root))
        else:
            plugin_root = raw_root

    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        report.error(path, "\"plugins\" must be an array")
        return
    seen = set()
    for index, entry in enumerate(plugins):
        where = "plugins[{}]".format(index)
        if not isinstance(entry, dict):
            report.error(path, "{} must be an object".format(where))
            continue
        entry_name = entry.get("name")
        if not isinstance(entry_name, str) or not entry_name:
            report.error(path, "{}: \"name\" is required".format(where))
        else:
            where = "{} ({})".format(where, entry_name)
            if not PLUGIN_NAME_RE.match(entry_name):
                report.error(path, "{}: \"name\" must be kebab-case".format(where))
            if entry_name in seen:
                report.error(path, "{}: duplicate plugin name".format(where))
            seen.add(entry_name)

        source = entry.get("source")
        if source is None:
            report.error(path, "{}: \"source\" is required".format(where))
        elif isinstance(source, str):
            if plugin_root is not None and _is_bare_name(source):
                # Bare-name source: resolved as <marketplace root>/<metadata.pluginRoot>/<name>.
                plugin_dir = root / plugin_root / source
            else:
                problem = _check_relative_path(source)
                if problem:
                    hint = "" if plugin_root is not None or not _is_bare_name(source) else (
                        " (a bare plugin name needs metadata.pluginRoot)")
                    report.error(path, "{}: source {!r} {}{}".format(where, source, problem, hint))
                    continue
                # Relative sources resolve from the marketplace root (the folder holding .claude-plugin/).
                plugin_dir = root / source
            if not plugin_dir.is_dir():
                report.error(path, "{}: source {!r} does not exist".format(where, source))
                continue
            manifest = plugin_dir / ".claude-plugin" / "plugin.json"
            if manifest.is_file() and isinstance(entry_name, str):
                try:
                    manifest_name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
                except (ValueError, OSError, AttributeError):
                    manifest_name = None  # reported by check_plugins
                if manifest_name is not None and manifest_name != entry_name:
                    report.error(
                        path,
                        "{}: entry name differs from plugin.json name {!r}; installs would fail with "
                        "'Plugin not found'".format(where, manifest_name),
                    )
        elif isinstance(source, dict):
            if not isinstance(source.get("source"), str):
                report.error(path, "{}: an object source needs a \"source\" kind (github, url, ...)".format(where))
        else:
            report.error(path, "{}: \"source\" must be a './' path or an object".format(where))


def check_plugins(root: Path, report: Report) -> None:
    plugins_dir = root / "plugins"
    if not plugins_dir.is_dir():
        return
    for plugin_dir in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
        manifest = plugin_dir / ".claude-plugin" / "plugin.json"
        if not manifest.is_file():
            report.error(manifest, "missing plugin manifest")
        else:
            _check_plugin_manifest(manifest, report)
        misplaced = plugin_dir / ".claude-plugin" / "skills"
        if misplaced.exists():
            report.error(misplaced, "skills/ belongs at the plugin root, not inside .claude-plugin/")
        check_skills(plugin_dir, report)


def _check_plugin_manifest(path: Path, report: Report) -> None:
    ok, data = _load_json(path, report)
    if not ok:
        return
    if not isinstance(data, dict):
        report.error(path, "must be a JSON object")
        return
    name = data.get("name")
    if not isinstance(name, str) or not name:
        report.error(path, "\"name\" is required")
    elif not PLUGIN_NAME_RE.match(name):
        report.error(path, "\"name\" {!r} must be kebab-case (no spaces, '@', ':' or '/')".format(name))
    if "version" in data and not isinstance(data["version"], str):
        report.error(path, "\"version\" must be a string")
    if "description" in data and not isinstance(data["description"], str):
        report.error(path, "\"description\" must be a string")
    if "author" in data:
        author = data["author"]
        if not isinstance(author, dict) or not isinstance(author.get("name"), str) or not author.get("name"):
            report.error(path, "\"author\" must be an object with a non-empty \"name\"")
    for key in MANIFEST_PATH_KEYS:
        values = data.get(key)
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list):
            continue  # inline objects (hooks, mcpServers) are allowed
        for value in values:
            if isinstance(value, str):
                problem = _check_relative_path(value)
                if problem:
                    report.error(path, "\"{}\" path {!r} {}".format(key, value, problem))


def parse_frontmatter(text: str) -> Optional[Dict[str, str]]:
    """Parse the simple YAML frontmatter of a SKILL.md by hand (no PyYAML).

    Returns None when the file has no frontmatter block. Only top-level `key: value` lines are
    read; folded or literal block scalars (`>` / `|`) collect their indented lines; YAML lists
    under a key are returned as the raw joined text.
    """
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    fields: Dict[str, str] = {}
    key: Optional[str] = None
    block: List[str] = []

    def flush() -> None:
        if key is not None and block:
            fields[key] = " ".join(part.strip() for part in block if part.strip())

    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):(?:\s+(.*))?\s*$", line) if not line[0].isspace() else None
        if match:
            flush()
            key, value = match.group(1), (match.group(2) or "").strip()
            block = []
            if value in ("", ">", "|", ">-", "|-", ">+", "|+"):
                fields[key] = ""
            else:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                fields[key] = value
                key = None
        elif key is not None:
            block.append(line)
    flush()
    return fields


def check_skills(plugin_dir: Path, report: Report) -> None:
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        return
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        path = skill_dir / "SKILL.md"
        if not path.is_file():
            report.error(path, "missing SKILL.md")
            continue
        report.checked += 1
        try:
            fields = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            report.error(path, "cannot read: {}".format(exc))
            continue
        if fields is None:
            report.error(path, "no YAML frontmatter (the file must start with a '---' block)")
            continue
        name = fields.get("name", "")
        description = fields.get("description", "")
        if not name:
            report.error(path, "frontmatter needs a non-empty \"name\"")
        elif name != skill_dir.name:
            report.error(
                path,
                "frontmatter name {!r} differs from its folder {!r}; the skill would not be "
                "/{}:{}".format(name, skill_dir.name, plugin_dir.name, skill_dir.name),
            )
        if not description:
            report.error(path, "frontmatter needs a non-empty \"description\"")
        listing = len(description) + len(fields.get("when_to_use", ""))
        if listing > SKILL_DESCRIPTION_LIMIT:
            report.warn(path, "description + when_to_use is {} chars; the skill listing truncates at {}".format(
                listing, SKILL_DESCRIPTION_LIMIT))


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------
def validate(root: Path, use_jsonschema: bool = True) -> Tuple[Report, str]:
    """Run every check under root. Returns (report, mode description)."""
    report = Report(root)
    schemas = check_schemas(root, report)
    jsonschema = _import_jsonschema() if use_jsonschema else None
    backend: Any = JsonschemaBackend(jsonschema, schemas) if jsonschema else FallbackBackend(schemas)
    mode = "{} ({})".format(backend.name, backend.version_label())
    check_schema_contents(backend, schemas, report)
    check_eval_cases(root, backend, schemas, report)
    check_eval_results(root, backend, schemas, report)
    check_marketplace(root, report)
    check_plugins(root, report)
    return report, mode


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate schemas, eval data and plugin layout.")
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent,
        help="repository root to validate (default: the repo containing this script)",
    )
    parser.add_argument(
        "--no-jsonschema", action="store_true",
        help="use the built-in fallback validator even when jsonschema is installed",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print("validate.py: --root {} is not a directory".format(root), file=sys.stderr)
        return 2

    report, mode = validate(root, use_jsonschema=not args.no_jsonschema)
    print("validate.py: root={}".format(root))
    print("validate.py: mode={}".format(mode))
    for line in report.warnings + report.errors:
        print(line)
    print("validate.py: {} files checked, {} errors, {} warnings -> {}".format(
        report.checked, len(report.errors), len(report.warnings), "OK" if report.ok else "FAILED"))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
