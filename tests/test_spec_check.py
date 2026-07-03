"""Tests for the spec-integrity gate (`skills/spec_check.py`).

Pins the subtle parts of the freeze gates:
  - duplicate fn names in one file are disambiguated by occurrence index, so a
    later same-named fn no longer overwrites an earlier one (the bare-name
    keying bug let the spec-def freeze silently skip the first occurrence);
  - `--check-spec-defs` catches a body change on ANY occurrence of a duplicate
    `spec fn`, not just the last;
  - drift entries report the real bare source name, not the synthetic key.

Run: `python3 -m unittest tests.test_spec_check`
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills"))

import spec_check  # noqa: E402


def _sigs(text: str) -> dict:
    return spec_check._extract_sigs(text)


class ClauseSectionParsing(unittest.TestCase):
    def test_stored_header_excludes_contract_clauses(self):
        text = (
            "verus! {\n"
            "proof fn f(a: nat) -> nat\n"
            "    requires a < 10,\n"
            "    ensures result == a,\n"
            "{ a }\n"
            "}\n"
        )
        sig = _sigs(text)["f"]
        self.assertEqual(sig["header"], "proof fn f(a: nat) -> nat")

    def test_plain_clauses_stop_at_next_keyword(self):
        text = (
            "verus! {\n"
            "proof fn f(a: nat)\n"
            "    requires a < 10, a > 0,\n"
            "    ensures a < 5,\n"
            "    decreases a,\n"
            "{ }\n"
            "}\n"
        )
        sig = _sigs(text)["f"]
        self.assertEqual(sig["requires"], "a < 10, a > 0,")
        self.assertEqual(sig["ensures"], "a < 5,")
        self.assertEqual(sig["decreases"], "a,")

    def test_keyword_inside_nested_expression_does_not_stop_clause(self):
        text = (
            "verus! {\n"
            "proof fn f(a: nat)\n"
            "    requires helper(ensures_like(a)),\n"
            "    ensures a < 5,\n"
            "{ }\n"
            "}\n"
        )
        sig = _sigs(text)["f"]
        self.assertEqual(sig["requires"], "helper(ensures_like(a)),")
        self.assertEqual(sig["ensures"], "a < 5,")

    def test_block_clause_stays_intact(self):
        text = (
            "verus! {\n"
            "proof fn f(a: nat)\n"
            "    requires { a < 10 && a > 0 }\n"
            "    ensures a < 5,\n"
            "{ }\n"
            "}\n"
        )
        sig = _sigs(text)["f"]
        self.assertEqual(sig["requires"], "{ a < 10 && a > 0 }")
        self.assertEqual(sig["ensures"], "a < 5,")

    def test_clause_edit_reports_clause_not_header(self):
        original = (
            "verus! {\n"
            "proof fn f(a: nat) -> nat\n"
            "    requires a < 10,\n"
            "    ensures result == a,\n"
            "{ a }\n"
            "}\n"
        )
        current = original.replace("result == a", "result >= a")
        drift = spec_check._verify_one(
            "t.rs", _sigs(original), _sigs(current), check_spec_defs=False)
        self.assertEqual(
            [(d["function"], d["field"]) for d in drift],
            [("f", "ensures")],
        )

    def test_parameter_edit_still_reports_header(self):
        original = "verus! {\nproof fn f(a: nat) -> nat ensures result == a { a }\n}\n"
        current = original.replace("a: nat", "a: int")
        drift = spec_check._verify_one(
            "t.rs", _sigs(original), _sigs(current), check_spec_defs=False)
        self.assertEqual(
            [(d["function"], d["field"]) for d in drift],
            [("f", "header")],
        )


class CosmeticHeaderAttrs(unittest.TestCase):
    def test_inline_attr_removal_on_spec_fn_is_not_drift(self):
        original = (
            "verus! {\n"
            "#[inline(always)]\n"
            "pub(crate) open spec fn l0() -> nat { constants::L.limbs[0] as nat }\n"
            "}\n"
        )
        current = original.replace("#[inline(always)]\n", "")

        drift = spec_check._verify_one(
            "t.rs", _sigs(original), _sigs(current), check_spec_defs=True)

        self.assertEqual(drift, [])

    def test_cold_and_inline_never_removal_are_not_drift(self):
        original = (
            "verus! {\n"
            "#[cold]\n"
            "#[inline(never)]\n"
            "proof fn lemma_a() { }\n"
            "}\n"
        )
        current = original.replace("#[cold]\n#[inline(never)]\n", "")

        drift = spec_check._verify_one(
            "t.rs", _sigs(original), _sigs(current), check_spec_defs=False)

        self.assertEqual(drift, [])

    def test_verifier_inline_removal_still_blocks(self):
        original = (
            "verus! {\n"
            "#[verifier::inline]\n"
            "open spec fn helper(x: nat) -> nat { x + 1 }\n"
            "}\n"
        )
        current = original.replace("#[verifier::inline]\n", "")

        drift = spec_check._verify_one(
            "t.rs", _sigs(original), _sigs(current), check_spec_defs=True)

        self.assertEqual([(d["function"], d["field"]) for d in drift],
                         [("helper", "header")])


class FrozenSpecRestore(unittest.TestCase):
    def test_restore_header_keeps_proof_body_edits(self):
        baseline = (
            "verus! {\n"
            "proof fn lemma_a(x: nat)\n"
            "    requires x < 10,\n"
            "    ensures x < 11,\n"
            "{\n"
            "    admit();\n"
            "}\n"
            "}\n"
        )
        current = baseline.replace("requires x < 10,", "requires x < 100,").replace(
            "admit();", "assert(x < 11);")
        drift = spec_check._verify_one(
            "t.rs", _sigs(baseline), _sigs(current), check_spec_defs=True)

        restored, unresolved = spec_check.restore_frozen_spec_drift(
            baseline, current, drift)

        self.assertEqual(unresolved, [])
        self.assertIn("requires x < 10,", restored)
        self.assertIn("assert(x < 11);", restored)
        self.assertEqual(
            spec_check._verify_one(
                "t.rs", _sigs(baseline), _sigs(restored),
                check_spec_defs=True),
            [],
        )

    def test_restore_spec_body_reverts_definition(self):
        baseline = "verus! {\nopen spec fn helper(x: nat) -> nat { x + 1 }\n}\n"
        current = baseline.replace("x + 1", "x")
        drift = spec_check._verify_one(
            "t.rs", _sigs(baseline), _sigs(current), check_spec_defs=True)

        restored, unresolved = spec_check.restore_frozen_spec_drift(
            baseline, current, drift)

        self.assertEqual(unresolved, [])
        self.assertIn("{ x + 1 }", restored)
        self.assertEqual(
            spec_check._verify_one(
                "t.rs", _sigs(baseline), _sigs(restored),
                check_spec_defs=True),
            [],
        )

    def test_restore_counts_malformed_prior_duplicate_occurrence(self):
        baseline = (
            "verus! {\n"
            "proof fn dup() {\n"
            "    if true {\n"
            "}\n"
            "proof fn dup(x: nat)\n"
            "    requires x < 10,\n"
            "    ensures x < 11,\n"
            "{\n"
            "    admit();\n"
            "}\n"
        )
        current = baseline.replace("requires x < 10,", "requires x < 100,")
        drift = spec_check._verify_one(
            "t.rs", _sigs(baseline), _sigs(current), check_spec_defs=True)

        restored, unresolved = spec_check.restore_frozen_spec_drift(
            baseline, current, drift)

        self.assertEqual([(d["key"], d["field"]) for d in drift],
                         [("dup#1", "requires")])
        self.assertEqual(unresolved, [])
        self.assertIn("requires x < 10,", restored)
        self.assertIn("proof fn dup() {", restored)


class DuplicateNameKeying(unittest.TestCase):
    def test_duplicate_specs_are_not_overwritten(self):
        text = (
            "verus! {\n"
            "spec fn foo() -> int { 1 }\n"
            "spec fn bar() -> int { 2 }\n"
            "spec fn foo() -> int { 3 }\n"
            "}\n"
        )
        sigs = _sigs(text)
        # Both `foo`s survive, under distinct keys; `bar` keeps its bare name.
        self.assertEqual(list(sigs), ["foo", "bar", "foo#1"])
        # The real source name is preserved on every entry.
        self.assertEqual(sigs["foo"]["name"], "foo")
        self.assertEqual(sigs["foo#1"]["name"], "foo")
        # The two bodies are captured independently, not collapsed.
        self.assertEqual(sigs["foo"]["spec_body"], "1")
        self.assertEqual(sigs["foo#1"]["spec_body"], "3")

    def test_unique_names_keep_bare_keys(self):
        text = "verus! {\nspec fn a() -> int { 1 }\nspec fn b() -> int { 2 }\n}\n"
        self.assertEqual(list(_sigs(text)), ["a", "b"])


class SpecDefFreeze(unittest.TestCase):
    """`check_spec_defs=True` must catch a body edit on EITHER occurrence."""

    base = (
        "verus! {\n"
        "spec fn foo() -> int { 1 }\n"
        "spec fn foo() -> int { 3 }\n"
        "}\n"
    )

    def _drift(self, original_text: str, current_text: str) -> list[dict]:
        return spec_check._verify_one(
            "t.rs", _sigs(original_text), _sigs(current_text),
            check_spec_defs=True,
        )

    def test_first_occurrence_body_change_caught(self):
        mutated = self.base.replace("{ 1 }", "{ 99 }")
        drift = self._drift(self.base, mutated)
        self.assertTrue(
            any(d["change"] == "spec_def_modified" and d["function"] == "foo"
                for d in drift),
            drift,
        )

    def test_second_occurrence_body_change_caught(self):
        mutated = self.base.replace("{ 3 }", "{ 99 }")
        drift = self._drift(self.base, mutated)
        self.assertTrue(
            any(d["change"] == "spec_def_modified" and d["function"] == "foo"
                for d in drift),
            drift,
        )

    def test_no_change_is_clean(self):
        self.assertEqual(self._drift(self.base, self.base), [])

    def test_spec_def_change_ignored_without_flag(self):
        mutated = self.base.replace("{ 1 }", "{ 99 }")
        drift = spec_check._verify_one(
            "t.rs", _sigs(self.base), _sigs(mutated), check_spec_defs=False,
        )
        self.assertEqual(drift, [])


class NewExternalBodyHelper(unittest.TestCase):
    """A NEW helper fn carrying `#[verifier::external_body]` must fail verify.

    Adding helper lemmas is allowed (prompt rule 4), but `external_body` skips
    the body entirely (the contract is trusted, never SMT-checked), so a fresh
    `external_body proof fn lemma_cheat(...) ensures GOAL {}` discharges an
    obligation with zero proof — a fake-green path the gate must close even
    though the fn is brand new (not in the snapshot)."""

    def _verify(self, original_text: str, current_text: str):
        import argparse
        import contextlib
        import io
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tgt = Path(td) / "t.rs"
            snap = Path(td) / "snap.json"
            tgt.write_text(original_text)
            spec_check.cmd_snapshot(
                argparse.Namespace(target=tgt, out=snap, siblings=[]))
            tgt.write_text(current_text)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = spec_check.cmd_verify(argparse.Namespace(
                    target=tgt, against=snap, check_spec_defs=False))
            return rc, json.loads(buf.getvalue())

    def test_new_external_body_helper_fails(self):
        original = "verus! {\nproof fn lemma_a() { }\n}\n"
        current = (
            "verus! {\n"
            "proof fn lemma_a() { }\n"
            "#[verifier::external_body]\n"
            "proof fn lemma_cheat() ensures false { }\n"
            "}\n"
        )
        rc, res = self._verify(original, current)
        self.assertEqual(rc, 1)
        self.assertFalse(res["okay"])
        self.assertTrue(
            any(d["change"] == "external_body_new_fn"
                and d["function"] == "lemma_cheat" for d in res["drift"]),
            res,
        )

    def test_new_plain_helper_is_clean(self):
        original = "verus! {\nproof fn lemma_a() { }\n}\n"
        current = (
            "verus! {\n"
            "proof fn lemma_a() { }\n"
            "proof fn lemma_b() { }\n"
            "}\n"
        )
        rc, res = self._verify(original, current)
        self.assertEqual(rc, 0)
        self.assertTrue(res["okay"], res)


class GeneratedLemmaContractDrift(unittest.TestCase):
    def _verify_file(self, path: Path, original_text: str, current_text: str):
        import argparse
        import contextlib
        import io
        import json
        snap = path.parent / "snap.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original_text)
        spec_check.cmd_snapshot(
            argparse.Namespace(target=path, out=snap, siblings=[]))
        path.write_text(current_text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = spec_check.cmd_verify(argparse.Namespace(
                target=path, against=snap, check_spec_defs=True))
        return rc, json.loads(buf.getvalue())

    def test_generated_lemma_contract_drift_is_allowed_but_preserved_raw(self):
        import tempfile
        original = (
            "verus! {\n"
            "proof fn lemma_field_abs_neg(a: nat)\n"
            "    ensures a == a,\n"
            "{ }\n"
            "}\n"
        )
        current = original.replace(
            "    ensures a == a,\n",
            "    requires a < p(),\n"
            "    ensures a == a,\n",
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "src/lemmas/field_lemmas/field_algebra_lemmas.rs"
            rc, res = self._verify_file(path, original, current)
        self.assertEqual(rc, 0)
        self.assertTrue(res["okay"], res)
        self.assertEqual(res["raw_drift_count"], 1)
        self.assertEqual(res["blocking_drift"], [])
        self.assertEqual(res["allowed_generated_contract_drift_count"], 1)
        self.assertEqual(res["drift"][0]["field"], "requires")

    def test_generated_lemma_header_drift_still_blocks(self):
        import tempfile
        original = (
            "verus! {\n"
            "pub proof fn lemma_field_abs_neg(a: nat)\n"
            "    ensures a == a,\n"
            "{ }\n"
            "}\n"
        )
        current = original.replace("pub proof fn", "pub(crate) proof fn")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "src/lemmas/field_lemmas/field_algebra_lemmas.rs"
            rc, res = self._verify_file(path, original, current)
        self.assertEqual(rc, 1)
        self.assertFalse(res["okay"], res)
        self.assertEqual(res["allowed_generated_contract_drift"], [])
        self.assertEqual(res["blocking_drift_count"], 1)
        self.assertEqual(res["blocking_drift"][0]["field"], "header")

    def test_generated_lemma_contract_drift_outside_lemmas_still_blocks(self):
        import tempfile
        original = (
            "verus! {\n"
            "proof fn lemma_api(a: nat)\n"
            "    ensures a == a,\n"
            "{ }\n"
            "}\n"
        )
        current = original.replace(
            "    ensures a == a,\n",
            "    requires a < 10,\n"
            "    ensures a == a,\n",
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "src/ristretto.rs"
            rc, res = self._verify_file(path, original, current)
        self.assertEqual(rc, 1)
        self.assertFalse(res["okay"], res)
        self.assertEqual(res["allowed_generated_contract_drift"], [])
        self.assertEqual(res["blocking_drift_count"], 1)

    def test_generated_lemma_spec_body_drift_still_blocks(self):
        import tempfile
        original = (
            "verus! {\n"
            "proof fn lemma_a()\n"
            "    ensures helper() == 1,\n"
            "{ }\n"
            "spec fn helper() -> int { 1 }\n"
            "}\n"
        )
        current = original.replace("spec fn helper() -> int { 1 }",
                                   "spec fn helper() -> int { 2 }")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "src/lemmas/field_lemmas/x.rs"
            rc, res = self._verify_file(path, original, current)
        self.assertEqual(rc, 1)
        self.assertFalse(res["okay"], res)
        self.assertEqual(res["allowed_generated_contract_drift"], [])
        self.assertEqual(res["blocking_drift"][0]["field"], "spec_body")


class VerifySnapshotFallback(unittest.TestCase):
    def _snapshot_file(self, tmpdir: Path):
        import argparse
        target = tmpdir / "t.rs"
        snap = tmpdir / "spec_snapshot.json"
        target.write_text("verus! {\nproof fn lemma_a() { }\n}\n")
        spec_check.cmd_snapshot(
            argparse.Namespace(target=target, out=snap, siblings=[]))
        return target, snap

    def test_resolve_against_uses_explicit_path_first(self):
        explicit = Path("/tmp/explicit-spec-snapshot.json")
        self.assertEqual(spec_check.resolve_against_snapshot(explicit), explicit)

    def test_verify_uses_spec_snapshot_env_when_against_omitted(self):
        import argparse
        import contextlib
        import io
        import json
        import os
        import tempfile

        old = os.environ.get("SPEC_SNAPSHOT")
        with tempfile.TemporaryDirectory() as td:
            target, snap = self._snapshot_file(Path(td))
            os.environ["SPEC_SNAPSHOT"] = str(snap)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = spec_check.cmd_verify(argparse.Namespace(
                    target=target, against=None, project=Path(td),
                    check_spec_defs=False))
            data = json.loads(buf.getvalue())
        if old is None:
            os.environ.pop("SPEC_SNAPSHOT", None)
        else:
            os.environ["SPEC_SNAPSHOT"] = old

        self.assertEqual(rc, 0)
        self.assertTrue(data["okay"], data)

    def test_cli_accepts_project_with_spec_snapshot_env(self):
        import json
        import os
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            target, snap = self._snapshot_file(Path(td))
            env = os.environ.copy()
            env["SPEC_SNAPSHOT"] = str(snap)
            proc = subprocess.run(
                [sys.executable, str(Path(spec_check.__file__)), "verify",
                 str(target), "--project", td],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertTrue(json.loads(proc.stdout)["okay"])

    def test_verify_missing_against_and_env_fails_json(self):
        import argparse
        import contextlib
        import io
        import json
        import os
        import tempfile

        old = os.environ.pop("SPEC_SNAPSHOT", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                target = Path(td) / "t.rs"
                target.write_text("verus! {\nproof fn lemma_a() { }\n}\n")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = spec_check.cmd_verify(argparse.Namespace(
                        target=target, against=None, project=Path(td),
                        check_spec_defs=False))
                data = json.loads(buf.getvalue())
        finally:
            if old is not None:
                os.environ["SPEC_SNAPSHOT"] = old

        self.assertEqual(rc, 2)
        self.assertFalse(data["okay"])
        self.assertIn("SPEC_SNAPSHOT", data["error"])


class SiblingHelperDiscovery(unittest.TestCase):
    def test_scalar_lemmas_trailing_underscore_dir_has_siblings(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            helper_dir = project / "src/lemmas/scalar_lemmas_"
            helper_dir.mkdir(parents=True)
            target = helper_dir / "montgomery_reduce_lemmas.rs"
            sibling = helper_dir / "radix_2w_lemmas.rs"
            mod_file = helper_dir / "mod.rs"
            target.write_text("verus! {}\n")
            sibling.write_text("verus! {}\n")
            mod_file.write_text("pub mod radix_2w_lemmas;\n")

            siblings = spec_check.discover_sibling_helpers(project, target)

        self.assertEqual(siblings, [sibling.resolve()])


if __name__ == "__main__":
    unittest.main()
