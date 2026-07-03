"""Independent, SPEC-DERIVED tests for peel's classify floors.

These were written against the *documented spec*, NOT the implementation, as an
independent oracle for the `--classify-floor` work (whose impl + in-file tests
were authored together and so cannot catch their own bugs). Every fixture path
and every expectation below is traceable to a documented requirement; nothing is
read off peel.py's internal constants (`_CONE_*`, the regexes, etc.).

Spec sources (quoted inline at each test):
  docs/spec_gen_experiment_design.md
    §"Codebase layers" — the L1..L5 table (file → layer):
        L1 Public exec API     edwards.rs, montgomery.rs, ristretto.rs, scalar.rs
        L2 Spec vocabulary     specs/*.rs   (contracts are WRITTEN in this)
        L3 Correctness lemmas  lemmas/<area>_lemmas/
        L4 Field               specs/field_specs*, lemmas/field_lemmas/, backend/.../field.rs
        L5 Number theory/vstd  common_lemmas/, vstd   (the assumed floor)
    §"Contract-integrity invariants (hold for every rung)":
        - never delete an `axiom_*`; never delete a `spec fn`
        - spec fns the contract is written in stay frozen
        - vstd is the assumed, frozen floor
  peel.py classify_cone docstring — the floor definitions:
        field         : L4 field + L5 number theory stay frozen
        number-theory : strip through L4 (field lemmas + backend field proofs);
                        freeze the L5/common/vstd core + spec vocabulary + axioms
        trusted-core  : strip every in-repo non-axiom proof artifact; freeze spec
                        definitions, exec code, axioms/assumes, and external vstd
  Rung ladder (§"Rung ladder, hardest last") — floors are cumulative, so the
  editable set must grow strictly: field ⊂ number-theory ⊂ trusted-core.

Run: python3 -m unittest tests.test_peel_floor_spec   (Python 3.11+)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import peel  # noqa: E402

FLOORS = ("field", "number-theory", "trusted-core")


def _write(project: Path, rel: str, text: str) -> None:
    p = project / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _layered_project(root: str) -> Path:
    """A minimal tree whose files sit at the L1..L5 paths the design doc names.
    Spec-fn names: `spec_*`; axiom names: `axiom_*`; lemma names: `lemma_*`."""
    project = Path(root) / "curve25519-dalek"
    # L1 — public exec API. Proof bodies are inline; exec must survive.
    _write(project, "src/edwards.rs",
           "pub fn decompress() -> u64 { let r = 1u64; proof { assert(r == 1); } r }\n")
    _write(project, "src/ristretto.rs",
           "pub fn decode() -> u64 { let r = 2u64; proof { assert(r == 2); } r }\n")
    # L1 API with NO inline proof (adversarial case for the cumulative ladder).
    _write(project, "src/scalar.rs",
           "pub fn from_bytes() -> u64 { 3u64 }\n")
    # L2 — spec vocabulary. PURE spec file: the contract's frozen vocabulary.
    _write(project, "src/specs/edwards_specs.rs",
           "pub open spec fn spec_point(x: int) -> int { x }\n")
    # L2/L4 — a spec module that ALSO carries a proof lemma + an axiom.
    _write(project, "src/specs/field_specs.rs",
           "pub open spec fn spec_field(x: nat) -> nat { x }\n"
           "proof fn lemma_field_spec() {}\n"
           "pub proof fn axiom_field_spec() {}\n")
    # L3 — correctness lemmas, plus a local spec fn + an axiom in the same file.
    _write(project, "src/lemmas/edwards_lemmas/curve.rs",
           "proof fn lemma_curve() {}\n"
           "pub proof fn axiom_curve() {}\n"
           "pub open spec fn spec_curve() -> int { 0 }\n")
    # L4 — field lemmas + backend field exec.
    _write(project, "src/lemmas/field_lemmas/add.rs",
           "proof fn lemma_field() {}\n"
           "pub proof fn axiom_field() {}\n")
    _write(project, "src/backend/serial/u64/field.rs",
           "pub fn fadd() -> u64 { let r = 4u64; proof { assert(r == 4); } r }\n")
    # L5 — number-theory/common substrate (the assumed floor).
    _write(project, "src/lemmas/common_lemmas/mul.rs",
           "proof fn lemma_common() {}\n"
           "pub proof fn axiom_common() {}\n"
           "pub open spec fn spec_common() -> int { 1 }\n")
    return project


# Names planted in the fixture, by spec category (NOT read from the impl).
AXIOM_NAMES = {"axiom_curve", "axiom_field", "axiom_common", "axiom_field_spec"}
SPEC_FN_NAMES = {"spec_point", "spec_field", "spec_curve", "spec_common"}


class FloorSpecInvariants(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.project = _layered_project(self._td.name)
        self.member = self.project.name  # "curve25519-dalek"

    def tearDown(self):
        self._td.cleanup()

    # ── helpers (read only the manifest contract: files[].path / proof_op /
    #    lemmas — all part of the public manifest shape, not impl internals) ──
    def _manifest(self, floor):
        return peel.classify_cone(self.project, floor=floor)

    def _editable(self, floor):
        return {f["path"] for f in self._manifest(floor)["files"]}

    def _deleted_lemmas(self, floor):
        out = []
        for f in self._manifest(floor)["files"]:
            out += f.get("lemmas", [])
        return out

    def _by_path(self, floor):
        return {f["path"]: f for f in self._manifest(floor)["files"]}

    # ── INVARIANT: never delete an axiom_* (contract-integrity inv. 3) ───────
    def test_no_floor_deletes_an_axiom(self):
        for floor in FLOORS:
            deleted = set(self._deleted_lemmas(floor))
            self.assertEqual(
                deleted & AXIOM_NAMES, set(),
                f"{floor}: scheduled an axiom_* for deletion (AXIOM_DRIFT vector)")

    # ── INVARIANT: never delete a spec fn (contract-integrity inv. 3) ────────
    def test_no_floor_deletes_a_spec_fn(self):
        for floor in FLOORS:
            deleted = set(self._deleted_lemmas(floor))
            self.assertEqual(
                deleted & SPEC_FN_NAMES, set(),
                f"{floor}: scheduled a spec fn for deletion (weakens the contract)")

    # ── INVARIANT: vstd / external is the frozen floor — nothing peeled escapes
    #    the member crate's src/ (L5 vstd is assumed, never reconstructed) ────
    def test_no_floor_escapes_the_member_src(self):
        prefix = f"{self.member}/src/"
        for floor in FLOORS:
            for p in self._editable(floor):
                self.assertTrue(
                    p.startswith(prefix),
                    f"{floor}: editable path escapes member src (vstd/external?): {p}")

    # ── INVARIANT: the contract's spec VOCABULARY file is never a target.
    #    A pure L2 spec file has no proof to reconstruct, so no floor should
    #    make it editable (it defines what the frozen contract MEANS). ────────
    def test_pure_spec_vocabulary_file_is_never_editable(self):
        vocab = f"{self.member}/src/specs/edwards_specs.rs"
        for floor in FLOORS:
            self.assertNotIn(
                vocab, self._editable(floor),
                f"{floor}: a pure spec-vocabulary file became editable")

    # ── INVARIANT: L1 API proof bodies are STRIPPED (red), not admitted (green)
    #    — the agent reconstructs them from nothing; admitting would fake-green.
    def test_L1_api_proofs_are_stripped_not_admitted(self):
        api = f"{self.member}/src/edwards.rs"
        for floor in FLOORS:
            entry = self._by_path(floor).get(api)
            self.assertIsNotNone(entry, f"{floor}: L1 API with proof not peeled")
            self.assertTrue(
                entry.get("proof_op", "admit").startswith("strip"),
                f"{floor}: L1 API proof_op is {entry.get('proof_op')!r}, "
                f"expected a strip (red) variant")

    # ── INVARIANT: floors freeze exactly their named layers (classify spec) ──
    def test_field_floor_freezes_L4_and_L5(self):
        # field: "L4 field + L5 number theory stay frozen"
        e = self._editable("field")
        self.assertFalse(any("/field_lemmas/" in p for p in e), "L4 field lemma editable at field floor")
        self.assertFalse(any(p.endswith("/backend/serial/u64/field.rs") for p in e), "L4 backend field editable at field floor")
        self.assertFalse(any("/common_lemmas/" in p for p in e), "L5 common editable at field floor")

    def test_number_theory_floor_peels_L4_freezes_L5(self):
        # number-theory: "strip through L4 ... freeze the L5/common core"
        e = self._editable("number-theory")
        self.assertTrue(any("/field_lemmas/" in p for p in e), "L4 field lemma NOT peeled at number-theory floor")
        self.assertFalse(any("/common_lemmas/" in p for p in e), "L5 common editable at number-theory floor")

    def test_trusted_core_floor_peels_in_repo_proof_incl_L5_common(self):
        # trusted-core: "strip every in-repo non-axiom proof artifact"
        e = self._editable("trusted-core")
        self.assertTrue(any("/common_lemmas/" in p for p in e), "L5 common NOT peeled at trusted-core floor")

    # ── INVARIANT: difficulty ladder is cumulative (rungs hardest-last) ──────
    #    A lower floor's editable set must be a strict subset of the next.
    def test_difficulty_ladder_is_cumulative(self):
        field = self._editable("field")
        nt = self._editable("number-theory")
        core = self._editable("trusted-core")
        self.assertTrue(field < nt, f"field ⊄ number-theory; field-only: {field - nt}")
        self.assertTrue(nt < core, f"number-theory ⊄ trusted-core; nt-only: {nt - core}")


class StripPreservesExec(unittest.TestCase):
    """SPEC: the strip variants remove proof scaffolding ONLY — exec code and
    contracts are frozen (docs §invariants; classify trusted-core docstring:
    'keeps ... exec code ... frozen'). A floor that nuked exec would break the
    crate, not pose a reconstruction task."""

    def test_strip_all_keeps_exec_statements_drops_proof(self):
        src = ("pub fn f() -> u64 {\n"
               "    let r = 7u64;\n"
               "    proof { assert(r == 7); }\n"
               "    r\n"
               "}\n")
        out, _ = peel.peel_file_text(src, 1, path="x.rs", proof_op="strip-all")
        self.assertIn("let r = 7u64;", out, "strip-all removed executable code")
        self.assertNotIn("assert(r == 7)", out, "strip-all left proof scaffolding")


if __name__ == "__main__":
    unittest.main()
