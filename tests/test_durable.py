"""Regression tests for shared atomic, durable writes."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.durable import atomic_write_text
from lib.results import write_json


class DurableWriteTests(unittest.TestCase):
    def test_replaces_atomically_and_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "receipt.json"
            atomic_write_text(path, "first", new_mode=0o640)
            self.assertEqual(path.read_text(), "first")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            atomic_write_text(path, "second", new_mode=0o600)
            self.assertEqual(path.read_text(), "second")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_failed_replace_leaves_old_value_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "receipt.json"
            path.write_text("old")
            with mock.patch("lib.durable.os.replace", side_effect=OSError("no")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, "new")
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_results_serialization_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "result.json"
            write_json(path, {"non_ascii": "λ", "value": 1})
            self.assertEqual(
                path.read_text(),
                '{\n  "non_ascii": "λ",\n  "value": 1\n}',
            )

    def test_file_and_parent_directory_are_fsynced(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "receipt.json"
            with mock.patch("lib.durable.os.fsync") as fsync:
                atomic_write_text(path, "value")
            self.assertEqual(fsync.call_count, 2)


if __name__ == "__main__":
    unittest.main()
