"""Regression coverage for source-tree and gate receipt identity."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.provenance import (  # noqa: E402
    accepted_promotion_tree_hash,
    gate_signature,
    receipt_key,
    source_tree_receipt,
    write_immutable_json,
)


class ProvenanceReceiptTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        (root / "Cargo.toml").write_text("[workspace]\nmembers = ['crate']\n")
        project = root / "crate"
        (project / "src").mkdir(parents=True)
        (project / "Cargo.toml").write_text("[package]\nname='crate'\nversion='0.1.0'\n")
        (project / "src" / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n")
        return project

    def test_tree_hash_covers_workspace_source_but_not_generated_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            first = source_tree_receipt(project)
            (project / "target").mkdir()
            (project / "target" / "noise").write_text("generated")
            self.assertEqual(first["tree_hash"], source_tree_receipt(project)["tree_hash"])
            (project / "src" / "lib.rs").write_text("pub fn value() -> u8 { 2 }\n")
            second = source_tree_receipt(project)
            self.assertNotEqual(first["tree_hash"], second["tree_hash"])
            self.assertEqual(second["file_count"], len(second["files"]))

    def test_gate_signature_and_immutable_write_bind_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tool = root / "verus_check.py"
            tool.write_text("print('one')\n")
            one = gate_signature(["python3", str(tool), "--rlimit", "80"], tool_paths=[tool])
            two = gate_signature(["python3", str(tool), "--rlimit", "90"], tool_paths=[tool])
            self.assertNotEqual(one["signature"], two["signature"])
            self.assertNotEqual(receipt_key("tree-a", one["signature"]), receipt_key("tree-b", one["signature"]))
            out = root / "receipt.json"
            value = {"tree_hash": "tree-a", "gate": one}
            write_immutable_json(out, value)
            write_immutable_json(out, value)
            with self.assertRaises(RuntimeError):
                write_immutable_json(out, {"tree_hash": "tree-b"})
            self.assertEqual(json.loads(out.read_text())["tree_hash"], "tree-a")

    def test_only_accepted_reusable_promotion_can_authorize_a_seed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accepted = root / "accepted.json"
            accepted.write_text(json.dumps({
                "decision": "ACCEPTED",
                "terminal_disposition": {"state": "ACCEPTED", "reusable": True},
                "final_tree_receipt": {"tree_hash": "tree-good"},
            }))
            self.assertEqual(accepted_promotion_tree_hash(accepted), "tree-good")

            rejected = root / "rejected.json"
            rejected.write_text(json.dumps({
                "decision": "REJECTED",
                "terminal_disposition": {"state": "REJECTED_DRIFTED", "reusable": False},
                "final_tree_receipt": {"tree_hash": "tree-rejected"},
            }))
            with self.assertRaises(ValueError):
                accepted_promotion_tree_hash(rejected)


if __name__ == "__main__":
    unittest.main()
