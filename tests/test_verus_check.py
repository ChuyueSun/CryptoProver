"""Verus interpreter-panic span promotion.

A Verus *internal* interpreter panic crashes the process before structured
diagnostics are emitted, so `parse_diagnostics` sees only the generic, location-
less "could not compile" line. run.py's next-round feedback is built from
`messages[]`, so without promotion the agent is handed a dead-end compile error
instead of the file:line it must fix. These tests pin that the panic's user-code
span is lifted into structured errors, and ONLY when the normal parser is blind.

Real fragment is from the 2026-06 corefloor run: a `by (bit_vector)` over
`self.0[31]` (a struct array-field) crashes `vir/src/interpreter.rs` at
`edwards.rs:341:33`.

Run: python3 -m unittest tests.test_verus_check
"""
import unittest
from pathlib import Path
import os
import sys
import tempfile
import json
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills"))
import verus_check  # noqa: E402

# Verbatim-shaped stderr: the interpreter panic blob + the generic compile line
# that is all `parse_diagnostics` can see.
PANIC_STDERR = (
    "thread 'interpreter' (19993098) panicked at vir/src/interpreter.rs:758:30:\n"
    "Expected the argument to array_view to already be an Interp(Array(_)).  "
    'Got: SpannedTyped { span: Span { raw_span: "ANY", id: 16642, '
    'data: [7605240700388178311, 1156239556090782], '
    'as_string: "curve25519-dalek/src/edwards.rs:341:33: 341:39 (#0)" }, '
    "typ: Primitive(Array, [Int(U(8)), ConstInt(32)]) }\n"
    "error: could not compile `curve25519-dalek` (lib) due to 1 previous error; "
    "30 warnings emitted\n"
)


class ExtractVerusPanicMessages(unittest.TestCase):
    def test_real_fragment_yields_actionable_span(self):
        out = verus_check.extract_verus_panic_messages(PANIC_STDERR)
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertEqual(e["file"], "curve25519-dalek/src/edwards.rs")
        self.assertEqual(e["line"], 341)
        self.assertEqual(e["column"], 33)
        self.assertEqual(e["severity"], "error")
        self.assertIn("internal panic", e["data"])

    def test_panic_msg_regex_matches_the_fragment(self):
        # The interpreter-panic message regex must recognize the crash blob.
        self.assertIsNotNone(verus_check._PANIC_MSG_RE.search(PANIC_STDERR))

    def test_no_panic_no_spans(self):
        normal = ("error[E0425]: cannot find value `x`\n"
                  "  --> curve25519-dalek/src/edwards.rs:10:5\n")
        self.assertEqual(verus_check.extract_verus_panic_messages(normal), [])

    def test_caps_at_three_spans(self):
        blob = "".join(
            'thread \'interpreter\' panicked at vir:1:1:\nx\n'
            f'as_string: "f{i}.rs:{i}:1: {i}:2 (#0)"\n' for i in range(6))
        self.assertEqual(len(verus_check.extract_verus_panic_messages(blob)), 3)


class ParseVerificationSummary(unittest.TestCase):
    def test_extracts_raw_verified_and_error_counts(self):
        out = verus_check.parse_verification_summary(
            "",
            "verification results:: 2088 verified, 34 errors\n",
        )

        self.assertEqual(out["verified_count"], 2088)
        self.assertEqual(out["raw_verus_error_count"], 34)

    def test_missing_summary_is_none(self):
        out = verus_check.parse_verification_summary("building...", "")

        self.assertIsNone(out["verified_count"])
        self.assertIsNone(out["raw_verus_error_count"])

    def test_uses_last_summary_when_multiple_are_present(self):
        out = verus_check.parse_verification_summary(
            "verification results:: 5 verified, 1 error\n",
            "verification results:: 2098 verified, 38 errors\n",
        )

        self.assertEqual(out["verified_count"], 2098)
        self.assertEqual(out["raw_verus_error_count"], 38)


class GenericCompileNoLongerHidesSpan(unittest.TestCase):
    """Parser-composition: on a panic, `parse_diagnostics` is blind (only the
    generic compile line), but the merge promotes the panic span so the agent
    sees the real location."""

    def test_parse_diagnostics_is_blind_to_the_panic_location(self):
        errors = [m for m in verus_check.parse_diagnostics(PANIC_STDERR)
                  if m["severity"] == "error"]
        # Only the generic compile error, with no usable file:line.
        self.assertFalse(any(m.get("file") and m.get("line") for m in errors))

    def test_merge_surfaces_span_when_parser_generic(self):
        errors = [m for m in verus_check.parse_diagnostics(PANIC_STDERR)
                  if m["severity"] == "error"]
        panic_errors = verus_check.extract_verus_panic_messages(PANIC_STDERR)
        returncode = 101
        generic_only = (
            returncode != 0
            and panic_errors
            and not any(m.get("file") and m.get("line") for m in errors))
        self.assertTrue(generic_only)
        merged = panic_errors + errors[:1]
        located = [m for m in merged if m.get("file") and m.get("line")]
        self.assertTrue(located)
        self.assertEqual(located[0]["file"], "curve25519-dalek/src/edwards.rs")
        self.assertEqual(located[0]["line"], 341)


class SummarizeMessages(unittest.TestCase):
    """`summarize_messages` is the grouped, COMPLETE error view the agent should
    read instead of a truncated `cargo verus | grep | head` (the false-"fixed"
    convergence trap)."""

    def test_empty(self):
        self.assertEqual(verus_check.summarize_messages([]), "")

    def test_only_notes_is_empty(self):
        # the rlimit-hint note (severity != "error") must not show as an error
        self.assertEqual(verus_check.summarize_messages(
            [{"severity": "note", "file": "", "line": 0, "data": "hint"}]), "")

    def test_groups_by_file_with_counts(self):
        msgs = [
            {"severity": "error", "file": "/x/src/montgomery.rs", "line": 469,
             "data": "postcondition not satisfied"},
            {"severity": "error", "file": "/x/src/montgomery.rs", "line": 507,
             "data": "postcondition not satisfied"},
            {"severity": "error", "file": "/x/src/edwards.rs", "line": 2811,
             "data": "assertion failed"},
            {"severity": "note", "file": "", "line": 0, "data": "hint"},
        ]
        out = verus_check.summarize_messages(msgs)
        self.assertIn("montgomery.rs: 2 error(s) @ lines 469, 507", out)
        self.assertIn("postcondition not satisfied x2", out)
        self.assertIn("edwards.rs: 1 error(s) @ lines 2811", out)
        # grouped by basename, sorted
        self.assertTrue(out.index("edwards.rs") < out.index("montgomery.rs"))

    def test_missing_file_falls_back(self):
        out = verus_check.summarize_messages(
            [{"severity": "error", "line": 1, "data": "x"}])
        self.assertIn("?:", out)

    def test_same_basename_different_dirs_not_merged(self):
        # this codebase has top-level scalar.rs AND backend .../u64/scalar.rs;
        # the src-relative key must keep them as distinct groups.
        msgs = [
            {"severity": "error", "file": "/x/src/scalar.rs", "line": 10,
             "data": "a"},
            {"severity": "error",
             "file": "/x/src/backend/serial/u64/scalar.rs", "line": 20,
             "data": "b"},
        ]
        out = verus_check.summarize_messages(msgs)
        self.assertIn("backend/serial/u64/scalar.rs:", out)
        self.assertEqual(out.count("error(s)"), 2)  # two groups, not merged


class ErrorsAlias(unittest.TestCase):
    def test_errors_alias_points_at_messages(self):
        messages = [{"severity": "error", "data": "x"}]
        result = verus_check.with_errors_alias({"messages": messages})
        self.assertIs(result["errors"], messages)

    def test_structured_messages_get_text_compat_fields(self):
        messages = [
            {"severity": "error", "file": "/x/src/ristretto.rs", "line": 42,
             "column": 9, "data": "assertion failed"},
            {"severity": "note", "file": "", "line": 0, "column": 0,
             "data": "hint only"},
        ]

        result = verus_check.with_errors_alias({"messages": messages})

        self.assertEqual(messages[0]["message"], "assertion failed")
        self.assertEqual(messages[1]["message"], "hint only")
        self.assertIs(result["errors"], messages)
        self.assertEqual(result["message_texts"], [
            "/x/src/ristretto.rs:42:9: assertion failed",
            "hint only",
        ])
        self.assertEqual(result["error_texts"], [
            "/x/src/ristretto.rs:42:9: assertion failed",
        ])

    def test_diagnostic_text_handles_unstructured_legacy_items(self):
        self.assertEqual(verus_check.diagnostic_text("raw error"), "raw error")

    def test_file_not_found_cli_includes_errors_alias(self):
        missing = Path("/tmp/definitely-missing-verus-check-target.rs")
        proc = subprocess.run(
            [sys.executable, str(Path(verus_check.__file__)), str(missing)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["errors"], data["messages"])
        self.assertEqual(data["messages"][0]["message"], data["messages"][0]["data"])
        self.assertEqual(data["error_texts"], data["message_texts"])
        self.assertIn("File not found:", data["message_texts"][0])
        self.assertEqual(data["error_count"], 1)
        self.assertEqual(data["num_errors"], 1)
        self.assertIsNone(data["num_verified"])

    def test_legacy_num_count_aliases_are_present(self):
        result = verus_check.with_errors_alias({
            "okay": False,
            "error_count": 34,
            "verified_count": 2088,
            "messages": [],
        })

        self.assertEqual(result["num_errors"], 34)
        self.assertEqual(result["num_verified"], 2088)


class ResolveTargetAndProject(unittest.TestCase):
    def test_targetless_whole_crate_uses_project_lib_anchor(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "src").mkdir()
            (project / "src" / "lib.rs").write_text("")

            target, resolved_project = verus_check.resolve_target_and_project(
                None, project, whole_crate=True)

            self.assertEqual(target, (project / "src" / "lib.rs").resolve())
            self.assertEqual(resolved_project, project.resolve())

    def test_targetless_whole_crate_requires_project(self):
        with self.assertRaises(ValueError):
            verus_check.resolve_target_and_project(
                None, None, whole_crate=True)

    def test_targetless_module_check_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                verus_check.resolve_target_and_project(
                    None, Path(td), whole_crate=False)

    def test_explicit_target_still_derives_project(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            target = project / "src" / "ristretto.rs"
            target.parent.mkdir()
            target.write_text("")
            (project / "Cargo.toml").write_text("[package]\nname='x'\n")

            resolved_target, resolved_project = verus_check.resolve_target_and_project(
                target, None, whole_crate=False)

            self.assertEqual(resolved_target, target.resolve())
            self.assertEqual(resolved_project, project.resolve())

    def test_relative_target_with_project_resolves_inside_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "curve25519-dalek"
            scratch = root / "scratch"
            (project / "src").mkdir(parents=True)
            scratch.mkdir()
            target = project / "src" / "ristretto.rs"
            target.write_text("")

            old_cwd = Path.cwd()
            try:
                os.chdir(scratch)
                resolved_target, resolved_project = (
                    verus_check.resolve_target_and_project(
                        Path("src/ristretto.rs"), project, whole_crate=False))
            finally:
                os.chdir(old_cwd)

            self.assertEqual(resolved_target, target.resolve())
            self.assertEqual(resolved_project, project.resolve())


if __name__ == "__main__":
    unittest.main()
