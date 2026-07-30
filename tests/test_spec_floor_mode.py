"""Pins the spec-floor (abstraction co-invention) mode registration and the
abstraction_floor_v1 manifest shape against the dual-LGTM'd ruling of record
(AGENT_DEBATE 2026-07-07: PEEL 43 / KEEP_FROZEN 23 / DROP 15 => 58 deleted
spec fns across edwards_specs + ristretto_specs)."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run  # noqa: E402


class SpecFloorRegistration(unittest.TestCase):
    def test_mode_is_whole_crate(self):
        self.assertIn("spec-floor", run._WHOLE_CRATE_MODES)

    def test_block_renders_and_carries_the_load_bearing_rules(self):
        allow_edit = [
            Path("curve25519-dalek/src/specs/edwards_specs.rs"),
            Path("curve25519-dalek/src/specs/ristretto_specs.rs"),
            Path("curve25519-dalek/src/lemmas/edwards_lemmas/decompress_lemmas.rs"),
            Path("curve25519-dalek/src/ristretto.rs"),
        ]
        block = run._spec_floor_experiment_block(allow_edit)
        for needle in (
            "spec-floor (abstraction co-invention)",
            "edwards_specs.rs",
            "COMPLETE_NONEQUIV",       # equivalence scoring disclosed to agent
            "SPEC_DRIFT",              # surviving definitions frozen
            "FROZEN_EDIT",
            "--whole-crate",
        ):
            self.assertIn(needle, block)

    def test_dispatch_via_build_experiment_block_path(self):
        # the dispatcher must route mode="spec-floor" to the block above
        src = Path(run.__file__).read_text()
        self.assertIn('if mode == "spec-floor":', src)
        self.assertIn("_spec_floor_experiment_block(allow_edit)", src)


class AbstractionFloorManifest(unittest.TestCase):
    MANIFEST = Path(__file__).resolve().parent.parent / (
        "peel_manifests/abstraction_floor_v1.json")

    def setUp(self):
        self.m = json.loads(self.MANIFEST.read_text())

    def test_mode_and_pin_declared(self):
        self.assertEqual(self.m["experiment_mode"], "spec-floor")
        self.assertTrue(self.m["pin"].startswith("consumer:"))

    def test_58_deleted_spec_fns_across_the_two_spec_files(self):
        spec_entries = [f for f in self.m["files"]
                        if f["path"].endswith("_specs.rs")]
        self.assertEqual(len(spec_entries), 2)
        names = [n for f in spec_entries for n in f["spec_fns"]]
        self.assertEqual(len(names), 58)
        self.assertEqual(len(set(names)), 58)
        # spot-pin one from each ruling class that must be deleted
        self.assertIn("ristretto_compress_affine", names)   # PEEL (weak pin)
        self.assertIn("batch_compress_body", names)         # DROP
        # and the KEEP_FROZEN set must NOT be deleted
        for kept in ("edwards_add", "is_on_edwards_curve",
                     "edwards_scalar_mul", "ristretto_equivalent"):
            self.assertNotIn(kept, names)

    def test_field_floor_files_carried_over(self):
        base = json.loads((self.MANIFEST.parent / "field_floor.json")
                          .read_text())
        base_paths = set(f["path"] for f in base["files"])
        mine = set(f["path"] for f in self.m["files"])
        self.assertTrue(base_paths <= mine)
        self.assertEqual(len(mine - base_paths), 2)


if __name__ == "__main__":
    unittest.main()
