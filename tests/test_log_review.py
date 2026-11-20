"""Run the log-review skill's own tests as part of `python3 -m unittest discover -s tests`.

The tests live next to the helper they test
(plugins/factory/skills/log-review/references/test_logrow.py) so the skill folder stays
self-contained. This loader imports that module under a unique name and hands its tests to
unittest, so CI's one discover run also checks the embedded schema copy in logrow.py against
schemas/review-log-row.schema.json (SchemaContractTest).
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REFERENCES = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "factory" / "skills" / "log-review" / "references"
)
TEST_FILE = REFERENCES / "test_logrow.py"
MODULE_NAME = "factory_log_review_test_logrow"


def _load_module():
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(MODULE_NAME, str(TEST_FILE))
    if spec is None or spec.loader is None:
        raise ImportError("cannot load {}".format(TEST_FILE))
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str) -> unittest.TestSuite:
    # Import errors must fail the run, not hide the suite.
    standard_tests.addTests(loader.loadTestsFromModule(_load_module()))
    return standard_tests
