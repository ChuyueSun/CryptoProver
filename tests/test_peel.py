"""All peel tests, one file. Four sections:

  A. UNIT TRANSFORMS   — peel.peel_file_text on small synthetic snippets:
     per-shell behavior, monotonicity, the pin rule, admit/strip variants,
     surface/classify helpers.
  B. GOLDEN FRAGMENTS  — hand-written EXACT before/after on verbatim real
     fragments (edwards / ristretto / montgomery / curve_equation_lemmas),
     one per shell. The expected states are authored, not generated, so a
     divergence means peel is wrong. `Verbatim` proves the inputs are real.
  C. FROZEN SURFACE    — whole-file goldens: the necessary frozen user surface
     (contracts + spec definitions + axioms) survives peel byte-for-byte while
     the proof layer is peeled. Uses the gate oracle; NegativeControls prove the
     oracle bites. Anchored to a pinned git ref, not the mutable worktree.
  D. FLOOR SAFETY      — the floor (axiom_*) is structurally safe under ALL
     three proof ops, not just fn-bodies admit.

Sections C and the Verbatim test read real source; they SKIP if the worktree is
absent. Needs Python 3.11+ (spec_check uses `int | None`).

Run: python3 -m unittest tests.test_peel
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills"))
import peel          # noqa: E402
import spec_check    # noqa: E402  (skills/spec_check.py — the gate oracle, as a module)
import lib.admits as admits  # noqa: E402


# ═════════════════════════════════════════════════════════════════════════════
# A. UNIT TRANSFORMS
# ═════════════════════════════════════════════════════════════════════════════

# A lemmas-style file: proof fns (admitted at P1), a helper lemma (deleted at
# P2), a spec fn (deleted at P3), an anchor with a contract (stripped at P4),
# an axiom_* (FLOOR — never touched), and an exec fn (FLOOR).
SRC = '''\
spec fn abstract_map(x: int) -> int {
    x + 1
}

/// helper
proof fn lemma_step(x: int)
    ensures abstract_map(x) == x + 1,
{
    assert(abstract_map(x) == x + 1);
}

pub proof fn anchor_thm(x: int)
    requires x > 0,
    ensures abstract_map(x) > 1,
{
    lemma_step(x);
}

pub proof fn axiom_trusted(x: int)
    ensures abstract_map(x) >= 0,
{
    assume(abstract_map(x) >= 0);
}

pub exec fn run(x: u64) -> u64 {
    x + 1
}
'''

# Path under /lemmas/ so the auto proof mode resolves to fn-bodies.
PATH = "curve25519-dalek/src/lemmas/edwards_lemmas/decompress_lemmas.rs"


def peel_at(depth, **kw):
    return peel.peel_file_text(SRC, depth, path=PATH, **kw)


class PeelDepths(unittest.TestCase):
    def test_p0_is_identity(self):
        out, rep = peel_at(0)
        self.assertEqual(out, SRC)
        self.assertEqual(rep["shells"], [])

    def test_p1_admits_proof_bodies(self):
        out, rep = peel_at(1)
        self.assertIn("admit()", out)
        self.assertNotIn("lemma_step(x);", out)
        self.assertIn("proof fn lemma_step", out)
        self.assertIn("spec fn abstract_map", out)
        self.assertIn("requires x > 0", out)
        self.assertEqual(rep["deleted"], [])
        self.assertEqual(rep["stripped"], [])
        self.assertEqual(rep["proof_mode"], "fn-bodies")

    def test_p2_deletes_lemma(self):
        out, rep = peel_at(2, lemmas=("lemma_step",))
        self.assertNotIn("proof fn lemma_step", out)
        self.assertIn("lemma_step", rep["deleted"])
        self.assertIn("spec fn abstract_map", out)
        self.assertIn("requires x > 0", out)

    def test_p2_drops_orphan_doc_comment_after_banner_separated_delete(self):
        src = '''\
verus! {
/// keep me even with a banner before the item
// =============================================================================
pub proof fn survivor()
{
}

/// deleted lemma doc separated from the item by a banner
// =============================================================================
pub proof fn deleted()
{
}
} // verus!
'''
        out, rep = peel.peel_file_text(
            src, 2, path=PATH, lemmas=("deleted",), proof_op="none")
        self.assertEqual(rep["deleted"], ["deleted"])
        self.assertIn("keep me even with a banner", out)
        self.assertIn("pub proof fn survivor", out)
        self.assertNotIn("deleted lemma doc", out)
        self.assertNotIn("pub proof fn deleted", out)

    def test_p3_deletes_spec_fn(self):
        out, rep = peel_at(3, lemmas=("lemma_step",), spec_fns=("abstract_map",))
        self.assertNotIn("spec fn abstract_map", out)
        self.assertNotIn("proof fn lemma_step", out)
        self.assertCountEqual(rep["deleted"], ["lemma_step", "abstract_map"])
        self.assertIn("requires x > 0", out)

    def test_p4_strips_contract(self):
        out, rep = peel_at(4, contract_fns=("anchor_thm",), proof_admit=False)
        self.assertNotIn("requires x > 0", out)
        self.assertNotIn("ensures abstract_map(x) > 1", out)
        self.assertIn("anchor_thm", rep["stripped"])
        self.assertIn("proof fn lemma_step", out)
        self.assertIn("spec fn abstract_map", out)
        self.assertIn("lemma_step(x);", out)  # anchor body NOT admitted (pin)

    def test_floor_survives_every_depth(self):
        for d in range(0, 5):
            kw = {}
            if d >= 2:
                kw["lemmas"] = ("lemma_step",)
            if d >= 3:
                kw["spec_fns"] = ("abstract_map",)
            if d >= 4:
                kw["contract_fns"] = ("anchor_thm",)
            out, _ = peel_at(d, **kw)
            self.assertIn("proof fn axiom_trusted", out, f"axiom gone at P{d}")
            self.assertIn("assume(abstract_map(x) >= 0)", out, f"axiom body gone at P{d}")
            self.assertIn("pub exec fn run", out, f"exec gone at P{d}")
            self.assertIn("x + 1", out, f"exec body gone at P{d}")

    def test_depth_is_monotone_on_deletions(self):
        _, r2 = peel_at(2, lemmas=("lemma_step",), spec_fns=("abstract_map",))
        _, r3 = peel_at(3, lemmas=("lemma_step",), spec_fns=("abstract_map",))
        self.assertTrue(set(r2["deleted"]).issubset(set(r3["deleted"])))
        self.assertIn("abstract_map", r3["deleted"])
        self.assertNotIn("abstract_map", r2["deleted"])

    def test_deterministic(self):
        a, _ = peel_at(3, lemmas=("lemma_step",), spec_fns=("abstract_map",))
        b, _ = peel_at(3, lemmas=("lemma_step",), spec_fns=("abstract_map",))
        self.assertEqual(a, b)

    def test_bad_depth_rejected(self):
        with self.assertRaises(ValueError):
            peel_at(5)


class PinRule(unittest.TestCase):
    def test_p4_requires_pin(self):
        with self.assertRaises(ValueError):
            peel._require_pin(4, None)

    def test_self_pinning_when_no_spec_deleted(self):
        for d in (0, 1, 2, 3):
            peel._require_pin(d, None)  # P1/P2 and a no-spec-delete P3 self-pin

    def test_p4_with_pin_ok(self):
        peel._require_pin(4, "proof")
        peel._require_pin(4, "consumer:to_edwards")
        peel._require_pin(4, "oracle:main")

    def test_spec_delete_requires_pin(self):
        with self.assertRaises(ValueError):
            peel._require_pin(3, None, deletes_spec=True)

    def test_spec_delete_with_pin_ok(self):
        peel._require_pin(3, "consumer:to_edwards", deletes_spec=True)
        peel._require_pin(3, "oracle:main", deletes_spec=True)


EXEC_SRC = '''\
pub fn decompress(x: u64) -> u64
    ensures result == x,
{
    let y = x;
    proof { lemma_a(x); assert(y == x); }
    y
}

pub fn sibling(x: u64) -> u64
    ensures result == x,
{
    let z = x;
    proof { lemma_b(x); }
    z
}
'''


class ProofShellVariants(unittest.TestCase):
    """admit (green/whole-file) vs strip (red/name-scoped) are different
    transforms, both expressible — not interchangeable forms of one op."""

    def test_admit_is_green_and_whole_file(self):
        out, rep = peel.peel_file_text(EXEC_SRC, 1, path="src/edwards.rs", proof_op="admit")
        self.assertEqual(out.count("admit()"), 2)
        self.assertIn("proof {", out)
        self.assertEqual(rep["proof_op"], "admit")

    def test_strip_is_red_and_name_scoped(self):
        out, rep = peel.peel_file_text(EXEC_SRC, 1, path="src/edwards.rs",
                                       proof_op="strip", strip_proof_fns=("decompress",))
        self.assertEqual(out.count("admit()"), 0)
        self.assertNotIn("lemma_a(x)", out)
        self.assertIn("let y = x;", out)
        self.assertIn("lemma_b(x)", out)  # sibling untouched
        self.assertEqual(rep["proof_op"], "strip")
        self.assertEqual(rep["proof_stripped"], ["decompress"])

    def test_strip_all_strips_every_fn(self):
        out, _ = peel.peel_file_text(EXEC_SRC, 1, path="src/edwards.rs", proof_op="strip-all")
        self.assertEqual(out.count("admit()"), 0)
        self.assertNotIn("lemma_a(x)", out)
        self.assertNotIn("lemma_b(x)", out)
        self.assertIn("let z = x;", out)

    def test_strip_consumes_assert_by_requires_tail(self):
        src = """\
pub fn digit(x: i8) -> i8
{
    let y = x;
    assert(y == x) by (bit_vector)
        requires
            -8i8 <= x,
            x <= 7i8,
    ;
    y
}
"""
        out, rep = peel.peel_file_text(src, 1, path="src/scalar.rs", proof_op="strip-all")
        self.assertEqual(rep["proof_stripped"], ["digit"])
        self.assertIn("let y = x;", out)
        self.assertIn("y\n}", out)
        self.assertNotIn("bit_vector", out)
        self.assertNotIn("requires", out)
        self.assertNotIn("-8i8 <= x", out)

    def test_strip_merges_overlapping_false_fn_spans(self):
        src = """\
pub fn pack() -> u64
{
    let result = 3u64;
    // recursive defn equals a polynomial
    proof {
        let ghost v = result;
        assert(v == result);
    }
    result
}
"""
        out, rep = peel.peel_file_text(src, 1, path="src/scalar.rs", proof_op="strip-all")
        self.assertIn("pack", rep["proof_stripped"])
        self.assertIn("let result = 3u64;", out)
        self.assertIn("result\n}", out)
        self.assertNotIn("let ghost v", out)
        self.assertNotIn("assert(v == result)", out)
        self.assertNotIn("proof {", out)

    def test_admit_vs_strip_diverge(self):
        a, _ = peel.peel_file_text(EXEC_SRC, 1, path="src/edwards.rs", proof_op="admit")
        s, _ = peel.peel_file_text(EXEC_SRC, 1, path="src/edwards.rs", proof_op="strip-all")
        self.assertNotEqual(a, s)

    def test_proof_admit_backcompat(self):
        out, rep = peel.peel_file_text(EXEC_SRC, 1, path="src/edwards.rs", proof_admit=False)
        self.assertEqual(rep["proof_op"], "none")
        self.assertEqual(out, EXEC_SRC)

    def test_bad_proof_op_rejected(self):
        with self.assertRaises(ValueError):
            peel.peel_file_text(EXEC_SRC, 1, path="x.rs", proof_op="bogus")


class Surface(unittest.TestCase):
    MANIFEST = {
        "name": "m", "pin": "proof",
        "files": [
            {"path": "a/lemmas.rs", "lemmas": ["l1", "l2"]},
            {"path": "b/specs.rs", "spec_fns": ["s1"]},
            {"path": "src/edwards.rs", "proof_admit": True, "contract_fns": ["decompress"]},
        ],
    }

    def test_editable_is_all_listed_files(self):
        s = peel.peel_surface(self.MANIFEST, 1)
        self.assertEqual(s["editable_files"], ["a/lemmas.rs", "b/specs.rs", "src/edwards.rs"])

    def test_shells_grow_with_depth(self):
        self.assertEqual(peel.peel_surface(self.MANIFEST, 1)["files"][0]["delete_lemmas"], [])
        self.assertEqual(peel.peel_surface(self.MANIFEST, 2)["files"][0]["delete_lemmas"], ["l1", "l2"])
        self.assertEqual(peel.peel_surface(self.MANIFEST, 2)["files"][1]["delete_spec_fns"], [])
        self.assertEqual(peel.peel_surface(self.MANIFEST, 3)["files"][1]["delete_spec_fns"], ["s1"])
        self.assertEqual(peel.peel_surface(self.MANIFEST, 3)["files"][2]["strip_contract"], [])
        self.assertEqual(peel.peel_surface(self.MANIFEST, 4)["files"][2]["strip_contract"], ["decompress"])

    def test_surface_does_not_mutate(self):
        before = json.dumps(self.MANIFEST, sort_keys=True)
        peel.peel_surface(self.MANIFEST, 4)
        self.assertEqual(before, json.dumps(self.MANIFEST, sort_keys=True))


class ClassifyCone(unittest.TestCase):
    def test_nonaxiom_proof_fns_excludes_axioms(self):
        text = ("proof fn lemma_a() {}\n"
                "pub proof fn axiom_b() {}\n"
                "proof fn lemma_c() {}\n"
                "spec fn s() -> int { 0 }\n")
        self.assertEqual(peel._nonaxiom_proof_fns(text), ["lemma_a", "lemma_c"])

    def _project_with_field_layer(self, root):
        project = Path(root) / "curve25519-dalek"
        files = {
            "src/lemmas/edwards_lemmas/curve.rs": (
                "proof fn lemma_curve() {}\n"
                "proof fn axiom_curve() {}\n"
            ),
            "src/lemmas/field_lemmas/add.rs": (
                "proof fn lemma_field() {}\n"
                "proof fn axiom_field() {}\n"
            ),
            "src/backend/serial/u64/field.rs": (
                "pub fn add() { proof { assert(true); } }\n"
            ),
            "src/backend/serial/u64/scalar.rs": (
                "pub fn scalar() { proof { assert(true); } }\n"
            ),
            "src/lemmas/common_lemmas/mul.rs": (
                "proof fn lemma_common() {}\n"
            ),
            "src/specs/field_specs.rs": (
                "spec fn field_add(x: nat, y: nat) -> nat { x + y }\n"
                "proof fn lemma_field_spec() {}\n"
                "proof fn axiom_field_spec() {}\n"
            ),
            "src/edwards.rs": "pub fn decompress() { proof { assert(true); } }\n",
        }
        for rel, text in files.items():
            p = project / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return project

    def test_classify_default_keeps_field_floor_frozen(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._project_with_field_layer(td)
            manifest = peel.classify_cone(project)
            paths = {f["path"] for f in manifest["files"]}

        self.assertEqual(manifest["name"], "field-floor-cut")
        self.assertEqual(manifest["floor"], "field")
        self.assertIn("curve25519-dalek/src/lemmas/edwards_lemmas/curve.rs", paths)
        self.assertIn("curve25519-dalek/src/edwards.rs", paths)
        self.assertNotIn("curve25519-dalek/src/lemmas/field_lemmas/add.rs", paths)
        self.assertNotIn("curve25519-dalek/src/backend/serial/u64/field.rs", paths)

    def test_classify_number_theory_floor_strips_through_fields(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._project_with_field_layer(td)
            manifest = peel.classify_cone(project, floor="number-theory")
            by_path = {f["path"]: f for f in manifest["files"]}

        self.assertEqual(manifest["name"], "number-theory-floor-cut")
        self.assertEqual(manifest["floor"], "number-theory")
        self.assertEqual(
            by_path["curve25519-dalek/src/lemmas/field_lemmas/add.rs"]["lemmas"],
            ["lemma_field"],
        )
        self.assertEqual(
            by_path["curve25519-dalek/src/backend/serial/u64/field.rs"]["proof_op"],
            "strip-all",
        )
        self.assertNotIn(
            "curve25519-dalek/src/backend/serial/u64/scalar.rs", by_path)

    def test_classify_trusted_core_strips_all_in_repo_proofs(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._project_with_field_layer(td)
            manifest = peel.classify_cone(project, floor="trusted-core")
            by_path = {f["path"]: f for f in manifest["files"]}

        self.assertEqual(manifest["name"], "trusted-core-cut")
        self.assertEqual(manifest["floor"], "trusted-core")
        self.assertEqual(
            by_path["curve25519-dalek/src/lemmas/common_lemmas/mul.rs"]["lemmas"],
            ["lemma_common"],
        )
        self.assertEqual(
            by_path["curve25519-dalek/src/specs/field_specs.rs"]["lemmas"],
            ["lemma_field_spec"],
        )
        self.assertEqual(
            by_path["curve25519-dalek/src/backend/serial/u64/scalar.rs"]["proof_op"],
            "strip-all",
        )
        self.assertIn("curve25519-dalek/src/lemmas/field_lemmas/add.rs", by_path)

    def test_trusted_core_peels_proof_bearing_mod_rs(self):
        # Regression: a `mod.rs` with real inline proof scaffolding must be
        # peeled (curve_models/mod.rs in the real tree). The classifier
        # content-gates rather than skipping every `mod.rs` by name.
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "curve25519-dalek"
            files = {
                # proof-bearing mod.rs → editable (strip-all)
                "src/backend/serial/curve_models/mod.rs":
                    "pub fn dbl() { proof { assert(true); } }\n",
                # true-glue mod.rs → frozen (no removable proof content)
                "src/backend/serial/mod.rs": "pub use self::u64;\n",
                # pure-axiom file → frozen even if an axiom body has scaffolding;
                # strip_proof_from_fns name-skips axiom_*, so nothing is removable
                "src/lemmas/ristretto_lemmas/axioms.rs":
                    "pub proof fn axiom_x() { assert(true); }\n",
                "src/lemmas/common_lemmas/mul.rs": "proof fn lemma_c() {}\n",
            }
            for rel, text in files.items():
                p = project / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            manifest = peel.classify_cone(project, floor="trusted-core")
            by_path = {f["path"]: f for f in manifest["files"]}

        cm = "curve25519-dalek/src/backend/serial/curve_models/mod.rs"
        self.assertIn(cm, by_path)
        self.assertEqual(by_path[cm]["proof_op"], "strip-all")
        self.assertNotIn("curve25519-dalek/src/backend/serial/mod.rs", by_path)
        self.assertNotIn(
            "curve25519-dalek/src/lemmas/ristretto_lemmas/axioms.rs", by_path)

    def test_classify_rejects_unknown_floor(self):
        with tempfile.TemporaryDirectory() as td:
            project = self._project_with_field_layer(td)
            with self.assertRaises(ValueError):
                peel.classify_cone(project, floor="basement")


# ═════════════════════════════════════════════════════════════════════════════
# B. GOLDEN FRAGMENTS — hand-written exact before/after on verbatim real code
# ═════════════════════════════════════════════════════════════════════════════

_SRC = "/private/tmp/dalek-spec-strip/curve25519-dalek/src"

P_CURVELEMMAS = "curve25519-dalek/src/lemmas/edwards_lemmas/curve_equation_lemmas.rs"
P_MONTGOMERY = "curve25519-dalek/src/montgomery.rs"
P_EDWARDS = "curve25519-dalek/src/edwards.rs"
P_RISTRETTO = "curve25519-dalek/src/ristretto.rs"

# 1. curve_equation_lemmas.rs :: lemma_double_distributes  (/lemmas/ → fn-bodies)
#    The `({...}) == ({...})` ensures stresses the body-finder: it must skip the
#    brace pairs INSIDE the ensures and admit only the real body.
CURVE_BEFORE = """\
pub proof fn lemma_double_distributes(a: (nat, nat), b: (nat, nat))
    ensures
        ({
            let ab = edwards_add(a.0, a.1, b.0, b.1);
            edwards_double(ab.0, ab.1)
        }) == ({
            let da = edwards_double(a.0, a.1);
            let db = edwards_double(b.0, b.1);
            edwards_add(da.0, da.1, db.0, db.1)
        }),
{
    let ab = edwards_add(a.0, a.1, b.0, b.1);
    lemma_double_is_scalar_mul_2(ab);
    lemma_double_is_scalar_mul_2(a);
    lemma_double_is_scalar_mul_2(b);
    axiom_edwards_scalar_mul_distributive(a, b, 2);
}"""
CURVE_P1 = """\
pub proof fn lemma_double_distributes(a: (nat, nat), b: (nat, nat))
    ensures
        ({
            let ab = edwards_add(a.0, a.1, b.0, b.1);
            edwards_double(ab.0, ab.1)
        }) == ({
            let da = edwards_double(a.0, a.1);
            let db = edwards_double(b.0, b.1);
            edwards_add(da.0, da.1, db.0, db.1)
        }),
{
    admit()
}"""
CURVE_P2 = ""  # whole lemma deleted

# 2. montgomery.rs :: identity  (→ proof-blocks) — the admit-vs-strip case
MONT_BEFORE = """\
    fn identity() -> (result: MontgomeryPoint)
        ensures
            montgomery_point_as_nat(result) == 0,
    {
        let result = MontgomeryPoint([0u8;32]);
        proof {
            assert forall|i: int| 0 <= i < 32 implies #[trigger] result.0[i] == 0u8 by {}
            assert(montgomery_point_as_nat(result) == 0) by {
                lemma_zero_limbs_is_zero(result);
            }
        }
        result
    }"""
MONT_P1_ADMIT = """\
    fn identity() -> (result: MontgomeryPoint)
        ensures
            montgomery_point_as_nat(result) == 0,
    {
        let result = MontgomeryPoint([0u8;32]);
        proof { admit(); }
        result
    }"""
MONT_P1_STRIP = """\
    fn identity() -> (result: MontgomeryPoint)
        ensures
            montgomery_point_as_nat(result) == 0,
    {
        let result = MontgomeryPoint([0u8;32]);
        result
    }"""

# 3. edwards.rs :: default  (shell C, P4 contract strip)
EDW_BEFORE = """\
    fn default() -> (result: EdwardsPoint)
        ensures
            is_identity_edwards_point(result),
    {
        EdwardsPoint::identity()
    }"""
# QUIRK (caught by this golden): strip_text leaves the stripped `ensures` line's
# 8-space indent on the body brace, so the brace is `        {` not the clean
# `    {`. Cosmetic — Verus ignores indentation — and PRE-EXISTING strip_text
# behavior the live rungs already produce, NOT a peel regression. If strip_text
# is ever tidied, set EDW_P4 == EDW_P4_CLEAN.
EDW_P4 = (
    "    fn default() -> (result: EdwardsPoint)\n"
    "        {\n"
    "        EdwardsPoint::identity()\n"
    "    }")
EDW_P4_CLEAN = """\
    fn default() -> (result: EdwardsPoint)
    {
        EdwardsPoint::identity()
    }"""

# 4. ristretto.rs :: eq_spec  (shell D, P3 spec delete)
RIST_BEFORE = """\
    open spec fn eq_spec(&self, other: &Self) -> bool {
        ristretto_equivalent(self.0, other.0)
    }"""
RIST_P3 = ""  # spec fn deleted


def out(text, depth, path, **kw):
    return peel.peel_file_text(text, depth, path=path, **kw)[0]


class GoldenCurvelemmas(unittest.TestCase):
    def test_p0_identity(self):
        self.assertEqual(out(CURVE_BEFORE, 0, P_CURVELEMMAS), CURVE_BEFORE)

    def test_p1_admits_body_keeps_header(self):
        self.assertEqual(out(CURVE_BEFORE, 1, P_CURVELEMMAS), CURVE_P1)

    def test_p2_deletes_whole_lemma(self):
        self.assertEqual(out(CURVE_BEFORE, 2, P_CURVELEMMAS,
                             lemmas=("lemma_double_distributes",)), CURVE_P2)

    def test_p3_p4_still_deleted(self):
        for d in (3, 4):
            self.assertEqual(out(CURVE_BEFORE, d, P_CURVELEMMAS,
                                 lemmas=("lemma_double_distributes",)), CURVE_P2)


class GoldenMontgomery(unittest.TestCase):
    def test_p1_admit_is_green(self):
        self.assertEqual(out(MONT_BEFORE, 1, P_MONTGOMERY, proof_op="admit"), MONT_P1_ADMIT)

    def test_p1_strip_is_red(self):
        self.assertEqual(out(MONT_BEFORE, 1, P_MONTGOMERY, proof_op="strip",
                             strip_proof_fns=("identity",)), MONT_P1_STRIP)

    def test_admit_and_strip_actually_diverge(self):
        a = out(MONT_BEFORE, 1, P_MONTGOMERY, proof_op="admit")
        s = out(MONT_BEFORE, 1, P_MONTGOMERY, proof_op="strip", strip_proof_fns=("identity",))
        self.assertNotEqual(a, s)
        self.assertIn("admit()", a)
        self.assertNotIn("admit()", s)


class GoldenEdwardsContract(unittest.TestCase):
    def test_p3_keeps_contract(self):
        self.assertEqual(out(EDW_BEFORE, 3, P_EDWARDS, proof_admit=False), EDW_BEFORE)

    def test_p4_strips_contract_keeps_body(self):
        self.assertEqual(out(EDW_BEFORE, 4, P_EDWARDS, contract_fns=("default",),
                             proof_admit=False), EDW_P4)

    def test_p4_clean_modulo_indentation(self):
        got = out(EDW_BEFORE, 4, P_EDWARDS, contract_fns=("default",), proof_admit=False)
        norm = lambda s: "\n".join(l.strip() for l in s.splitlines())
        self.assertEqual(norm(got), norm(EDW_P4_CLEAN))


class GoldenRistrettoSpec(unittest.TestCase):
    def test_p1_p2_leave_spec_untouched(self):
        for d in (0, 1, 2):
            self.assertEqual(out(RIST_BEFORE, d, P_RISTRETTO), RIST_BEFORE)

    def test_p3_deletes_spec_fn(self):
        self.assertEqual(out(RIST_BEFORE, 3, P_RISTRETTO, spec_fns=("eq_spec",)), RIST_P3)


class GoldenVerbatim(unittest.TestCase):
    """Prove the BEFORE fragments are REAL code: each must be an exact substring
    of its source file. Skipped when the worktree is absent."""

    CASES = [
        (CURVE_BEFORE, "lemmas/edwards_lemmas/curve_equation_lemmas.rs"),
        (MONT_BEFORE, "montgomery.rs"),
        (EDW_BEFORE, "edwards.rs"),
        (RIST_BEFORE, "ristretto.rs"),
    ]

    @unittest.skipUnless(os.path.isdir(_SRC), f"source worktree {_SRC} absent")
    def test_fragments_are_verbatim(self):
        for frag, rel in self.CASES:
            with open(os.path.join(_SRC, rel)) as f:
                src = f.read()
            self.assertIn(frag, src, f"{rel}: fragment not verbatim in source")


# ═════════════════════════════════════════════════════════════════════════════
# C. FROZEN SURFACE — whole-file: the necessary frozen user surface survives peel
# ═════════════════════════════════════════════════════════════════════════════
#
# FROZEN SURFACE (necessary; the agent must not be able to weaken it):
#   1. CONTRACT  — every requires/ensures/decreases on the API exec fns.
#   2. SPEC DEFS — every `spec fn` BODY (the vocabulary the contract is phrased
#                  in). A frozen contract does NOT pin a definition it delegates
#                  to: `is_valid := true` makes `ensures is_valid(r)` vacuous.
#   3. FLOOR     — every `axiom_*` body + assume/external_body + exec statements.
# PEELED LAYER: inline proof{} (P1) + standalone helper lemmas (P2-delete).
# Scope: the SELF-PINNING depths (P1/P2). P3/P4 cut INTO the surface (need a pin).

_WT = "/private/tmp/dalek-spec-strip"
_REF = "103b92b9"     # pinned clean proven main — deterministic, not the live tree

API_FILES = [
    "curve25519-dalek/src/edwards.rs",
    "curve25519-dalek/src/ristretto.rs",
    "curve25519-dalek/src/montgomery.rs",
]
LEMMA_FILE = "curve25519-dalek/src/lemmas/edwards_lemmas/curve_equation_lemmas.rs"


def _read_ref(relpath):
    r = subprocess.run(
        ["git", "-C", _WT, "-c", f"safe.directory={_WT}", "show", f"{_REF}:{relpath}"],
        capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


_HAVE_REPO = _read_ref(API_FILES[0]) is not None


def _surface_violations(orig, peeled):
    """Gate-oracle drift split into frozen-surface VIOLATIONS (contract edit,
    spec-body change, external_body added, axiom/spec removal) vs the expected
    removed proof-fn lemmas (the peeled layer)."""
    o = spec_check._extract_sigs(orig)
    c = spec_check._extract_sigs(peeled)
    drift = spec_check._verify_one("f", o, c, check_spec_defs=True)
    violations, removed_lemmas = [], []
    for d in drift:
        ch, name = d["change"], d.get("function")
        if ch == "removed":
            sig = o.get(name, {})
            if name.startswith("axiom_") or sig.get("mode") == "spec":
                violations.append(("removed-frozen", name))
            else:
                removed_lemmas.append(name)
        else:
            violations.append((ch, name))
    return violations, removed_lemmas


@unittest.skipUnless(_HAVE_REPO, f"repo {_WT}@{_REF} absent")
class FrozenSurfaceUnderProofPeel(unittest.TestCase):
    """API files, P1: peel inline proofs (admit AND strip-all). The frozen
    surface (contracts + all spec defs + axioms) stays intact."""

    def _check(self, rel, proof_op, expect_admits):
        t = _read_ref(rel)
        peeled, _ = peel.peel_file_text(t, 1, path=rel, proof_op=proof_op)
        viol, removed = _surface_violations(t, peeled)
        self.assertEqual(viol, [], f"{rel} [{proof_op}]: frozen surface violated")
        self.assertEqual(removed, [], f"{rel} [{proof_op}]: unexpectedly deleted fns")
        if expect_admits:
            self.assertGreater(peeled.count("admit()"), 0, f"{rel}: nothing admitted")
        else:
            self.assertEqual(peeled.count("admit()"), 0, f"{rel}: strip should seed 0 admits")
        self.assertEqual(re.findall(r"\bassume\s*\(", t), re.findall(r"\bassume\s*\(", peeled))

    def test_admit_green_keeps_surface(self):
        for rel in API_FILES:
            with self.subTest(file=rel):
                self._check(rel, "admit", expect_admits=True)

    def test_strip_all_red_keeps_surface(self):
        for rel in API_FILES:
            with self.subTest(file=rel):
                self._check(rel, "strip-all", expect_admits=False)


@unittest.skipUnless(_HAVE_REPO, f"repo {_WT}@{_REF} absent")
class FrozenSurfaceUnderLemmaDelete(unittest.TestCase):
    """Lemma file, P2: delete the whole proof layer; only non-axiom proof fns
    may go — axioms (floor) and any spec defs stay frozen."""

    def test_delete_peels_only_proof_layer(self):
        t = _read_ref(LEMMA_FILE)
        lemmas = tuple(peel._nonaxiom_proof_fns(t))
        axioms_before = set(re.findall(r"\bfn\s+(axiom_\w+)", t))
        self.assertTrue(lemmas, "expected helper lemmas to delete")

        peeled, _ = peel.peel_file_text(t, 2, path=LEMMA_FILE, lemmas=lemmas, proof_op="none")
        viol, removed = _surface_violations(t, peeled)

        self.assertEqual(viol, [], "lemma delete violated the frozen surface")
        self.assertCountEqual(removed, list(lemmas))
        self.assertEqual(axioms_before, set(re.findall(r"\bfn\s+(axiom_\w+)", peeled)))


@unittest.skipUnless(_HAVE_REPO, f"repo {_WT}@{_REF} absent")
class NegativeControls(unittest.TestCase):
    """Prove the oracle BITES, so a green frozen-surface result is meaningful."""

    def test_weakened_spec_def_is_a_violation(self):
        t = _read_ref(API_FILES[0])
        m = re.search(r"(open spec fn \w+_spec\([^)]*\)\s*->\s*bool\s*)\{", t)
        self.assertIsNotNone(m)
        i = m.end() - 1
        depth = 0
        while True:
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        weakened = t[:m.end()] + " true " + t[i:]
        viol, _ = _surface_violations(t, weakened)
        self.assertTrue(any(c == "spec_def_modified" for c, _ in viol),
                        "oracle FAILED to catch a hollowed spec definition")

    def test_p4_contract_strip_is_a_violation(self):
        t = _read_ref(API_FILES[0])
        self.assertIn("fn default() -> (result: EdwardsPoint)", t)
        peeled, _ = peel.peel_file_text(t, 4, path=API_FILES[0],
                                        contract_fns=("default",), proof_admit=False)
        viol, _ = _surface_violations(t, peeled)
        self.assertTrue(any(c == "modified" for c, _ in viol),
                        "oracle FAILED to catch a P4 contract strip")


# Spec-gate unit tests (duplicate-name keying, spec-def freeze) live in their
# own file: tests/test_spec_check.py. test_peel.py keeps only the peel↔gate
# INTEGRATION coverage above (FrozenSurface*, NegativeControls).


# ═════════════════════════════════════════════════════════════════════════════
# D. FLOOR SAFETY — axioms structurally untouchable under every proof op
# ═════════════════════════════════════════════════════════════════════════════

class FloorSafety(unittest.TestCase):
    """The floor (`axiom_*`) must survive ALL proof ops, not just fn-bodies
    admit. The risk: `strip-all` auto-names every fn (axioms included), and an
    axiom *could* carry strippable body content. `strip_proof_from_fns` now
    name-skips `axiom_*`, so floor-safety is STRUCTURAL, not contingent on
    axiom bodies being inert. (A real axiom is `{ admit() }` with nothing to
    strip — these synthetic axioms carry a proof{} to make the guard observable.)"""

    AXIOMY = ("proof fn axiom_evil()\n    ensures true,\n{\n    proof { sneaky(); }\n}\n"
              "\nproof fn lemma_real()\n    ensures true,\n{\n    proof { honest(); }\n}\n")

    def test_strip_all_skips_axiom_keeps_lemma(self):
        out, _ = peel.peel_file_text(self.AXIOMY, 1, path="x.rs", proof_op="strip-all")
        self.assertIn("sneaky()", out, "strip-all touched an axiom body")
        self.assertNotIn("honest()", out, "strip-all should strip the real lemma")

    def test_strip_skips_axiom_even_when_named(self):
        out, _ = peel.peel_file_text(self.AXIOMY, 1, path="x.rs", proof_op="strip",
                                     strip_proof_fns=("axiom_evil", "lemma_real"))
        self.assertIn("sneaky()", out, "explicitly-named axiom was stripped")
        self.assertNotIn("honest()", out)

    def test_fn_bodies_admit_skips_axiom(self):
        # The pre-existing structural guard on the admit side (admit.py).
        out = admits.admit_proof_fn_bodies(
            "proof fn axiom_x()\n    ensures true,\n{\n    real_axiom_body();\n}\n")
        self.assertIn("real_axiom_body()", out, "fn-bodies admit hollowed an axiom")


if __name__ == "__main__":
    unittest.main()
