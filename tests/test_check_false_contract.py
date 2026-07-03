"""Unit tests for the E7 witness-checker's pure logic (skills/check_false_contract.py).

The verus-dependent path is validated by real runs (a synthetic false contract
→ verified; the dalek cases → unconfirmed without hints). Here we pin the
parsing + snippet construction, including the soundness-critical property that
the predicate comes from the snapshot sig, never from caller-supplied text.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_P = Path(__file__).resolve().parents[1] / "skills" / "check_false_contract.py"
_spec = importlib.util.spec_from_file_location("cfc", _P)
cfc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfc)


class SplitTopLevel(unittest.TestCase):
    def test_respects_paren_depth(self):
        self.assertEqual(
            cfc._split_top_level("a < 10, f(x, y), b == c + (d, e)"),
            ["a < 10", "f(x, y)", "b == c + (d, e)"])


class ParamParsing(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(
            cfc._params_from_header("proof fn f(x: nat, y: nat) requires x == 0,"),
            [("x", "nat"), ("y", "nat")])

    def test_multiline_and_u128(self):
        h = ("pub(crate) proof fn lemma_carry8_bound( limb8: u128, n4: u64, "
             "carry8: u128, ) requires limb8 < 1,")
        names = [n for n, _ in cfc._params_from_header(h)]
        self.assertEqual(names, ["limb8", "n4", "carry8"])

    def test_self_is_skipped_as_param_but_flagged_in_build(self):
        sig = {"header": "proof fn f(self, a: nat) requires a < 1, ensures a < 0,",
               "requires": "a < 1, ensures a < 0,", "ensures": "a < 0,"}
        body, reason = cfc.build_check_fn(sig, {"a": "0"})
        self.assertIsNone(body)
        self.assertIn("self", reason)


class CleanRequires(unittest.TestCase):
    def test_strips_ensures_tail(self):
        # Defensive compatibility for snapshots made before spec_check stopped
        # over-capturing later clauses into `requires`.
        self.assertEqual(
            cfc._clean_requires("a < 10, b == c, ensures a < 5,"),
            "a < 10, b == c")


class ModuleScoping(unittest.TestCase):
    def test_derive_module_from_project_relative_file(self):
        project = Path("/tmp/dalek/curve25519-dalek")
        self.assertEqual(
            cfc._derive_module(
                project / "src/lemmas/ristretto_lemmas/coset_lemmas.rs",
                project),
            "lemmas::ristretto_lemmas::coset_lemmas")
        self.assertEqual(
            cfc._derive_module(project / "src/ristretto.rs", project),
            "ristretto")
        self.assertEqual(
            cfc._derive_module(project / "src/foo/mod.rs", project),
            "foo")

    def test_derive_module_rejects_outside_project(self):
        self.assertIsNone(cfc._derive_module(
            Path("/tmp/other/src/lib.rs"), Path("/tmp/dalek/curve25519-dalek")))

    def test_cargo_verus_cmd_scopes_to_module(self):
        cmd = cfc._cargo_verus_cmd(Path("/tmp/missing-project"),
                                   "lemmas::ristretto_lemmas::coset_lemmas",
                                   30.0)
        self.assertIn("--verify-module", cmd)
        self.assertIn("lemmas::ristretto_lemmas::coset_lemmas", cmd)
        self.assertIn("--rlimit", cmd)
        self.assertNotIn("-p", cmd)


class BuildCheckFn(unittest.TestCase):
    SIG = {"header": "proof fn f(x: nat, y: nat) requires is_curve(x,y), x == 0,",
           "requires": "is_curve(x,y), x == 0, ensures y == 1,",
           "ensures": "y == 1,"}

    def test_builds_asserts_for_requires_and_negated_ensures(self):
        body, reason = cfc.build_check_fn(self.SIG, {"x": "0", "y": "p() + 1"})
        self.assertIsNone(reason)
        self.assertRegex(body, r"proof fn _fc_[A-Za-z0-9_]+_witness_check\(\)")
        self.assertIn("let x: nat = 0;", body)
        self.assertIn("let y: nat = p() + 1;", body)
        self.assertIn("assert(is_curve(x,y));", body)
        self.assertIn("assert(x == 0);", body)
        self.assertIn("assert(!((y == 1)));", body)  # ensures negated

    def test_build_can_use_nonce_marker_name(self):
        body, reason = cfc.build_check_fn(
            self.SIG, {"x": "0", "y": "p() + 1"},
            marker_name="_fc_resume9_witness_check")
        self.assertIsNone(reason)
        self.assertIn("proof fn _fc_resume9_witness_check()", body)

    def test_multi_ensures_negates_the_conjunction(self):
        # Postcondition is e1 ∧ e2; false iff ¬(e1 ∧ e2). Must NOT emit the
        # invalid `assert(!(e1, e2))`.
        sig = {"header": "proof fn f(x: nat) requires x < 10,",
               "requires": "x < 10, ensures x == 1, x == 2,",
               "ensures": "x == 1, x == 2,"}
        body, reason = cfc.build_check_fn(sig, {"x": "7"})
        self.assertIsNone(reason)
        self.assertIn("assert(!((x == 1) && (x == 2)));", body)
        self.assertNotIn("!(x == 1, x == 2)", body)

    def test_missing_witness_value_is_unconfirmed(self):
        body, reason = cfc.build_check_fn(self.SIG, {"x": "0"})  # no y
        self.assertIsNone(body)
        self.assertIn("missing", reason)

    def test_witness_breakouts_are_rejected(self):
        # Witness values are injected into `let x: T = <value>;`, so they must be
        # single closed expressions, not statement snippets.
        for value in ("0); assume(false); let _ = (0", "0; admit();",
                      "0 /* comment", "{}", "0\n+ 1"):
            with self.subTest(value=value):
                body, reason = cfc.build_check_fn(self.SIG, {"x": value, "y": "1"})
                self.assertIsNone(body, value)
                self.assertRegex(reason, "closed expression|comment|forbidden")

    def test_witness_forbidden_tokens_are_rejected(self):
        for value in ("assume(false)", "admit()", "#[verifier::external_body]",
                      "assume_specification [f] (...)"):
            with self.subTest(value=value):
                body, reason = cfc.build_check_fn(self.SIG, {"x": value, "y": "1"})
                self.assertIsNone(body, value)
                self.assertIn("forbidden", reason)

    def test_witness_requires_balanced_delimiters(self):
        body, reason = cfc.build_check_fn(self.SIG, {"x": "p() + (1", "y": "1"})
        self.assertIsNone(body)
        self.assertIn("unbalanced", reason)

    def test_witness_values_must_be_strings(self):
        body, reason = cfc.build_check_fn(self.SIG, {"x": 0, "y": "1"})
        self.assertIsNone(body)
        self.assertIn("string expression", reason)

    def test_predicate_comes_from_sig_not_witness(self):
        # The witness dict cannot smuggle in a weakened predicate — build only
        # ever reads requires/ensures from the sig.
        body, _ = cfc.build_check_fn(
            self.SIG, {"x": "0", "y": "1", "ensures": "false", "requires": "true"})
        self.assertIn("assert(!((y == 1)));", body)   # sig's ensures, not "false"
        self.assertNotIn("assert(!(false))", body)


class NormalizeWitness(unittest.TestCase):
    def test_dict_witness_is_used_as_is(self):
        sig = {"header": "proof fn f(x: nat) ensures x == 0,"}
        witness = {"x": "0"}
        normalized, reason = cfc._normalize_witness(sig, witness)
        self.assertIs(normalized, witness)
        self.assertIsNone(reason)

    def test_legacy_scalar_witness_wraps_for_one_param(self):
        sig = {"header": "proof fn f(a: nat) ensures a == 0,"}
        normalized, reason = cfc._normalize_witness(sig, "p()")
        self.assertEqual(normalized, {"a": "p()"})
        self.assertIsNone(reason)

    def test_legacy_scalar_witness_rejected_for_multi_param(self):
        sig = {"header": "proof fn f(a: nat, b: nat) ensures a == b,"}
        normalized, reason = cfc._normalize_witness(sig, "p()")
        self.assertIsNone(normalized)
        self.assertIn("JSON object", reason)
        self.assertIn("one-param", reason)

    def test_non_string_scalar_witness_rejected_for_one_param(self):
        sig = {"header": "proof fn f(a: nat) ensures a == 0,"}
        normalized, reason = cfc._normalize_witness(sig, 0)
        self.assertIsNone(normalized)
        self.assertIn("string expression", reason)


class StripFcMarkers(unittest.TestCase):
    """Fix 1: a leaked injection (SIGKILLed restore) must self-heal on the next
    run, not poison the shared worktree with a duplicate-definition compile error."""

    def test_strips_both_injected_fns(self):
        leaked = (
            "verus! {\n\n"
            "pub proof fn real_lemma() { }\n\n"
            "proof fn _fc_resume9_witness_check() {\n"
            "        let n: nat = 7;\n"
            "        assert(n < 10);\n"
            "        assert(!((n < 5)));\n"
            "    }\n\n"
            "proof fn _fc_resume9_tripwire() {\n"
            "        assert(false);\n"
            "    }\n\n"
            "} // verus!\n"
        )
        out = cfc._strip_fc_markers(leaked)
        self.assertNotIn("_fc_resume9_witness_check", out)
        self.assertNotIn("_fc_resume9_tripwire", out)
        self.assertIn("pub proof fn real_lemma() { }", out)   # real code preserved
        self.assertIn("} // verus!", out)

    def test_strips_legacy_fixed_markers_and_agent_scratch(self):
        txt = (
            "proof fn _fc_witness_check() {\n    assert(true);\n}\n"
            "proof fn _fc_agent_probe() {\n    assert(false);\n}\n"
            "pub proof fn keep() { }\n")
        out = cfc._strip_fc_markers(txt)
        self.assertNotIn("_fc_witness_check", out)
        self.assertNotIn("_fc_agent_probe", out)
        self.assertIn("pub proof fn keep() { }", out)

    def test_idempotent_when_no_markers(self):
        clean = "verus! {\npub proof fn real_lemma() { }\n} // verus!\n"
        self.assertEqual(cfc._strip_fc_markers(clean), clean)

    def test_strips_a_double_leak(self):
        # two leaked tripwires (two SIGKILLed runs) → both removed
        txt = ("proof fn _fc_tripwire() {\n    assert(false);\n}\n"
               "proof fn _fc_tripwire() {\n    assert(false);\n}\n"
               "pub proof fn keep() { }\n")
        out = cfc._strip_fc_markers(txt)
        self.assertNotIn("_fc_tripwire", out)
        self.assertIn("pub proof fn keep() { }", out)

    def test_does_not_strip_marker_looking_comment(self):
        txt = (
            "verus! {\n"
            "// proof fn _fc_tripwire() { example text }\n"
            "pub proof fn keep() { }\n"
            "} // verus!\n"
        )
        self.assertEqual(cfc._strip_fc_markers(txt), txt)

    def test_unbalanced_marker_is_left_unchanged(self):
        txt = (
            "verus! {\n"
            "proof fn _fc_tripwire() {\n"
            "    assert(false);\n"
            "pub proof fn keep() { }\n"
            "} // verus!\n"
        )
        self.assertEqual(cfc._strip_fc_markers(txt), txt)


class RustcCompileFailed(unittest.TestCase):
    """Fix 2: a rustc/parse compile error must not be misread as a verus
    assertion result (the bug that mislabeled carry8 as witness_assert_failed)."""

    def test_e_code_is_compile_failure(self):
        self.assertTrue(cfc._rustc_compile_failed(
            "error[E0428]: `_fc_tripwire` must be defined only once\n"
            "error: could not compile `curve25519-dalek`"))
        self.assertTrue(cfc._rustc_compile_failed("error[E0308]: mismatched types"))

    def test_unclosed_delimiter_is_compile_failure(self):
        self.assertTrue(cfc._rustc_compile_failed(
            "error: this file contains an unclosed delimiter\n  315 | } // verus!"))

    def test_verus_assertion_failure_is_not_compile_failure(self):
        # a real verus semantic failure ALSO says "could not compile" but carries
        # no E-code → must NOT be treated as a build failure.
        self.assertFalse(cfc._rustc_compile_failed(
            "error: assertion failed\n  --> src/lib.rs:41:16\n"
            "error: could not compile `fc_postest` (lib) due to 1 previous error"))

    def test_clean_stderr_is_not_compile_failure(self):
        self.assertFalse(cfc._rustc_compile_failed(
            "verification results:: 3 verified, 0 errors"))


class RegionBucketing(unittest.TestCase):
    def test_outside_ranges_finds_foreign_errors(self):
        self.assertEqual(
            cfc._outside_ranges({9, 10, 12, 20, 30}, range(10, 13), range(20, 22)),
            {9, 30})

    def test_outside_ranges_accepts_clean_injected_region_only(self):
        self.assertEqual(
            cfc._outside_ranges({10, 12, 20}, range(10, 13), range(20, 22)),
            set())

    def test_module_not_clean_overrides_line_bucket_signals(self):
        self.assertEqual(
            cfc._classify_checked_region(
                compile_failed=False,
                module_not_clean=True,
                tripwire_fired=True,
                witness_failed=False),
            (False, "module_not_clean"))
        self.assertEqual(
            cfc._classify_checked_region(
                compile_failed=False,
                module_not_clean=True,
                tripwire_fired=True,
                witness_failed=True),
            (False, "module_not_clean"))

    def test_clean_region_still_verifies(self):
        self.assertEqual(
            cfc._classify_checked_region(
                compile_failed=False,
                module_not_clean=False,
                tripwire_fired=True,
                witness_failed=False),
            (True, "false_contract"))

    def test_clean_region_keeps_witness_failure(self):
        self.assertEqual(
            cfc._classify_checked_region(
                compile_failed=False,
                module_not_clean=False,
                tripwire_fired=True,
                witness_failed=True),
            (False, "witness_assert_failed"))


if __name__ == "__main__":
    unittest.main()
