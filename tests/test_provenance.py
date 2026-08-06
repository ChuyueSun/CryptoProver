"""Regression coverage for source-tree and gate receipt identity."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.provenance import (  # noqa: E402
    accepted_promotion_tree_hash,
    canonical_json_bytes,
    gate_signature,
    derive_lineage_id,
    receipt_id,
    receipt_key,
    sha256_file,
    source_tree_receipt,
    reusable_seed_authority,
    validate_credential_identity,
    validate_launch_registration,
    validate_campaign_spec,
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
            (Path(td) / ".git").write_text(
                "gitdir: /path/that/depends/on/worktree/location\n"
            )
            self.assertEqual(first["tree_hash"], source_tree_receipt(project)["tree_hash"])
            (project / "src" / "lib.rs").write_text("pub fn value() -> u8 { 2 }\n")
            second = source_tree_receipt(project)
            self.assertNotEqual(first["tree_hash"], second["tree_hash"])
            self.assertEqual(second["file_count"], len(second["files"]))

    def test_tree_hash_covers_literal_include_below_ignored_directory(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            generated = project / "results" / "generated.rs"
            generated.parent.mkdir()
            generated.write_text("pub const VALUE: u8 = 1;\n")
            (project / "src" / "lib.rs").write_text(
                'include!("../results/generated.rs");\n'
            )
            first = source_tree_receipt(project)
            generated.write_text("pub const VALUE: u8 = 2;\n")
            second = source_tree_receipt(project)

            self.assertNotEqual(first["tree_hash"], second["tree_hash"])
            self.assertIn(
                "crate/results/generated.rs",
                {entry["path"] for entry in second["files"]},
            )
            self.assertEqual(
                second["literal_compiler_inputs"],
                ["crate/results/generated.rs"],
            )

    def test_tree_hash_covers_path_attribute_below_ignored_directory(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            generated = project / "target" / "evil.rs"
            generated.parent.mkdir()
            generated.write_text("pub fn value() -> u8 { 1 }\n")
            (project / "src" / "lib.rs").write_text(
                '#[path = "../target/evil.rs"]\nmod evil;\n'
            )
            first = source_tree_receipt(project)
            generated.write_text("pub fn value() -> u8 { 2 }\n")
            second = source_tree_receipt(project)
            self.assertNotEqual(first["tree_hash"], second["tree_hash"])

    def test_tree_hash_covers_raw_string_include_and_path_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            first_input = project / "target" / "one.rs"
            second_input = project / "target" / "two.rs"
            first_input.parent.mkdir()
            first_input.write_text("pub const ONE: u8 = 1;\n")
            second_input.write_text("pub fn two() -> u8 { 2 }\n")
            (project / "src" / "lib.rs").write_text(
                'include!(r#"../target/one.rs"#);\n'
                '#[path = r"../target/two.rs"] mod two;\n'
            )
            before = source_tree_receipt(project)
            first_input.write_text("pub const ONE: u8 = 9;\n")
            second_input.write_text("pub fn two() -> u8 { 9 }\n")
            after = source_tree_receipt(project)
            self.assertNotEqual(before["tree_hash"], after["tree_hash"])
            self.assertEqual(
                set(after["literal_compiler_inputs"]),
                {"crate/target/one.rs", "crate/target/two.rs"},
            )

    def test_source_subdirectory_named_target_is_not_treated_as_build_output(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._workspace(Path(td))
            module = project / "src" / "target" / "mod.rs"
            module.parent.mkdir()
            module.write_text("pub fn value() -> u8 { 1 }\n")
            first = source_tree_receipt(project)
            module.write_text("pub fn value() -> u8 { 2 }\n")
            second = source_tree_receipt(project)
            self.assertNotEqual(first["tree_hash"], second["tree_hash"])

    def test_linked_worktree_control_file_cannot_change_source_hash(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            project = self._workspace(repo)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(
                [
                    "git", "-C", str(repo),
                    "-c", "user.email=test@example.com",
                    "-c", "user.name=test",
                    "commit", "-q", "-m", "source",
                ],
                check=True,
            )
            first = base / "a"
            second = base / "worktree-with-a-different-path-length"
            for destination in (first, second):
                subprocess.run(
                    [
                        "git", "-C", str(repo), "worktree", "add", "-q",
                        "--detach", str(destination), "HEAD",
                    ],
                    check=True,
                )
            self.assertNotEqual(
                (first / ".git").read_text(),
                (second / ".git").read_text(),
            )
            self.assertEqual(
                source_tree_receipt(first / project.name)["tree_hash"],
                source_tree_receipt(second / project.name)["tree_hash"],
            )

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

    def test_receipt_id_is_path_independent_and_content_sensitive(self):
        base = {"schema_version": 2, "tree_hash": "tree-a"}
        first = {**base, "receipt_path": "/one"}
        second = {**base, "receipt_path": "/two"}
        self.assertEqual(receipt_id(first), receipt_id(second))
        self.assertNotEqual(
            receipt_id(first),
            receipt_id({**second, "tree_hash": "tree-b"}),
        )
        self.assertEqual(
            canonical_json_bytes({"b": 2, "a": 1}),
            b'{"a":1,"b":2}',
        )

    def test_campaign_spec_binds_manifest_hash_and_census(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest_dir = root / "peel_manifests"
            manifest_dir.mkdir()
            manifest = manifest_dir / "cut.json"
            manifest.write_text(json.dumps({
                "files": [
                    {"path": "a.rs", "lemmas": ["one", "two"]},
                    {"path": "b.rs", "proof_op": "strip-all"},
                ],
            }))
            spec = manifest_dir / "campaign.json"
            spec.write_text(json.dumps({
                "schema_version": 1,
                "campaign_id": "test",
                "status": "pre_registered",
                "task": {
                    "manifest_path": "peel_manifests/cut.json",
                    "manifest_sha256": sha256_file(manifest),
                    "manifest_file_count": 2,
                    "deleted_named_lemma_count": 2,
                    "source_ref": "ref",
                    "source_commit": "commit",
                    "source_tree": "tree",
                    "expected_pre_tree_hash": "a" * 64,
                    "expected_post_peel_tree_hash": "b" * 64,
                },
                "stopping_policy": {
                    "k": None,
                    "max_cost_usd": None,
                    "max_wall_seconds": None,
                },
            }))
            validated = validate_campaign_spec(spec, repo_root=root)
            self.assertEqual(validated["manifest_file_count"], 2)
            self.assertEqual(
                validated["unresolved_budget_fields"],
                ["k", "max_cost_usd", "max_wall_seconds"],
            )
            with self.assertRaisesRegex(ValueError, "budget is not authorized"):
                validate_campaign_spec(
                    spec, repo_root=root, require_launch_budget=True,
                )

            manifest.write_text(manifest.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "manifest hash mismatch"):
                validate_campaign_spec(spec, repo_root=root)

    def test_launch_registration_binds_runtime_and_budget(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "launch.json"
            campaign = {
                "campaign_id": "campaign",
                "campaign_spec_sha256": "campaign-sha",
                "manifest_sha256": "manifest-sha",
            }
            value = {
                "schema_version": 1,
                "scoreable": True,
                "campaign_id": "campaign",
                "campaign_spec_sha256": "campaign-sha",
                "manifest_sha256": "manifest-sha",
                "container_image_digest": "image",
                "harness_git_commit": "commit",
                "harness_source_tree_sha256": "tree",
                "provider_proxy_policy_sha256": "policy",
                "credential_identity": {
                    "kind": "claude_code_oauth",
                    "algorithm": "sha256-salt-token-v1",
                    "salt_hex": "a" * 64,
                    "digest": hashlib.sha256(
                        bytes.fromhex("a" * 64) + b"oauth-token"
                    ).hexdigest(),
                },
                "hint_level": "H0",
                "execution": {
                    "agent_backend": "claude",
                    "model": "claude-haiku-4-5",
                    "rounds": 10,
                    "max_task_minutes": 60,
                    "verus_rlimit": 80,
                    "cargo_jobs": 4,
                    "cpu_shares": 1024,
                    "memory": "8g",
                    "max_parallel": 1,
                    "agent_max_turns": 50,
                    "experiment_mode": "field-floor",
                },
                "budget": {
                    "authorization_id": "user-1",
                    "max_cost_usd": 1000,
                    "max_wall_seconds": 604800,
                    "plateau_k": 4,
                },
            }
            path.write_text(json.dumps(value))
            result = validate_launch_registration(
                path, campaign=campaign,
                actual_image_digest="image",
                actual_harness_commit="commit",
                actual_harness_tree_sha256="tree",
                actual_proxy_policy_sha256="policy",
            )
            self.assertEqual(
                result["credential_identity"]["kind"], "claude_code_oauth")
            validate_credential_identity(value, "oauth-token")
            with self.assertRaisesRegex(ValueError, "does not match"):
                validate_credential_identity(value, "other-token")
            missing_identity = {**value}
            missing_identity.pop("credential_identity")
            path.write_text(json.dumps(missing_identity))
            with self.assertRaisesRegex(ValueError, "credential_identity"):
                validate_launch_registration(path, campaign=campaign)
            self.assertEqual(result["registration_id"], receipt_id(value))
            missing_turn_cap = json.loads(json.dumps(value))
            missing_turn_cap["execution"].pop("agent_max_turns")
            path.write_text(json.dumps(missing_turn_cap))
            with self.assertRaisesRegex(ValueError, "agent_max_turns"):
                validate_launch_registration(path, campaign=campaign)
            value["budget"]["max_cost_usd"] = None
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "must be positive"):
                validate_launch_registration(path, campaign=campaign)

            for invalid in (float("nan"), float("inf"), float("-inf")):
                invalid_value = json.loads(json.dumps(value))
                invalid_value["budget"]["max_cost_usd"] = invalid
                path.write_text(json.dumps(invalid_value))
                with self.assertRaisesRegex(ValueError, "must be positive"):
                    validate_launch_registration(path, campaign=campaign)

            invalid_plateau = json.loads(json.dumps(value))
            invalid_plateau["budget"]["max_cost_usd"] = 1000
            invalid_plateau["budget"]["plateau_k"] = 1.5
            path.write_text(json.dumps(invalid_plateau))
            with self.assertRaisesRegex(ValueError, "positive integer"):
                validate_launch_registration(path, campaign=campaign)

            invalid_execution = json.loads(json.dumps(invalid_plateau))
            invalid_execution["budget"]["plateau_k"] = 4
            invalid_execution["execution"]["max_task_minutes"] = float("nan")
            path.write_text(json.dumps(invalid_execution))
            with self.assertRaisesRegex(ValueError, "must be positive"):
                validate_launch_registration(path, campaign=campaign)

    def test_banked_partial_requires_fresh_exact_gate_and_vector(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bank.json"
            value = {
                "schema_version": 2,
                "decision": "BANKED_PARTIAL",
                "scoreable": True,
                "lineage_id": "lineage",
                "campaign_spec_sha256": "campaign",
                "terminal_disposition": {
                    "state": "BANKED_PARTIAL",
                    "reusable": True,
                },
                "final_tree_receipt": {"tree_hash": "tree"},
                "banking_gate_receipt": {
                    "fresh": True,
                    "exact_tree_match": True,
                    "tree_receipt": {"tree_hash": "tree"},
                    "vector": {
                        "hard_admits": 4,
                        "verification_errors": 3,
                        "resource_limits": 2,
                        "raw_errors": 5,
                    },
                },
            }
            value["receipt_id"] = receipt_id(value)
            path.write_text(json.dumps(value))
            authority = reusable_seed_authority(path)
            self.assertEqual(authority["kind"], "BANKED_PARTIAL")
            self.assertEqual(authority["tree_hash"], "tree")
            self.assertEqual(
                derive_lineage_id("campaign", "start"),
                derive_lineage_id("campaign", "start"),
            )

            value["banking_gate_receipt"]["fresh"] = False
            value["receipt_id"] = receipt_id(value)
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "fresh exact-tree"):
                reusable_seed_authority(path)

    def test_accepted_seed_requires_scored_green_exact_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "accepted.json"
            forged = {
                "decision": "ACCEPTED",
                "terminal_disposition": {
                    "state": "ACCEPTED", "reusable": True,
                },
                "final_tree_receipt": {"tree_hash": "tree"},
                "lineage_id": "lineage",
            }
            path.write_text(json.dumps(forged))
            with self.assertRaisesRegex(ValueError, "content ID mismatch"):
                reusable_seed_authority(path)

            accepted = {
                **forged,
                "schema_version": 2,
                "scoreable": True,
                "campaign_spec_sha256": "campaign",
                "acceptance_gate_receipt": {
                    "fresh": True,
                    "exact_tree_match": True,
                    "tree_receipt": {"tree_hash": "tree"},
                    "verus_result": {"okay": True},
                    "vector": {
                        "hard_admits": 0,
                        "verification_errors": 0,
                        "resource_limits": 0,
                        "raw_errors": 0,
                    },
                },
            }
            accepted["receipt_id"] = receipt_id(accepted)
            path.write_text(json.dumps(accepted))
            self.assertEqual(reusable_seed_authority(path)["kind"], "COMPLETE")

            accepted["acceptance_gate_receipt"]["vector"]["hard_admits"] = -1
            accepted["receipt_id"] = receipt_id(accepted)
            path.write_text(json.dumps(accepted))
            with self.assertRaisesRegex(ValueError, "non-negative"):
                reusable_seed_authority(path)


if __name__ == "__main__":
    unittest.main()
