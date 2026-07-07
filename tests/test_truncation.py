"""Pin the truncated-whole-crate-run policy (2026-07-04 masking incident).

A Verus internal worker panic (vir/src/poly.rs) aborted whole-crate
verification mid-run on the field-floor stage3 trees; the harness scored the
partial error list as the full frontier for ~40 rounds ("only nt:342
remains") while ~48 real scalar-module errors stayed invisible. The fix has
three layers, each pinned here:

  1. verus_check flags a run with no final "verification results" summary as
     `truncated` and fails closed if it would otherwise look green.
  2. run.py holds truncated gate results indeterminate (like timeouts):
     `_compile_blocked_or_indeterminate` and `_plateau_metric_indeterminate`.
  3. The next-round failure queue carries an explicit PARTIAL warning.

Run: `python3 -m unittest tests.test_truncation`
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "skills"))

import run  # noqa: E402
import verus_check  # noqa: E402


def _verification_error(file: str = "curve25519-dalek/src/scalar.rs",
                        line: int = 733) -> dict:
    return {"file": file, "line": line, "column": 13, "severity": "error",
            "data": "postcondition not satisfied"}


SUMMARY_PRESENT = {"verified_count": 2031, "raw_verus_error_count": 0}
SUMMARY_ABSENT = {"verified_count": None, "raw_verus_error_count": None}


class ParseSummary(unittest.TestCase):
    def test_summary_line_parsed(self):
        out = verus_check.parse_verification_summary(
            "verification results:: 2031 verified, 0 errors", "")
        self.assertEqual(out["verified_count"], 2031)
        self.assertEqual(out["raw_verus_error_count"], 0)

    def test_missing_summary_yields_none(self):
        out = verus_check.parse_verification_summary(
            "error: could not compile `curve25519-dalek` (lib)", "")
        self.assertIsNone(out["verified_count"])
        self.assertIsNone(out["raw_verus_error_count"])


class AssessTruncation(unittest.TestCase):
    def test_summary_present_not_truncated(self):
        truncated, extra = verus_check.assess_truncation(
            SUMMARY_PRESENT, 0, [])
        self.assertFalse(truncated)
        self.assertEqual(extra, [])

    def test_aborted_run_with_errors_gets_partial_note(self):
        # The live incident shape: rc=101, one real error parsed, no summary.
        truncated, extra = verus_check.assess_truncation(
            SUMMARY_ABSENT, 101, [_verification_error()])
        self.assertTrue(truncated)
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0]["severity"], "note")
        self.assertIn("PARTIAL", extra[0]["data"])

    def test_dead_green_fails_closed(self):
        # rc==0, zero errors, no summary: must NOT read as verified.
        truncated, extra = verus_check.assess_truncation(
            SUMMARY_ABSENT, 0, [])
        self.assertTrue(truncated)
        severities = [m["severity"] for m in extra]
        self.assertIn("error", severities)
        self.assertIn("note", severities)


class IndeterminateGates(unittest.TestCase):
    def test_truncated_failed_run_is_indeterminate(self):
        result = {"okay": False, "truncated": True,
                  "messages": [_verification_error()]}
        self.assertTrue(run._compile_blocked_or_indeterminate(result))

    def test_untruncated_verification_failure_still_scored(self):
        result = {"okay": False, "truncated": False,
                  "messages": [_verification_error()]}
        self.assertFalse(run._compile_blocked_or_indeterminate(result))

    def test_okay_run_never_indeterminate(self):
        # okay short-circuits: with the fail-closed rule a truncated run can
        # only be okay if it had a summary, so okay=True wins.
        result = {"okay": True, "truncated": False, "messages": []}
        self.assertFalse(run._compile_blocked_or_indeterminate(result))

    def test_plateau_metric_indeterminate_on_truncation(self):
        result = {"okay": False, "truncated": True,
                  "messages": [_verification_error()]}
        self.assertTrue(run._plateau_metric_indeterminate(
            "field-floor", 0, result))

    def test_plateau_metric_scored_when_untruncated(self):
        result = {"okay": False, "truncated": False,
                  "messages": [_verification_error()]}
        self.assertFalse(run._plateau_metric_indeterminate(
            "field-floor", 0, result))


class FailureQueueWarning(unittest.TestCase):
    def test_truncated_queue_carries_partial_warning(self):
        block = run.build_failure_queue_block(
            [_verification_error()], truncated=True)
        self.assertIn("TRUNCATED", block)
        self.assertIn("PARTIAL", block)

    def test_untruncated_queue_has_no_warning(self):
        block = run.build_failure_queue_block(
            [_verification_error()], truncated=False)
        self.assertNotIn("TRUNCATED", block)


if __name__ == "__main__":
    unittest.main()
