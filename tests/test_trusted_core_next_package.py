import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "trusted_core_next_package", ROOT / "docker" / "trusted_core_next_package.py"
)
next_package = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(next_package)


class TrustedCoreNextPackageTests(unittest.TestCase):
    def test_production_manifest_files_define_successor_authority(self):
        manifest = json.loads(
            (ROOT / "peel_manifests" / "trusted_core_floor.json").read_text()
        )
        authorized = next_package._manifest_authorized_paths(manifest)
        expected = sorted(
            {entry["path"] for entry in manifest["files"]} | {"Cargo.lock"}
        )
        self.assertEqual(authorized, expected)
        self.assertIn("curve25519-dalek/src/ristretto.rs", authorized)

    def test_manifest_authority_never_silently_collapses_to_cargo_lock(self):
        for malformed in ({}, {"files": []}, {"files": [{}]}):
            with self.subTest(manifest=malformed):
                with self.assertRaisesRegex(
                    next_package.NextPackageError, "manifest"
                ):
                    next_package._manifest_authorized_paths(malformed)

    def test_fixture_only_manifest_alias_is_rejected(self):
        with self.assertRaisesRegex(next_package.NextPackageError, "manifest"):
            next_package._manifest_authorized_paths({
                "editable_files": ["crate/src/lib.rs"],
            })

    def test_successor_dominance_guard_rejects_seed_reset(self):
        def gate(tree, verification, rlimit, raw, verified):
            return {
                "tree_receipt": {"tree_hash": tree},
                "vector": {
                    "verification_errors": verification,
                    "resource_limits": rlimit,
                    "timeouts": 0,
                    "panics": 0,
                    "build_wrappers": 1,
                    "compile_errors": 0,
                    "raw_errors": raw,
                    "verified_count": verified,
                },
            }

        best = gate("round-7", 54, 9, 64, 1580)
        reset_bank = gate("seed", 83, 8, 92, 1527)
        primary_regression = gate("round-9", 55, 7, 63, 1581)
        self.assertEqual(
            next_package._gate_frontier_relation(best, reset_bank),
            "REGRESSED",
        )
        self.assertEqual(
            next_package._gate_frontier_relation(best, best),
            "NEUTRAL",
        )
        self.assertEqual(
            next_package._gate_frontier_relation(best, primary_regression),
            "REGRESSED",
        )
        with self.assertRaisesRegex(
            next_package.NextPackageError, "dominated"
        ):
            next_package._validate_bank_frontier({
                "final_tree_receipt": {"tree_hash": "seed"},
                "banking_gate_receipt": reset_bank,
                "best_decided_gate_receipt": best,
            })
        with self.assertRaisesRegex(
            next_package.NextPackageError, "dominated"
        ):
            next_package._validate_bank_frontier({
                "final_tree_receipt": {"tree_hash": "round-9"},
                "banking_gate_receipt": primary_regression,
                "best_decided_gate_receipt": best,
            })
        self.assertEqual(
            next_package._validate_bank_frontier({
                "final_tree_receipt": {"tree_hash": "round-7"},
                "banking_gate_receipt": best,
                "best_decided_gate_receipt": best,
            }),
            "NEUTRAL",
        )

    def test_successor_dominance_guard_accepts_first_decided_root_bank(self):
        first_bank = {
            "tree_receipt": {"tree_hash": "root-round-1"},
            "vector": {
                "verification_errors": 12,
                "resource_limits": 1,
                "timeouts": 0,
                "panics": 0,
                "build_wrappers": 1,
                "compile_errors": 0,
                "raw_errors": 14,
                "verified_count": 100,
            },
        }
        self.assertEqual(
            next_package._gate_frontier_relation({}, first_bank),
            "INITIAL",
        )
        self.assertEqual(
            next_package._validate_bank_frontier({
                "final_tree_receipt": {"tree_hash": "root-round-1"},
                "banking_gate_receipt": first_bank,
                "best_decided_gate_receipt": {},
            }),
            "INITIAL",
        )

    def test_successor_dominance_guard_requires_decided_tree_vectors(self):
        with self.assertRaisesRegex(
            next_package.NextPackageError, "lack tree identity"
        ):
            next_package._gate_frontier_relation(
                {"vector": {"verified_count": 1}},
                {"tree_receipt": {"tree_hash": "bank"},
                 "vector": {"verified_count": 1}},
            )

    def test_registration_binds_exact_predecessor_chain(self):
        current = {
            "budget": {"max_cost_usd": 100.0},
            "predecessor_accounting": {"oauth_evidence_sha256": "e" * 64},
            "offline_recovery_amendment": {"next_tree": "stale"},
        }
        promotion = {
            "receipt_id": "p", "final_tree_receipt": {"tree_hash": "tree"},
        }
        terminal = {"receipt_id": "t"}
        campaign = {"receipt_id": "c", "recorded_cost_usd": 12.5}
        updated = next_package.updated_registration(
            current, current_sha="r" * 64, promotion=promotion,
            terminal=terminal, campaign=campaign, patch_sha="a" * 64,
            replay_sha="b" * 64,
        )
        predecessor = updated["predecessor_accounting"]
        self.assertEqual(predecessor["promotion_receipt_id"], "p")
        self.assertEqual(predecessor["terminal_validation_receipt_id"], "t")
        self.assertEqual(predecessor["campaign_state_receipt_id"], "c")
        self.assertEqual(predecessor["bank_tree_hash"], "tree")
        self.assertEqual(predecessor["accounted_cost_usd"], 12.5)
        self.assertEqual(predecessor["available_headroom_usd"], 87.5)
        self.assertTrue(updated["supervisor_amendment"]["fail_closed"])
        self.assertNotIn("offline_recovery_amendment", updated)

    def test_package_replaces_all_lineage_roles_and_rehashes_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["old-reg", "old-patch", "old-promotion", "old-terminal", "old-state", "fixed"]
            paths = {name: root / name for name in names}
            for name, path in paths.items():
                path.write_text(name)
            new = {}
            for role in ("reg", "patch", "promotion", "terminal", "state", "replay"):
                path = root / f"new-{role}"
                path.write_text(f"new-{role}")
                new[role] = path
            argv = [
                "run", "--launch-registration", str(paths["old-reg"]),
                "--seed-wip", str(paths["old-patch"]),
                "--seed-receipt", str(paths["old-promotion"]),
                "--predecessor-terminal", str(paths["old-terminal"]),
                "--campaign-state", str(paths["old-state"]),
            ]
            current = {
                "launch_argv": argv,
                "immutable_inputs": [
                    {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in paths.values()
                ],
            }
            updated = next_package.updated_package(
                current, registration=new["reg"], patch=new["patch"],
                promotion=new["promotion"], terminal=new["terminal"],
                campaign=new["state"], replay=new["replay"],
            )
            input_paths = {entry["path"] for entry in updated["immutable_inputs"]}
            self.assertIn(str(paths["fixed"]), input_paths)
            for path in new.values():
                self.assertIn(str(path), input_paths)
            for name in names[:-1]:
                self.assertNotIn(str(paths[name]), input_paths)
            self.assertEqual(len(updated["package_id"]), 64)

    def test_root_package_without_seed_argv_gains_inserted_options(self):
        # F9: a fresh root launch carries NO seed/predecessor options
        # (run_agents.sh forbids them), so the first bank must be able to
        # insert them rather than fail closed on replace-only semantics.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_reg = root / "old-reg"
            old_reg.write_text("root registration")
            fixed = root / "fixed"
            fixed.write_text("fixed input")
            new = {}
            for role in ("reg", "patch", "promotion", "terminal", "state", "replay"):
                path = root / f"new-{role}"
                path.write_text(f"new-{role}")
                new[role] = path
            current = {
                "launch_argv": [
                    "run", "--launch-registration", str(old_reg),
                    "--other-flag", "kept",
                ],
                "immutable_inputs": [
                    {"path": str(path),
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in (old_reg, fixed)
                ],
            }
            updated = next_package.updated_package(
                current, registration=new["reg"], patch=new["patch"],
                promotion=new["promotion"], terminal=new["terminal"],
                campaign=new["state"], replay=new["replay"],
            )
            argv = updated["launch_argv"]
            for option, role in (
                ("--seed-wip", "patch"), ("--seed-receipt", "promotion"),
                ("--predecessor-terminal", "terminal"),
                ("--campaign-state", "state"),
            ):
                index = argv.index(option)
                self.assertEqual(argv[index + 1], str(new[role]))
            self.assertEqual(
                argv[argv.index("--launch-registration") + 1], str(new["reg"]),
            )
            self.assertEqual(argv[argv.index("--other-flag") + 1], "kept")
            input_paths = {entry["path"] for entry in updated["immutable_inputs"]}
            self.assertIn(str(fixed), input_paths)
            for path in new.values():
                self.assertIn(str(path), input_paths)
            self.assertNotIn(str(old_reg), input_paths)
            self.assertEqual(len(updated["package_id"]), 64)

    def test_cumulative_patch_replays_exact_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            bank = root / "bank"
            for tree in (canonical, bank):
                (tree / "crate" / "src").mkdir(parents=True)
                (tree / ".gitignore").write_text("Cargo.lock\n")
                (tree / "Cargo.toml").write_text("[workspace]\nmembers=['crate']\n")
                (tree / "crate" / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
                (tree / "crate" / "src" / "lib.rs").write_text("fn old() {}\n")
            (bank / "crate" / "src" / "lib.rs").write_text("fn proved() {}\n")
            (bank / "Cargo.lock").write_text("generated lock\n")
            expected = next_package.provenance.source_tree_receipt(bank / "crate")["tree_hash"]
            patch = root / "bank.patch"
            receipt = next_package.build_cumulative_patch(
                canonical=canonical, bank=bank, project_rel=Path("crate"),
                authorized=["Cargo.lock", "crate/src/lib.rs"], expected_tree=expected,
                patch_path=patch,
            )
            self.assertTrue(receipt["okay"])
            self.assertEqual(receipt["tree_hash"], expected)
            self.assertIn("diff --git", patch.read_text())
            self.assertIn("Cargo.lock", patch.read_text())

    def test_cumulative_patch_rejects_wrong_bank_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            bank = root / "bank"
            for tree, body in ((canonical, "old"), (bank, "new")):
                (tree / "crate" / "src").mkdir(parents=True)
                (tree / "Cargo.toml").write_text("[workspace]\nmembers=['crate']\n")
                (tree / "crate" / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
                (tree / "crate" / "src" / "lib.rs").write_text(body)
            with self.assertRaisesRegex(next_package.NextPackageError, "tree mismatch"):
                next_package.build_cumulative_patch(
                    canonical=canonical, bank=bank, project_rel=Path("crate"),
                    authorized=["crate/src/lib.rs"], expected_tree="wrong",
                    patch_path=root / "bank.patch",
                )

    def test_cumulative_patch_rejects_unauthorized_bank_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            bank = root / "bank"
            for tree in (canonical, bank):
                (tree / "crate" / "src").mkdir(parents=True)
                (tree / "Cargo.toml").write_text("[workspace]\nmembers=['crate']\n")
                (tree / "crate" / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
                (tree / "crate" / "src" / "lib.rs").write_text("same")
                (tree / "crate" / "src" / "frozen.rs").write_text("old")
            (bank / "crate" / "src" / "frozen.rs").write_text("changed")
            expected = next_package.provenance.source_tree_receipt(bank / "crate")["tree_hash"]
            with self.assertRaisesRegex(next_package.NextPackageError, "unauthorized"):
                next_package.build_cumulative_patch(
                    canonical=canonical, bank=bank, project_rel=Path("crate"),
                    authorized=["crate/src/lib.rs"], expected_tree=expected,
                    patch_path=root / "bank.patch",
                )


if __name__ == "__main__":
    unittest.main()
