"""Security and portability pins for result-directory identifiers."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lib.results import (
    task_dir,
    validate_launcher_run_id,
    validate_run_id,
)


class ResultIdentifierTests(unittest.TestCase):
    def test_accepts_portable_run_ids(self):
        for value in ("run-1", "layer_A.20260805", "0", "A_b-c.d"):
            with self.subTest(value=value):
                self.assertEqual(validate_run_id(value), value)

    def test_rejects_traversal_shell_control_and_pathological_ids(self):
        bad = (
            "", ".hidden", ".", "..", "a..b", "../escape", "a/b",
            r"a\b", "a b", "a\nline", "x;touch_pwned", "$(id)",
            "x" * 129,
        )
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_run_id(value)

    def test_task_dir_rejects_both_untrusted_components_before_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results"
            for run_id, target_id in (("../escape", "target"),
                                      ("run", "../../escape")):
                with self.subTest(run_id=run_id, target_id=target_id), \
                        self.assertRaises(ValueError):
                    task_dir(root, run_id, target_id)
            self.assertFalse(root.exists())

    def test_launcher_validation_reports_unsupported_python_precisely(self):
        with self.assertRaisesRegex(
            RuntimeError,
            r"Python 3\.11\+ required; got 3\.9\.6 from /usr/bin/python3",
        ):
            validate_launcher_run_id(
                "safe-run",
                version_info=(3, 9, 6),
                executable="/usr/bin/python3",
            )

    def test_module_cli_validates_runtime_and_run_id(self):
        root = Path(__file__).resolve().parents[1]
        accepted = subprocess.run(
            [sys.executable, "-m", "lib.results", "validate-run-id", "run-1"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        rejected = subprocess.run(
            [sys.executable, "-m", "lib.results", "validate-run-id", "../bad"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("invalid run id", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
