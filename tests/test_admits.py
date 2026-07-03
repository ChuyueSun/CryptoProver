"""Tests for the canonical axiom-aware admit counter and the
inventory JSON shape used by `skills/admit_inventory.py`.

Replaces (and merges) the previous two files:
  - tests/test_count_admits.py (algorithm regression table)
  - tests/test_admit_inventory.py (JSON-shape + cross-check tests)

Single algorithm now lives in `lib.admits`. `run._count_llm_target_admits`
is an import alias of `lib.admits.count_non_axiom`, so callers of either
name run identical code — verified by `AliasIntegrity`.

Run: `python3 -m unittest tests.test_admits`
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.admits import (  # noqa: E402
    admit_proof_blocks,
    admit_proof_fn_bodies,
    classify_admit_lines,
    count_non_axiom,
    find_proof_fn_body_brace,
    inventory_file,
    inventory_files,
    strip_comments_strings,
)


# ---------- algorithm regression table ----------------------------------
# Migrated verbatim from the previous tests/test_count_admits.py.
# These fixtures pin specific bugs found in PR review and during real
# agent runs. Don't delete cases here without understanding why each
# one was added — see comments on each block.

class AdmitCounterRegressionTable(unittest.TestCase):
    """Each case is (description, source, expected_non_axiom_count)."""

    CASES = [
        # --- basic ---
        ("bare admit() in non-axiom function",
         "pub proof fn lemma_normal() {\n    admit();\n}\n", 1),
        ("no admits at all",
         "pub proof fn lemma_x() {\n    let y = 1;\n}\n", 0),

        # --- regression: pippenger doc-comment bug ---
        ("doc comment mentioning admit() text does not count",
         "//! All proofs are done — no `admit()` remains.\n"
         "pub proof fn lemma_x() {\n    let y = 1;\n}\n", 0),
        ("inline `//` comment with admit() text does not count",
         "pub proof fn lemma_x() {\n    let y = 1;  // admit() once\n}\n", 0),

        # --- axiom-by-convention exclusion ---
        ("top-level axiom_* body's admit is excluded",
         "pub proof fn axiom_x()\n{\n    admit();\n}\n", 0),
        ("pub(crate) axiom_* body's admit is excluded",
         "pub(crate) proof fn axiom_x()\n{\n    admit();\n}\n", 0),
        ("pub(super) axiom_* body's admit is excluded",
         "pub(super) proof fn axiom_x()\n{\n    admit();\n}\n", 0),
        ("axiom_* admit excluded; later non-axiom admit counted",
         "pub proof fn axiom_x()\n{\n    admit();\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),

        # --- regression: indented closing brace (P1) ---
        ("indented axiom inside impl block — closing } is indented",
         "impl X {\n    pub proof fn axiom_indented() {\n"
         "        admit()\n    }\n}\n"
         "pub proof fn lemma_outer() {\n    admit();\n}\n", 1),

        # --- regression: brace-counting confused by ensures ({...}) (P2 v1) ---
        # This is the case that broke a 2026-05 reimplementation of the
        # counter: the `{` inside `({ ... })` was mistaken for the body
        # opener. The _BODY_OPEN_RE end-of-line anchor is what prevents
        # this — do NOT loosen it.
        ("inline ensures ({...}) followed by standalone { body opener",
         "pub proof fn axiom_x()\n    ensures\n"
         "        ({ let z = 1; z == 1 }),\n{\n    admit();\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),
        ("axiom requires with if/else expression braces stays in signature",
         "pub proof fn axiom_x(flag: bool, out: nat)\n    requires\n"
         "        out == (if flag {\n            1\n        } else {\n            2\n        }),\n"
         "{\n    admit();\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),

        # --- regression: same-line body opener on final sig line (P2 v2) ---
        ("multi-line header, `ensures e, {` on its own line",
         "pub proof fn axiom_x(y: int)\n    ensures\n        y > 0, {\n"
         "    admit();\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),
        ("multi-line header with `) {` body opener",
         "pub proof fn axiom_x(\n    y: int,\n) {\n    admit();\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),

        # --- body with nested {} blocks should not exit early ---
        ("axiom body with nested if{}",
         "pub proof fn axiom_x() {\n    if true {\n        admit()\n    }\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),

        # --- real-world Verus: array-type ; in args (must not confuse
        #     the counter — `;` is only a body-end signal when at top level)
        ("fn with `&[u8; 32]` arg — array-type `;` is harmless",
         "pub fn encode_253_bits(data: &[u8; 32]) -> Option<u8> {\n"
         "    admit();\n    None\n}\n", 1),
        ("axiom_* fn with array-type arg",
         "pub proof fn axiom_array_pkg(buf: &[u8; 64]) {\n"
         "    admit();\n}\n", 0),
    ]

    def test_table(self):
        for desc, src, want in self.CASES:
            with self.subTest(desc=desc):
                got = count_non_axiom(src)
                self.assertEqual(got, want, f"{desc}: got {got}, want {want}")


# ---------- comment / string-aware admit detection ----------------------

class CommentAndStringAwareAdmits(unittest.TestCase):
    """`strip_comments_strings` masks comments + string/char literals so an
    `admit()` counts only as real code. Pins BOTH failure directions:
      - a comment/string *mention* must NOT be counted (else a done file is
        stuck at LIMIT), and
      - a real `admit()` after a `//`-bearing string literal must NOT be lost
        (a naive `split("//")` drops it → fake COMPLETE on a Verus-green file
        that is green only because of that admit). [P2 review]
    """

    def test_strip_is_length_preserving(self):
        src = 'let u = "https://x"; admit();  // note\nfn f() {}\n'
        self.assertEqual(len(strip_comments_strings(src)), len(src))
        # newline positions preserved → line count unchanged
        self.assertEqual(
            strip_comments_strings(src).count("\n"), src.count("\n"))

    def test_string_line_continuation_preserves_line_numbers(self):
        # Regression: a `\`<newline> continuation inside a string must keep the
        # newline, else every downstream line number shifts (found by the
        # whole-repo length/newline invariant sweep on scalar.rs).
        src = 'let m = "first line \\\n    second line";\nproof fn f() { admit(); }\n'
        masked = strip_comments_strings(src)
        self.assertEqual(masked.count("\n"), src.count("\n"))
        self.assertEqual(len(masked), len(src))
        # the admit on the real last line is still found at the right line
        self.assertEqual(classify_admit_lines(src)["non_axiom_lines"], [3])

    def test_admit_after_string_with_slashes_is_counted(self):
        # The P2 case: // lives inside a string literal, real admit follows.
        src = 'proof fn lemma_x() {\n    let _ = "https://e.com"; admit();\n}\n'
        self.assertEqual(count_non_axiom(src), 1)
        self.assertIn(2, classify_admit_lines(src)["non_axiom_lines"])

    def test_admit_inside_string_literal_not_counted(self):
        src = 'proof fn lemma_x() {\n    let s = "call admit() here";\n}\n'
        self.assertEqual(count_non_axiom(src), 0)

    def test_admit_in_line_comment_not_counted(self):
        src = 'proof fn lemma_x() {\n    // TODO admit() later\n    assert(true);\n}\n'
        self.assertEqual(count_non_axiom(src), 0)

    def test_admit_in_block_comment_not_counted(self):
        src = 'proof fn lemma_x() {\n    /* admit();\n       still comment */\n    assert(true);\n}\n'
        self.assertEqual(count_non_axiom(src), 0)

    def test_real_admit_with_trailing_comment_still_counted(self):
        src = 'proof fn lemma_x() {\n    admit();  // discharge later\n}\n'
        self.assertEqual(count_non_axiom(src), 1)

    def test_classify_remaining_admits_matches_directions(self):
        # run.classify_remaining_admits shares the masker via run.py.
        import tempfile as _tf
        from run import classify_remaining_admits
        cases = [
            ('let _ = "https://x"; admit();', 1),   # real admit after string//
            ('//! no `admit()` remains here', 0),    # doc-comment mention
            ('let s = "admit()";', 0),               # inside string literal
        ]
        for body, want_hard in cases:
            with self.subTest(body=body), _tf.TemporaryDirectory() as td:
                p = Path(td) / "t.rs"
                p.write_text(f"proof fn lemma_x() {{\n    {body}\n}}\n")
                res = classify_remaining_admits(p)
                self.assertEqual(res["hard"], want_hard, f"{body!r}")


# ---------- inventory JSON shape ----------------------------------------

class InventoryJsonShape(unittest.TestCase):
    """The `skills/admit_inventory.py` CLI uses `inventory_file` /
    `inventory_files` from `lib.admits`. Pin the JSON shape callers
    (including the agent's prompt-guided reuse of `non_axiom_count`)
    depend on."""

    def test_inventory_classifies_axiom_and_non_axiom(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "target.rs"
            p.write_text(
                "// admit() in comment ignored\n"
                "pub proof fn axiom_foundation() {\n    admit();\n}\n"
                "pub proof fn lemma_needed() {\n"
                "    // another admit() comment\n    admit();\n}\n"
            )
            inv = inventory_files([p])

        self.assertEqual(inv["non_axiom_count"], 1)
        self.assertEqual(inv["axiom_count"], 1)
        self.assertFalse(inv["okay_for_complete"])
        # Per-admit entries report file + line only (no fn name).
        self.assertEqual(
            set(inv["non_axiom_admits"][0].keys()), {"file", "line"})
        self.assertEqual(
            set(inv["axiom_admits"][0].keys()), {"file", "line"})

    def test_okay_when_only_axiom_admits_remain(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "target.rs"
            p.write_text("pub proof fn axiom_only() {\n    admit();\n}\n")
            inv = inventory_files([p])

        self.assertEqual(inv["non_axiom_count"], 0)
        self.assertEqual(inv["axiom_count"], 1)
        self.assertTrue(inv["okay_for_complete"])

    def test_line_numbers_are_one_indexed_and_point_at_admit(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "target.rs"
            p.write_text(
                "pub proof fn lemma_a() {\n"   # line 1
                "    admit();\n"                # line 2
                "}\n"                           # line 3
                "pub proof fn lemma_b() {\n"   # line 4
                "    let y = 1;\n"              # line 5
                "    admit();\n"                # line 6
                "}\n"                           # line 7
            )
            inv = inventory_file(p)
        self.assertEqual(
            [a["line"] for a in inv["non_axiom_admits"]], [2, 6])

    def test_classify_lines_returns_both_partitions(self):
        cls = classify_admit_lines(
            "pub proof fn axiom_a() {\n    admit();\n}\n"  # line 2 axiom
            "pub proof fn lemma_b() {\n    admit();\n}\n"  # line 5 non-axiom
        )
        self.assertEqual(cls["non_axiom_lines"], [5])
        self.assertEqual(cls["axiom_lines"], [2])


# ---------- alias integrity ---------------------------------------------

class AliasIntegrity(unittest.TestCase):
    """`run._count_llm_target_admits` is an import alias of
    `lib.admits.count_non_axiom`. Pin that this stays true — if someone
    accidentally reintroduces a local definition with the same name in
    run.py, this test catches the divergence on the first run."""

    def test_run_py_alias_is_lib_admits_count_non_axiom(self):
        from run import _count_llm_target_admits as run_counter
        self.assertIs(run_counter, count_non_axiom,
                      "run._count_llm_target_admits must be the same object "
                      "as lib.admits.count_non_axiom (drop any local "
                      "reimplementation that shadows the import)")


# ---------- real-file anchor tests --------------------------------------

class RealFileInvariants(unittest.TestCase):
    """Anchor tests against a live Verus worktree.

    Hard-coded counts go stale the moment an agent run mutates a file,
    which produced a confusing false failure during earlier patch work.
    Instead, this class tests *invariants* that hold regardless of
    intermediate file state:

      1. Counts are bounded: 0 ≤ count ≤ raw_grep_count('admit()').
      2. Files known to be pure-axiom or pure-doc-comment must return 0
         (these are stable properties of the file's structure, not of a
         mutable admit count).

    Worktree path resolution:
      1. `$DALEK_WORKTREE` environment variable, else
      2. first existing path from `_CANDIDATE_WORKTREES`, else
      3. skip the entire class (suite stays runnable in CI / fresh
         clones / unrelated machines).
    """

    _CANDIDATE_WORKTREES = (
        Path.home() / "inference-dalek/eval_results/exp20i-pre-stepverify/worktree/curve25519-dalek",
        Path.home() / "dalek-lite/curve25519-dalek",
    )

    @classmethod
    def _resolve_worktree(cls) -> Path | None:
        env = os.environ.get("DALEK_WORKTREE")
        if env:
            p = Path(env).expanduser()
            return p if p.exists() else None
        for p in cls._CANDIDATE_WORKTREES:
            if p.exists():
                return p
        return None

    EXPECT_ZERO = (
        ("src/lemmas/edwards_lemmas/pippenger_lemmas.rs",
         "doc-comment mentions admit() — must not count"),
        ("src/specs/edwards_specs.rs",       "single axiom_*-bodied admit"),
        ("src/specs/window_specs.rs",        "single axiom_*-bodied admit"),
    )

    EXPECT_BOUNDED = (
        ("src/lemmas/edwards_lemmas/curve_equation_lemmas.rs",
         "mid-run count varies; bounds must always hold"),
    )

    def setUp(self):
        wt = self._resolve_worktree()
        if wt is None:
            env_hint = "set $DALEK_WORKTREE or place a worktree at one of: " + \
                       ", ".join(str(p) for p in self._CANDIDATE_WORKTREES)
            self.skipTest(f"no Verus worktree found ({env_hint})")
        self.worktree = wt

    @staticmethod
    def _raw_admit_count(text: str) -> int:
        return text.count("admit()")

    def test_zero_invariant(self):
        for rel, why in self.EXPECT_ZERO:
            with self.subTest(file=rel):
                f = self.worktree / rel
                if not f.exists():
                    self.skipTest(f"{rel} missing in this worktree")
                got = count_non_axiom(f.read_text())
                self.assertEqual(
                    got, 0,
                    f"{rel}: expected 0 LLM-target admits ({why}); got {got}")

    def test_bounded_invariant(self):
        files = [(rel, why) for rel, why in self.EXPECT_BOUNDED] + \
                [(rel, why) for rel, why in self.EXPECT_ZERO]
        for rel, why in files:
            with self.subTest(file=rel):
                f = self.worktree / rel
                if not f.exists():
                    self.skipTest(f"{rel} missing in this worktree")
                text = f.read_text()
                got = count_non_axiom(text)
                raw = self._raw_admit_count(text)
                self.assertGreaterEqual(
                    got, 0, f"{rel}: counter returned negative ({got})")
                self.assertLessEqual(
                    got, raw,
                    f"{rel}: counter ({got}) exceeds raw 'admit()' "
                    f"line count ({raw}) — over-counting bug")


# ---------- rejection continue message ----------------------------------

class RejectionContinueMsg(unittest.TestCase):
    """The continuation message sent on the next round when a previous
    `END_REASON:COMPLETE` is overridden. Pin that the message
    (a) actually changes from the default `"continue"`, and
    (b) names the specific rejection cause so the agent can act on it."""

    def test_verus_failing_with_admits_remaining(self):
        from run import _rejection_continue_msg
        msg = _rejection_continue_msg(verus_okay=False, admits_left=1)
        self.assertNotEqual(msg, "continue")
        self.assertIn("rejected", msg)
        self.assertIn("verus_okay=False", msg)
        self.assertIn("admits remaining=1", msg)

    def test_verus_passing_but_admits_remaining(self):
        from run import _rejection_continue_msg
        msg = _rejection_continue_msg(verus_okay=True, admits_left=2)
        self.assertIn("verus_okay=True", msg)
        self.assertIn("admits remaining=2", msg)

    def test_verus_failing_zero_admits_points_to_verus_diagnostics(self):
        from run import _rejection_continue_msg
        msg = _rejection_continue_msg(verus_okay=False, admits_left=0)
        self.assertIn("verus_okay=False", msg)
        self.assertIn("admits remaining=0", msg)
        self.assertIn("no remaining admits", msg)
        self.assertIn("Verus errors", msg)
        self.assertIn("top-level API proof body", msg)
        self.assertNotIn("locate any remaining `admit()`", msg)

    def test_message_mentions_recovery_options(self):
        from run import _rejection_continue_msg
        msg = _rejection_continue_msg(verus_okay=False, admits_left=3)
        self.assertIn("verus_check", msg)
        self.assertIn("COMPLETE", msg)
        self.assertIn("LIMIT", msg)


class SiblingFailureFeedback(unittest.TestCase):
    def test_sibling_failure_message_says_continue_and_names_files(self):
        from run import _sibling_failure_continue_msg
        msg = _sibling_failure_continue_msg([
            "src/lemmas/ristretto_lemmas/coset_lemmas.rs"
        ])
        self.assertIn("not a terminal harness error", msg)
        self.assertIn("fix", msg)
        self.assertIn("coset_lemmas.rs", msg)
        self.assertIn("COMPLETE", msg)


class FrozenEditRecoveryFeedback(unittest.TestCase):
    """FROZEN_EDIT is recoverable (revert + relocate), not instantly terminal.
    The continuation message must (a) say the file was reverted, (b) name it,
    and (c) tell the agent to relocate the work into an editable file."""

    def test_frozen_edit_message_says_reverted_and_relocate(self):
        from run import _frozen_edit_continue_msg
        msg = _frozen_edit_continue_msg([
            "curve25519-dalek/src/lemmas/montgomery_curve_lemmas.rs"
        ])
        self.assertNotEqual(msg, "continue")
        self.assertIn("REVERTED", msg)
        self.assertIn("RELOCATE", msg)
        self.assertIn("FROZEN_EDIT", msg)
        self.assertIn("montgomery_curve_lemmas.rs", msg)
        # Points the agent at a legal home for the helper.
        self.assertIn("proof fn lemma_", msg)

    def test_frozen_edit_message_truncates_long_file_lists(self):
        from run import _frozen_edit_continue_msg
        msg = _frozen_edit_continue_msg([f"f{i}.rs" for i in range(8)])
        self.assertIn("...", msg)
        # Only the first five are shown by name.
        self.assertIn("f0.rs", msg)
        self.assertNotIn("f7.rs", msg)

    def test_tainted_frozen_recovery_round_cannot_be_final_green(self):
        """P1 invariant: a frozen-edit-recovery round is verified BEFORE the
        revert that removes the helper which made it pass. The loop taints it
        (`verus_okay=False`); if it is the LAST round, the final-state gate must
        NOT promote it to COMPLETE off that stale pre-revert green."""
        from lib.results import RoundResult
        from run import _final_end_reason
        tainted = RoundResult(
            round_number=7, end_reason="FROZEN_EDIT_RECOVERED",
            returncode=0, duration_seconds=1.0, verus_okay=False)
        last_round_okay = bool([tainted] and [tainted][-1].verus_okay)
        admits_remaining = 0  # even with zero admits, the post-revert state is unverified
        done_for_real = last_round_okay and admits_remaining == 0
        self.assertFalse(done_for_real)
        self.assertNotEqual(_final_end_reason(done_for_real, None), "COMPLETE")

    def test_clean_round_with_zero_admits_is_final_green(self):
        """Contrast: an untainted okay last round with no admits DOES complete —
        confirming the taint (not some unrelated block) is what gates P1."""
        from lib.results import RoundResult
        from run import _final_end_reason
        clean = RoundResult(
            round_number=8, end_reason="COMPLETE",
            returncode=0, duration_seconds=1.0, verus_okay=True)
        done_for_real = bool([clean][-1].verus_okay) and 0 == 0
        self.assertTrue(done_for_real)
        self.assertEqual(_final_end_reason(done_for_real, None), "COMPLETE")

    def test_frozen_diff_parser_includes_both_sides_of_rename(self):
        from run import _frozen_paths_from_diff_name_status_z
        out = (
            "M\0src/montgomery.rs\0"
            "R100\0src/lemmas/old.rs\0src/lemmas/new.rs\0"
        )
        got = _frozen_paths_from_diff_name_status_z(
            out, {"src/montgomery.rs"})
        self.assertEqual(got, ["src/lemmas/new.rs", "src/lemmas/old.rs"])

    def test_frozen_diff_parser_filters_editable_paths(self):
        from run import _frozen_paths_from_diff_name_status_z
        out = "A\0src/ristretto.rs\0D\0src/lemmas/frozen.rs\0"
        got = _frozen_paths_from_diff_name_status_z(
            out, {"src/ristretto.rs"})
        self.assertEqual(got, ["src/lemmas/frozen.rs"])

    def test_frozen_diff_parser_ignores_verilib_sidecars(self):
        from run import _frozen_paths_from_diff_name_status_z
        out = (
            "M\0.verilib/.gitignore\0"
            "A\0.verilib/certs/specs/probe.json\0"
            "M\0src/lemmas/frozen.rs\0"
        )
        got = _frozen_paths_from_diff_name_status_z(
            out, {"src/ristretto.rs"})
        self.assertEqual(got, ["src/lemmas/frozen.rs"])

    def test_frozen_git_diff_failure_reports_error(self):
        from run import _frozen_paths_changed_from_git
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "broken-worktree"
            project.mkdir()
            (project / ".git").write_text(
                "gitdir: /definitely/missing/worktree-gitdir\n")
            paths, err = _frozen_paths_changed_from_git(project, set())
        self.assertEqual(paths, [])
        self.assertIsNotNone(err)
        self.assertIn("git diff rc=", err)

    def test_sibling_failures_are_flattened_into_verus_diagnostics(self):
        from run import _flatten_sibling_fail_messages
        msgs = _flatten_sibling_fail_messages([{
            "file": "src/lemmas/ristretto_lemmas/coset_lemmas.rs",
            "errors": [
                {
                    "file": "curve25519-dalek/src/lemmas/ristretto_lemmas/coset_lemmas.rs",
                    "line": 45,
                    "column": 54,
                    "data": "disallowed: field expression for an opaque datatype",
                },
                {
                    "file": "curve25519-dalek/src/lemmas/ristretto_lemmas/coset_lemmas.rs",
                    "line": 45,
                    "column": 54,
                    "data": "disallowed: field expression for an opaque datatype",
                },
            ],
        }])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["line"], 45)
        self.assertIn("sibling re-verify failed for", msgs[0]["data"])
        self.assertIn("opaque datatype", msgs[0]["data"])


class PostAgentStateSnapshot(unittest.TestCase):
    def test_records_dirty_state_before_round_json_exists(self):
        from run import snapshot_post_agent_round_state

        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            src = project / "src"
            src.mkdir()
            target = src / "ristretto.rs"
            target.write_text("fn ct_eq() {\n    proof {}\n}\n")
            subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(project), "add", "src/ristretto.rs"], check=True)
            subprocess.run([
                "git", "-C", str(project),
                "-c", "user.name=test",
                "-c", "user.email=test@example.com",
                "commit", "-q", "-m", "baseline",
            ], check=True)

            target.write_text(
                "fn ct_eq() {\n"
                "    proof {\n"
                "        assert(edwards_x(self.0) == self.0.X);\n"
                "    }\n"
                "}\n"
            )
            tdir = project / "results" / "run" / "ristretto"
            snapshots_root = tdir / "snapshots"

            state = snapshot_post_agent_round_state(
                project, [target], snapshots_root, tdir, 2, target)

            self.assertTrue((tdir / "round_2_agent_state.json").exists())
            self.assertTrue((snapshots_root / "round_2_agent" / "ristretto.rs").exists())
            self.assertEqual(
                state["diff_name_status"],
                [{"status": "M", "paths": ["src/ristretto.rs"]}],
            )
            self.assertIn("edwards_x(self.0)", state["target_diff_excerpt"])
            self.assertEqual(state["phase"], "post_claude_pre_verus")


# ---------- final-state gate / NEEDS_DECOMP escalation ------------------

class FinalEndReasonGate(unittest.TestCase):
    """`run._final_end_reason` resolves the recorded end_reason from the
    final-state gate. Pin the decision table — especially that NEEDS_DECOMP
    (the Feature2 escalation) is preserved when the proof did NOT actually
    finish, but is promoted to COMPLETE when it did. Demoting it to LIMIT
    would lose the "needs missing infrastructure" signal a retry relies on
    to bump its budget."""

    def test_done_for_real_is_always_complete(self):
        from run import _final_end_reason
        # Regardless of the agent's self-declared reason.
        for claimed in ("COMPLETE", "LIMIT", "NEEDS_DECOMP", None, ""):
            with self.subTest(claimed=claimed):
                self.assertEqual(
                    _final_end_reason(True, claimed), "COMPLETE")

    def test_needs_decomp_preserved_when_not_done(self):
        from run import _final_end_reason
        self.assertEqual(
            _final_end_reason(False, "NEEDS_DECOMP"), "NEEDS_DECOMP")

    def test_needs_decomp_is_case_insensitive(self):
        from run import _final_end_reason
        self.assertEqual(
            _final_end_reason(False, "needs_decomp"), "NEEDS_DECOMP")

    def test_unfinished_complete_claim_demoted_to_limit(self):
        from run import _final_end_reason
        # Agent claimed COMPLETE but evidence disagrees (not done_for_real).
        self.assertEqual(_final_end_reason(False, "COMPLETE"), "LIMIT")

    def test_honest_limit_and_missing_reason_are_limit(self):
        from run import _final_end_reason
        self.assertEqual(_final_end_reason(False, "LIMIT"), "LIMIT")
        self.assertEqual(_final_end_reason(False, None), "LIMIT")
        self.assertEqual(_final_end_reason(False, ""), "LIMIT")

    def test_rate_limited_preserved_when_not_done(self):
        from run import _final_end_reason
        self.assertEqual(
            _final_end_reason(False, "RATE_LIMITED"), "RATE_LIMITED")

    def test_rate_limited_beats_done_for_real(self):
        from run import _final_end_reason
        # A 429 halt must NOT be promoted to COMPLETE even when the target
        # trivially verifies (zero hard admits) — otherwise the throttle is
        # masked, the launcher won't halt, and a trivial target lands in
        # proven_registry off a round the agent never ran.
        self.assertEqual(
            _final_end_reason(True, "RATE_LIMITED"), "RATE_LIMITED")

    def test_rate_limited_is_case_insensitive(self):
        from run import _final_end_reason
        self.assertEqual(
            _final_end_reason(False, "rate_limited"), "RATE_LIMITED")

    def test_rate_limit_or_hang_preserved_even_over_green(self):
        from run import _final_end_reason
        self.assertEqual(
            _final_end_reason(True, "RATE_LIMIT_OR_HANG"), "RATE_LIMIT_OR_HANG")
        self.assertEqual(
            _final_end_reason(False, "rate_limit_or_hang"), "RATE_LIMIT_OR_HANG")

    def test_transport_failures_preserved_even_over_green(self):
        from run import _final_end_reason
        for reason in ("RETRY_EXHAUSTED", "TRANSPORT_ERROR",
                       "USER_INTERRUPTED", "retry_exhausted",
                       "INTERRUPTED_SIGNAL", "transport_error",
                       "user_interrupted", "interrupted_signal"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    _final_end_reason(True, reason), reason.upper())
                self.assertEqual(
                    _final_end_reason(False, reason), reason.upper())

    def test_drift_signals_never_promoted_to_complete(self):
        from run import _final_end_reason
        # Cheating signals win even when verus is green (done_for_real=True):
        # a weakened spec / injected axiom / doctored verification skill is how
        # an agent fakes a green.
        for drift in ("SPEC_DRIFT", "AXIOM_DRIFT", "TOOLING_DRIFT",
                      "spec_drift", "axiom_drift", "tooling_drift"):
            with self.subTest(drift=drift):
                self.assertEqual(
                    _final_end_reason(True, drift), drift.upper())
                self.assertEqual(
                    _final_end_reason(False, drift), drift.upper())

    def test_false_contract_preserved_even_over_green(self):
        from run import _final_end_reason
        # E7: a machine-verified-false frozen contract means the crate cannot be
        # honestly completed, so it's preserved even if done_for_real (which
        # shouldn't co-occur, but must never read as COMPLETE).
        self.assertEqual(_final_end_reason(True, "FALSE_CONTRACT"), "FALSE_CONTRACT")
        self.assertEqual(_final_end_reason(False, "FALSE_CONTRACT"), "FALSE_CONTRACT")
        self.assertEqual(_final_end_reason(False, "false_contract"), "FALSE_CONTRACT")

    def test_sibling_verus_fail_is_terminal(self):
        from run import _final_end_reason
        # A broken sibling/top-level module is not a cheat, but a target-only
        # green is still not "done" — never promote it to COMPLETE.
        self.assertEqual(
            _final_end_reason(True, "SIBLING_VERUS_FAIL"), "SIBLING_VERUS_FAIL")
        self.assertEqual(
            _final_end_reason(False, "sibling_verus_fail"), "SIBLING_VERUS_FAIL")


class GeneratedContractDrift(unittest.TestCase):
    def test_allows_contract_clause_drift_in_allow_edit_lemma_files(self):
        from run import _partition_generated_contract_drift
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lemma = root / "src/lemmas/ristretto_lemmas/coset_lemmas.rs"
            api = root / "src/ristretto.rs"
            drift = [
                {"file": str(lemma), "function": "lemma_bad", "field": "header"},
                {"file": str(lemma), "function": "lemma_bad", "field": "requires"},
                {"file": str(lemma), "function": "lemma_bad", "field": "ensures"},
                {"file": str(lemma), "function": "lemma_bad", "field": "decreases"},
                {"file": str(api), "function": "decompress", "field": "requires"},
            ]
            allowed, blocked = _partition_generated_contract_drift(
                drift, [lemma, api])
            self.assertEqual(allowed, drift[1:4])
            self.assertEqual(blocked, [drift[0], drift[4]])

    def test_blocks_non_contract_and_non_allow_edit_drift(self):
        from run import _partition_generated_contract_drift
        with tempfile.TemporaryDirectory() as td:
            lemma = Path(td) / "src/lemmas/foo.rs"
            other_lemma = Path(td) / "src/lemmas/other.rs"
            drift = [
                {"file": str(lemma), "function": "lemma_bad", "field": "requires"},
                {"file": str(lemma), "function": "axiom_bad", "field": "requires"},
                {"file": str(lemma), "function": "spec_helper", "field": "spec_body"},
                {"file": str(other_lemma), "function": "lemma_other", "field": "ensures"},
            ]
            allowed, blocked = _partition_generated_contract_drift(drift, [lemma])
            self.assertEqual(allowed, [drift[0]])
            self.assertEqual(blocked, drift[1:])

    def test_false_contract_claim_against_editable_generated_lemma_is_rejected(self):
        from run import _is_generated_editable_contract_claim
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lemma = root / "src/lemmas/field_lemmas/field_algebra_lemmas.rs"
            api = root / "src/ristretto.rs"
            self.assertTrue(
                _is_generated_editable_contract_claim(
                    lemma, "lemma_field_abs_neg", [lemma, api]))
            self.assertFalse(
                _is_generated_editable_contract_claim(
                    lemma, "axiom_field_abs_neg", [lemma, api]))
            self.assertFalse(
                _is_generated_editable_contract_claim(
                    api, "decompress", [lemma, api]))
            self.assertFalse(
                _is_generated_editable_contract_claim(
                    lemma, "lemma_field_abs_neg", [api]))

    def test_false_contract_verify_rejects_editable_generated_lemma_claim(self):
        from run import _verify_false_contract_claims
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "curve25519-dalek"
            lemma = project / "src/lemmas/field_lemmas/field_algebra_lemmas.rs"
            lemma.parent.mkdir(parents=True)
            lemma.write_text("verus! {}\n")
            tdir = root / "results" / "run" / "ristretto"
            tdir.mkdir(parents=True)
            (tdir / "false_contract_claims.json").write_text(json.dumps([{
                "function": "lemma_field_abs_neg",
                "file": "src/lemmas/field_lemmas/field_algebra_lemmas.rs",
                "witness": {"a": "p()"},
            }]))
            snapshot = tdir / "spec_snapshot.json"
            snapshot.write_text("{}")

            verified, unconfirmed = _verify_false_contract_claims(
                tdir, project, snapshot, allow_edit=[lemma])

            self.assertEqual(verified, [])
            self.assertEqual(len(unconfirmed), 1)
            self.assertEqual(
                unconfirmed[0]["failure_class"], "editable_generated_contract")
            self.assertIn("repair the contract", unconfirmed[0]["reason"])

    def test_spec_drift_diagnostic_partitions_generated_contracts(self):
        from run import _build_spec_drift_diagnostic
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lemma = root / "src/lemmas/field_lemmas/foo.rs"
            api = root / "src/ristretto.rs"
            drift = [
                {
                    "file": str(lemma),
                    "function": "lemma_generated",
                    "field": "ensures",
                },
                {
                    "file": str(api),
                    "function": "decompress",
                    "field": "ensures",
                },
                {
                    "file": str(lemma),
                    "function": "lemma_removed",
                    "change": "removed",
                },
            ]

            diag = _build_spec_drift_diagnostic(
                drift, [lemma, api], returncode=1, stderr_tail="tail")

            self.assertEqual(diag["returncode"], 1)
            self.assertEqual(diag["raw_drift"], drift)
            self.assertEqual(diag["allowed_generated_contract_drift"], [drift[0]])
            self.assertEqual(diag["blocking_drift"], drift[1:])
            self.assertEqual(diag["stderr_tail"], "tail")

    def test_spec_drift_verus_result_is_synthetic_and_source_located(self):
        from run import _spec_drift_verus_result

        result = _spec_drift_verus_result([{
            "file": "/work/curve25519-dalek/src/lemmas/foo.rs",
            "function": "local_pow2",
            "field": "header",
            "original": "spec fn local_pow2(n: nat) -> nat",
            "current": "#[verifier::memoize] spec fn local_pow2(n: nat) -> nat",
            "line": 48,
        }])

        self.assertFalse(result["okay"])
        self.assertTrue(result["skipped_verus_due_to_spec_drift"])
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["messages"][0]["file"], "/work/curve25519-dalek/src/lemmas/foo.rs")
        self.assertEqual(result["messages"][0]["line"], 48)
        self.assertIn("SPEC_DRIFT", result["messages"][0]["data"])
        self.assertIn("local_pow2", result["messages"][0]["data"])

    def test_false_contract_round_json_rewritten_when_unconfirmed(self):
        from lib.results import RoundResult
        from run import _finalize_false_contract_round
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            rr = RoundResult(
                round_number=2,
                end_reason="FALSE_CONTRACT",
                returncode=0,
                duration_seconds=1.0,
                verus_okay=False,
            )

            final_reason = _finalize_false_contract_round(tdir, rr, [])

            self.assertEqual(final_reason, "NEEDS_DECOMP")
            self.assertEqual(rr.end_reason, "NEEDS_DECOMP")
            written = json.loads((tdir / "round_2.json").read_text())
            self.assertEqual(written["end_reason"], "NEEDS_DECOMP")

    def test_false_contract_round_json_preserves_verified_false(self):
        from lib.results import RoundResult
        from run import _finalize_false_contract_round
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            rr = RoundResult(
                round_number=1,
                end_reason="FALSE_CONTRACT",
                returncode=0,
                duration_seconds=1.0,
                verus_okay=False,
            )

            final_reason = _finalize_false_contract_round(
                tdir, rr, [{"function": "decompress"}])

            self.assertEqual(final_reason, "FALSE_CONTRACT")
            written = json.loads((tdir / "round_1.json").read_text())
            self.assertEqual(written["end_reason"], "FALSE_CONTRACT")

    def test_forbidden_construct_is_terminal(self):
        from run import _final_end_reason
        # A new assume(...) / #[verifier::external_body] discharges an
        # obligation without SMT — a cheat in the same class as spec drift,
        # never promoted to COMPLETE even when verus is green.
        self.assertEqual(
            _final_end_reason(True, "FORBIDDEN_CONSTRUCT"), "FORBIDDEN_CONSTRUCT")
        self.assertEqual(
            _final_end_reason(False, "forbidden_construct"), "FORBIDDEN_CONSTRUCT")


class ClaudeNoResultExitClassification(unittest.TestCase):
    """Nonzero Claude exits without a final result event are infrastructure
    failures, not proof LIMIT/no-op rounds."""

    def test_retry_exhausted_when_last_event_is_api_retry(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text("\n".join([
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({
                    "type": "system",
                    "subtype": "api_retry",
                    "attempt": 10,
                    "max_retries": 10,
                    "error_status": None,
                    "error": "unknown",
                }),
            ]))
            info = run._classify_claude_no_result_exit(1, {}, raw)

        self.assertIsNotNone(info)
        self.assertEqual(info["reason"], "RETRY_EXHAUSTED")
        self.assertIn("retry 10/10", info["message"])
        self.assertIn("unknown", info["message"])

    def test_transport_error_for_other_no_result_nonzero_exit(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text(json.dumps({"type": "system", "subtype": "init"}))
            info = run._classify_claude_no_result_exit(1, {}, raw)

        self.assertIsNotNone(info)
        self.assertEqual(info["reason"], "TRANSPORT_ERROR")
        self.assertIn("rc=1", info["message"])

    def test_user_interrupted_when_raw_stream_says_interrupted(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text("\n".join([
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": "[Request interrupted by user]",
                        }],
                    },
                }),
            ]))
            info = run._classify_claude_no_result_exit(143, {}, raw)

        self.assertIsNotNone(info)
        self.assertEqual(info["reason"], "USER_INTERRUPTED")
        self.assertIn("user interruption", info["message"])

    def test_user_interrupted_tool_use_marker_is_detected(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text(json.dumps({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "content": "[Request interrupted by user for tool use]",
                    }],
                },
            }))
            info = run._classify_claude_no_result_exit(143, {}, raw)

        self.assertIsNotNone(info)
        self.assertEqual(info["reason"], "USER_INTERRUPTED")

    def test_signal_kill_is_not_transport_failure(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text(json.dumps({"type": "system", "subtype": "init"}))
            info = run._classify_claude_no_result_exit(-9, {}, raw)

        self.assertIsNone(info)

    def test_received_signal_is_persistable_interruption(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text("\n".join([
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({
                    "type": "system",
                    "subtype": "api_retry",
                    "attempt": 3,
                    "max_retries": 10,
                }),
            ]))
            info = run._classify_claude_no_result_exit(
                -9, {}, raw, interrupted_signal=15)

        self.assertIsNotNone(info)
        self.assertEqual(info["reason"], "INTERRUPTED_SIGNAL")
        self.assertEqual(info["signal"], 15)
        self.assertIn("signal 15", info["message"])

    def test_user_interrupt_marker_beats_signal_label(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text("[Request interrupted by user for tool use]")
            info = run._classify_claude_no_result_exit(
                -9, {}, raw, interrupted_signal=15)

        self.assertIsNotNone(info)
        self.assertEqual(info["reason"], "USER_INTERRUPTED")

    def test_success_or_result_event_is_not_transport_failure(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text("")
            self.assertIsNone(run._classify_claude_no_result_exit(0, {}, raw))
            self.assertIsNone(run._classify_claude_no_result_exit(
                1, {"type": "result"}, raw))


class RawUsageSummary(unittest.TestCase):
    """Raw stream usage observability must survive missing final result events."""

    def test_summarizes_assistant_result_model_and_task_progress_usage(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text("\n".join([
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "id": "msg1",
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 3,
                            "cache_read_input_tokens": 5,
                            "cache_creation_input_tokens": 7,
                        },
                        "content": [{"type": "text", "text": "x"}],
                    },
                }),
                json.dumps({
                    # Same message id/chunk usage repeats in stream-json; event
                    # sums record it, unique-message sums dedupe it.
                    "type": "assistant",
                    "message": {
                        "id": "msg1",
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 3,
                            "cache_read_input_tokens": 5,
                            "cache_creation_input_tokens": 7,
                        },
                        "content": [{"type": "tool_use", "name": "Bash"}],
                    },
                }),
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "id": "msg2",
                        "usage": {
                            "input_tokens": 11,
                            "output_tokens": 13,
                            "cache_read_input_tokens": 17,
                            "cache_creation_input_tokens": 19,
                        },
                    },
                }),
                json.dumps({
                    "type": "system",
                    "subtype": "task_progress",
                    "task_id": "a",
                    "usage": {
                        "total_tokens": 100,
                        "tool_uses": 2,
                        "duration_ms": 3000,
                    },
                }),
                json.dumps({
                    "type": "system",
                    "subtype": "task_progress",
                    "task_id": "a",
                    "usage": {
                        "total_tokens": 120,
                        "tool_uses": 3,
                        "duration_ms": 4000,
                    },
                }),
                json.dumps({
                    "type": "result",
                    "total_cost_usd": 4.25,
                    "usage": {
                        "input_tokens": 23,
                        "output_tokens": 29,
                        "cache_read_input_tokens": 31,
                        "cache_creation_input_tokens": 37,
                    },
                    "modelUsage": {
                        "claude-opus-4-8": {
                            "inputTokens": 41,
                            "outputTokens": 43,
                            "cacheReadInputTokens": 47,
                            "cacheCreationInputTokens": 53,
                            "costUSD": 5.5,
                            "contextWindow": 200000,
                            "maxOutputTokens": 64000,
                        },
                    },
                }),
                "not json",
            ]))

            summary = run.summarize_raw_usage(raw)

        self.assertEqual(summary["jsonl_lines"], 7)
        self.assertEqual(summary["parse_errors"], 1)
        self.assertEqual(summary["assistant_usage_events"], 3)
        self.assertEqual(summary["assistant_usage_unique_messages"], 2)
        self.assertEqual(
            summary["assistant_usage_event_sums"]
            ["cache_creation_input_tokens"],
            33,
        )
        self.assertEqual(
            summary["assistant_usage_unique_message_sums"]
            ["cache_creation_input_tokens"],
            26,
        )
        self.assertEqual(summary["result_events"], 1)
        self.assertEqual(summary["result_total_cost_usd_max"], 4.25)
        self.assertEqual(
            summary["model_usage_by_model_max"]["claude-opus-4-8"]
            ["cache_creation_input_tokens"],
            53,
        )
        self.assertEqual(summary["task_progress_events"], 2)
        self.assertEqual(summary["task_progress_latest_total_tokens"], 120)
        self.assertEqual(summary["task_progress_latest_tool_uses"], 3)

    def test_missing_raw_usage_summary_returns_zero_shape(self):
        import run

        summary = run.summarize_raw_usage(Path("/no/such/round.jsonl"))

        self.assertEqual(summary["jsonl_lines"], 0)
        self.assertEqual(summary["assistant_usage_events"], 0)
        self.assertEqual(summary["result_events"], 0)
        self.assertEqual(
            summary["assistant_usage_event_sums"]
            ["cache_creation_input_tokens"],
            0,
        )


class ClaudeMemoryCarryover(unittest.TestCase):
    """Claude Code auto-memory should be durable across harness fresh sessions."""

    def test_snapshot_copies_auto_memory_to_round_and_latest_dirs(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            memory = root / "home" / ".claude" / "projects" / "x" / "memory"
            memory.mkdir(parents=True)
            (memory / "MEMORY.md").write_text("- [state](state.md)\n")
            (memory / "state.md").write_text("remaining proof notes\n")
            raw = root / "round_1.jsonl"
            raw.write_text(json.dumps({
                "type": "system",
                "subtype": "init",
                "memory_paths": {"auto": str(memory)},
            }))
            tdir = root / "results" / "run" / "task"

            snap = run._snapshot_claude_memory(raw, tdir, 1)

            self.assertEqual(snap, tdir / "claude_memory" / "round_1")
            self.assertEqual(
                (tdir / "claude_memory" / "round_1" / "state.md").read_text(),
                "remaining proof notes\n",
            )
            self.assertEqual(
                (tdir / "claude_memory" / "latest" / "MEMORY.md").read_text(),
                "- [state](state.md)\n",
            )

    def test_fresh_session_prompt_includes_history_and_memory(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            latest = tdir / "claude_memory" / "latest"
            latest.mkdir(parents=True)
            (latest / "state.md").write_text("scalar.rs remaining: bit extraction\n")

            memory = run._render_claude_memory_carryover(tdir, max_chars=2000)
            prompt = run._fresh_session_prompt(
                "BASE PROMPT",
                "Continue from the previous rejected COMPLETE.",
                memory,
            )

        self.assertIn("BASE PROMPT", prompt)
        self.assertIn("Fresh-session carryover", prompt)
        self.assertIn("previous rejected COMPLETE", prompt)
        self.assertIn("scalar.rs remaining: bit extraction", prompt)


class ClassifyLemmaInAxiomFile(unittest.TestCase):
    """`run.classify_remaining_admits` must classify a `lemma_*` admit as
    'hard' (an unfinished proof to pursue) even in an axioms.rs file or under
    an 'Axiom:' docstring, while a real `axiom_*` stays 'intentional'.

    Pins the false-green fix (merge-pr2-clean 3cd1183): montgomery_curve_lemmas
    ran 8 rounds, closed 0 of 4 obligations, yet emitted COMPLETE because the
    lemma_* obligations sat under their original 'Axiom:' docstrings and were
    mis-flagged intentional. Without the lemma_* guard, `hard` undercounts and
    a never-proved module gets promoted LIMIT->COMPLETE."""

    def _classify(self, src: str, name: str = "axioms.rs") -> dict:
        from run import classify_remaining_admits
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / name
            p.write_text(src)
            return classify_remaining_admits(p)

    def test_lemma_in_axioms_file_is_hard(self):
        # File basename axioms.rs would flag everything intentional, but a
        # lemma_* admit is an unfinished proof.
        src = (
            "proof fn lemma_foo()\n"
            "    ensures true\n"
            "{\n"
            "    admit()\n"
            "}\n"
        )
        res = self._classify(src, name="axioms.rs")
        self.assertEqual(res["hard"], 1, res["detail"])
        self.assertEqual(res["intentional"], 0, res["detail"])

    def test_lemma_under_axiom_docstring_is_hard(self):
        src = (
            "/// Axiom: this used to be assumed.\n"
            "proof fn lemma_bar()\n"
            "    ensures true\n"
            "{\n"
            "    admit()\n"
            "}\n"
        )
        res = self._classify(src, name="curve_lemmas.rs")
        self.assertEqual(res["hard"], 1, res["detail"])

    def test_axiom_fn_stays_intentional(self):
        src = (
            "proof fn axiom_foundational()\n"
            "    ensures true\n"
            "{\n"
            "    admit()\n"
            "}\n"
        )
        res = self._classify(src, name="axioms.rs")
        self.assertEqual(res["intentional"], 1, res["detail"])
        self.assertEqual(res["hard"], 0, res["detail"])


class AreaTopLevelModules(unittest.TestCase):
    """`run._area_top_level_modules` maps a lemmas/<area>_lemmas sibling to the
    top-level module(s) that consume it — the sibling-verify gate re-checks
    those when the agent edits a helper."""

    def _map(self, p: str):
        from run import _area_top_level_modules
        return _area_top_level_modules(Path(p))

    def test_field_lemmas_maps_to_field_modules(self):
        self.assertEqual(
            self._map("/x/src/lemmas/field_lemmas/u64_5_lemmas.rs"),
            ["field", "backend::serial::u64::field"])

    def test_edwards_and_ristretto_areas(self):
        self.assertEqual(
            self._map("/x/src/lemmas/edwards_lemmas/mul_base_lemmas.rs"),
            ["edwards"])
        self.assertEqual(
            self._map("/x/src/lemmas/ristretto_lemmas/elligator_lemmas.rs"),
            ["ristretto"])

    def test_no_top_level_consumer_returns_empty(self):
        # common_lemmas has no top-level consumer; a non-lemmas path likewise.
        self.assertEqual(
            self._map("/x/src/lemmas/common_lemmas/foo.rs"), [])
        self.assertEqual(self._map("/x/src/edwards.rs"), [])


class AxiomFnNames(unittest.TestCase):
    """`lib.admits.axiom_fn_names` backs the axiom-integrity gate."""

    def test_captures_axiom_names_with_modifiers(self):
        from lib.admits import axiom_fn_names
        src = (
            "pub proof fn axiom_a() { admit() }\n"
            "broadcast proof fn axiom_b() { admit() }\n"
            "pub broadcast proof fn axiom_c() {}\n"
            "pub(crate) proof fn axiom_d() {}\n"
            "pub(super) proof fn axiom_e() {}\n"
            "proof fn lemma_not_axiom() {}\n"
            "spec fn axiom_lookalike() -> bool { true }\n"  # not a proof fn
        )
        self.assertEqual(
            axiom_fn_names(src),
            {"axiom_a", "axiom_b", "axiom_c", "axiom_d", "axiom_e"})

    def test_new_axiom_detected_by_set_diff(self):
        from lib.admits import axiom_fn_names
        before = axiom_fn_names("pub proof fn axiom_a() { admit() }\n")
        after = axiom_fn_names(
            "pub proof fn axiom_a() { admit() }\n"
            "proof fn axiom_cheat() { admit() }\n")
        self.assertEqual(after - before, {"axiom_cheat"})

    def test_ignores_axiom_lookalikes_in_comments_and_strings(self):
        from lib.admits import axiom_fn_names
        before = axiom_fn_names(
            "/* proof fn axiom_masked() { admit() } */\n"
            "let s = \"proof fn axiom_string() { admit() }\";\n")
        after = axiom_fn_names(
            "/* proof fn axiom_masked() { admit() } */\n"
            "let s = \"proof fn axiom_string() { admit() }\";\n"
            "proof fn axiom_masked() { admit() }\n")
        self.assertEqual(before, set())
        self.assertEqual(after - before, {"axiom_masked"})


class ForbiddenConstructCounter(unittest.TestCase):
    """`lib.admits.count_forbidden_constructs` backs the harness's
    forbidden-construct integrity gate (assume(...) / external_body)."""

    def test_counts_assume_and_external_body(self):
        from lib.admits import count_forbidden_constructs
        src = (
            "proof fn lemma_a() { assume(false); }\n"
            "#[verifier::external_body]\n"
            "proof fn lemma_b() ensures false {}\n"
            "proof fn lemma_c() { assume (x > 0); }\n"  # whitespace before (
        )
        c = count_forbidden_constructs(src)
        self.assertEqual(c["assume"], 2)
        self.assertEqual(c["external_body"], 1)

    def test_ignores_comments_and_strings(self):
        from lib.admits import count_forbidden_constructs
        src = (
            "// assume(false) in a comment is not real code\n"
            'let s = "external_body and assume() in a string";\n'
            "proof fn ok() {}\n"
        )
        c = count_forbidden_constructs(src)
        self.assertEqual(c["assume"], 0)
        self.assertEqual(c["external_body"], 0)

    def test_assume_specification_not_matched_as_assume(self):
        # `assume_specification` has no word boundary before the `_`, so the
        # bare-assume regex must not match it.
        from lib.admits import count_forbidden_constructs
        c = count_forbidden_constructs("assume_specification foo();\n")
        self.assertEqual(c["assume"], 0)

    def test_introduced_detected_by_count_diff(self):
        from lib.admits import count_forbidden_constructs
        before = count_forbidden_constructs("proof fn lemma_a() {}\n")
        after = count_forbidden_constructs(
            "proof fn lemma_a() { assume(false); }\n")
        self.assertGreater(after["assume"], before["assume"])


class ClassifyAcrossExtraFiles(unittest.TestCase):
    """`run.classify_remaining_admits(target, extra)` aggregates the admit
    classification across the target plus allow-edit dep files, matching the
    COMPLETE gate's `_count_gate_admits` scope, and tags each detail with its
    file."""

    def test_extra_files_are_aggregated(self):
        from run import classify_remaining_admits
        with tempfile.TemporaryDirectory() as td:
            tgt = Path(td) / "target.rs"
            dep = Path(td) / "dep.rs"
            tgt.write_text("proof fn lemma_t() { admit() }\n")
            dep.write_text("proof fn lemma_d() { admit() }\n")
            res = classify_remaining_admits(tgt, [dep])
            self.assertEqual(res["total"], 2, res["detail"])
            self.assertEqual(res["hard"], 2, res["detail"])
            files = {d["file"] for d in res["detail"]}
            self.assertEqual(files, {str(tgt), str(dep)})

    def test_extra_dedups_target(self):
        from run import classify_remaining_admits
        with tempfile.TemporaryDirectory() as td:
            tgt = Path(td) / "target.rs"
            tgt.write_text("proof fn lemma_t() { admit() }\n")
            # target also passed as an "extra" — must not be double-counted.
            res = classify_remaining_admits(tgt, [tgt])
            self.assertEqual(res["total"], 1, res["detail"])

    def test_no_extra_matches_single_file(self):
        from run import classify_remaining_admits
        with tempfile.TemporaryDirectory() as td:
            tgt = Path(td) / "target.rs"
            tgt.write_text("proof fn lemma_t() { admit() }\n")
            self.assertEqual(classify_remaining_admits(tgt)["total"], 1)


class RoundSnapshots(unittest.TestCase):
    """Per-round snapshots must be collision-free for multi-file runs."""

    def test_duplicate_basenames_get_distinct_snapshot_files(self):
        from run import _snapshot_name, snapshot_files
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a" / "mod.rs"
            b = root / "b" / "mod.rs"
            snap = root / "snap"
            a.parent.mkdir()
            b.parent.mkdir()
            a.write_text("proof fn from_a() {}\n")
            b.write_text("proof fn from_b() {}\n")

            files = [a, b]
            snapshot_files(files, snap)

            name_a = _snapshot_name(a, files)
            name_b = _snapshot_name(b, files)
            self.assertNotEqual(name_a, name_b)
            self.assertEqual((snap / name_a).read_text(), a.read_text())
            self.assertEqual((snap / name_b).read_text(), b.read_text())

    def test_partial_round_snapshot_writes_manifest(self):
        from run import _snapshot_name, snapshot_partial_round_state
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a" / "mod.rs"
            b = root / "b" / "mod.rs"
            missing = root / "missing.rs"
            a.parent.mkdir()
            b.parent.mkdir()
            a.write_text("proof fn from_a() {}\n")
            b.write_text("proof fn from_b() {}\n")
            files = [a, b, missing]

            manifest = snapshot_partial_round_state(files, root / "snapshots", 3)

            snap_dir = root / "snapshots" / "round_3_partial"
            self.assertEqual(Path(manifest["snapshot_dir"]), snap_dir)
            self.assertTrue(manifest["tainted"])
            self.assertEqual((snap_dir / _snapshot_name(a, files)).read_text(), a.read_text())
            self.assertEqual((snap_dir / _snapshot_name(b, files)).read_text(), b.read_text())
            written = json.loads((snap_dir / "manifest.json").read_text())
            self.assertEqual(written["round_number"], 3)
            self.assertTrue(written["tainted"])
            entries = {e["source"]: e for e in written["files"]}
            self.assertEqual(len(entries), 3)
            self.assertTrue(entries[str(a)]["exists"])
            self.assertTrue(entries[str(b)]["exists"])
            self.assertIn("sha256", entries[str(a)])
            self.assertIn("sha256", entries[str(b)])
            self.assertFalse(entries[str(missing)]["exists"])


class SpecDriftRecovery(unittest.TestCase):
    def test_restores_header_from_round0_and_keeps_proof_body(self):
        import argparse

        from run import _restore_spec_drift_from_baseline, snapshot_files
        from skills import spec_check

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.rs"
            snapshots_root = root / "snapshots"
            spec_snapshot = root / "spec_snapshot.json"
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
            target.write_text(baseline)
            snapshot_files([target], snapshots_root / "round_0")
            spec_check.cmd_snapshot(
                argparse.Namespace(target=target, out=spec_snapshot, siblings=[]))
            target.write_text(
                baseline.replace("requires x < 10,", "requires x < 100,")
                        .replace("admit();", "assert(x < 11);")
            )
            drift = spec_check._verify_one(
                str(target),
                spec_check._extract_sigs(baseline),
                spec_check._extract_sigs(target.read_text()),
                check_spec_defs=True,
            )

            recovery = _restore_spec_drift_from_baseline(
                drift, [target], snapshots_root, target, spec_snapshot, {}, None)

            self.assertTrue(recovery["okay"], recovery)
            text = target.read_text()
            self.assertIn("requires x < 10,", text)
            self.assertIn("assert(x < 11);", text)
            self.assertEqual(recovery["residual_drift"], [])

    def test_restore_fails_closed_when_round0_snapshot_missing(self):
        from run import _restore_spec_drift_from_baseline

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.rs"
            target.write_text("verus! {\nproof fn lemma_a() { }\n}\n")
            drift = [{
                "file": str(target),
                "function": "lemma_a",
                "key": "lemma_a",
                "field": "header",
            }]

            recovery = _restore_spec_drift_from_baseline(
                drift, [target], root / "snapshots", target,
                root / "spec_snapshot.json", {}, None)

            self.assertFalse(recovery["okay"])
            self.assertIn(str(target), recovery["unresolved"])

    def test_restore_falls_back_to_full_file_for_removed_function(self):
        import argparse

        from run import _restore_spec_drift_from_baseline, snapshot_files
        from skills import spec_check

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.rs"
            snapshots_root = root / "snapshots"
            spec_snapshot = root / "spec_snapshot.json"
            baseline = (
                "verus! {\n"
                "proof fn lemma_a()\n"
                "    ensures true,\n"
                "{\n"
                "    admit();\n"
                "}\n"
                "proof fn lemma_b() { }\n"
                "}\n"
            )
            current = "verus! {\nproof fn lemma_b() { assert(true); }\n}\n"
            target.write_text(baseline)
            snapshot_files([target], snapshots_root / "round_0")
            spec_check.cmd_snapshot(
                argparse.Namespace(target=target, out=spec_snapshot, siblings=[]))
            target.write_text(current)
            drift = spec_check._verify_one(
                str(target),
                spec_check._extract_sigs(baseline),
                spec_check._extract_sigs(current),
                check_spec_defs=True,
            )

            recovery = _restore_spec_drift_from_baseline(
                drift, [target], snapshots_root, target, spec_snapshot, {}, None)

            self.assertTrue(recovery["okay"], recovery)
            self.assertEqual(recovery["full_file_restored"], [str(target)])
            self.assertEqual(target.read_text(), baseline)


class EndReasonRegex(unittest.TestCase):
    """`run.END_REASON_RE` parses the agent's `END_REASON:<TOKEN>` line.
    Pin that NEEDS_DECOMP is recognised alongside COMPLETE/LIMIT, that the
    match is case-insensitive and line-anchored, and that prose merely
    mentioning the token does not match."""

    def _last(self, text: str):
        from run import END_REASON_RE
        matches = END_REASON_RE.findall(text)
        return matches[-1].upper() if matches else None

    def test_recognises_all_three_tokens(self):
        self.assertEqual(self._last("END_REASON:COMPLETE"), "COMPLETE")
        self.assertEqual(self._last("END_REASON:LIMIT"), "LIMIT")
        self.assertEqual(self._last("END_REASON:NEEDS_DECOMP"), "NEEDS_DECOMP")

    def test_case_insensitive(self):
        self.assertEqual(self._last("end_reason:needs_decomp"), "NEEDS_DECOMP")

    def test_last_token_wins_over_earlier_mention(self):
        # Agent reasons aloud, then commits on the final line.
        text = ("I considered END_REASON:LIMIT but the lemma is missing.\n"
                "MISSING: lemma_reduce_chain_5\n"
                "END_REASON:NEEDS_DECOMP\n")
        self.assertEqual(self._last(text), "NEEDS_DECOMP")

    def test_inline_prose_mention_does_not_match(self):
        # No line is *just* the token, so nothing matches (line-anchored).
        self.assertIsNone(
            self._last("emit END_REASON:NEEDS_DECOMP when blocked"))


# ---------- admit-skeleton creation (mode-aware) ------------------------
# Ported from inference-dalek tests/test_starting_state.py (the pure-text
# core of construct_admitted_state). These pin the *correct*, mode-aware
# admitter: only proof fn bodies + inline proof {} blocks are admitted;
# axiom_* fns, spec fn defs, and exec code are preserved.

class FindProofFnBodyBrace(unittest.TestCase):
    """`find_proof_fn_body_brace` locates the body-opening `{`, skipping
    Verus clause braces (`forall ==> {}`, `by {}`, `if/else {}`)."""

    def test_simple_proof_fn(self):
        code = "proof fn foo() { body }"
        self.assertEqual(
            find_proof_fn_body_brace(code, code.index("proof")),
            code.index("{"))

    def test_body_brace_on_own_line(self):
        code = (
            "proof fn foo(x: int)\n"
            "    requires\n"
            "        x > 0,\n"
            "    ensures\n"
            "        x + 1 > 1,\n"
            "{\n"
            "    admit()\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("proof"))
        self.assertIsNotNone(r)
        self.assertEqual(code[r], "{")
        self.assertEqual(code[r - 1], "\n")

    def test_skips_forall_implies_brace(self):
        code = (
            "pub proof fn lemma(digits: Seq<Seq<i8>>)\n"
            "    requires\n"
            "        forall|k: int|\n"
            "            0 <= k < digits.len() ==> {\n"
            "                &&& digits[k].len() == 64\n"
            "            },\n"
            "    ensures\n"
            "        result == true,\n"
            "{\n"
            "    admit()\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("pub proof"))
        self.assertIsNotNone(r)
        body_line_start = code.rfind("\n", 0, r) + 1
        self.assertEqual(code[body_line_start:r].strip(), "")

    def test_skips_by_brace_in_clause(self):
        code = (
            "proof fn lemma(x: int)\n"
            "    requires\n"
            "        x > 0,\n"
            "    ensures\n"
            "        (x + 1 > 1) by {\n"
            "            // clause proof\n"
            "        },\n"
            "{\n"
            "    admit()\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("proof"))
        self.assertIsNotNone(r)
        body_line_start = code.rfind("\n", 0, r) + 1
        self.assertEqual(code[body_line_start:r].strip(), "")

    def test_skips_if_else_braces(self):
        code = (
            "proof fn lemma(x: int) -> (r: int)\n"
            "    ensures\n"
            "        r == if x > 0 { x } else { -x },\n"
            "{\n"
            "    admit();\n"
            "    0\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("proof"))
        self.assertIsNotNone(r)
        body_line_start = code.rfind("\n", 0, r) + 1
        self.assertEqual(code[body_line_start:r].strip(), "")

    def test_one_liner_fn_sig_line(self):
        code = "pub proof fn lemma_trivial() { admit() }"
        self.assertEqual(
            find_proof_fn_body_brace(code, code.index("pub proof")),
            code.index("{"))

    def test_pub_crate_proof_fn(self):
        code = (
            "pub(crate) proof fn helper()\n"
            "    requires true,\n"
            "{\n"
            "    admit()\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("pub(crate)"))
        self.assertIsNotNone(r)
        self.assertEqual(code[r], "{")

    def test_no_brace_returns_none(self):
        self.assertIsNone(find_proof_fn_body_brace("proof fn foo()", 0))

    def test_real_straus_pattern(self):
        code = (
            "pub proof fn lemma_straus_ct_correct(\n"
            "    scalars: Seq<Scalar>,\n"
            "    digits: Seq<Seq<i8>>,\n"
            ")\n"
            "    requires\n"
            "        scalars.len() == digits.len(),\n"
            "        forall|k: int|\n"
            "            0 <= k < digits.len() ==> {\n"
            "                &&& (#[trigger] digits[k]).len() == 64\n"
            "                &&& radix_16_all_bounded_seq(digits[k])\n"
            "            },\n"
            "    ensures\n"
            "        straus_ct_partial(digits, 0) == true,\n"
            "    decreases scalars.len(),\n"
            "{\n"
            "    let n = scalars.len();\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("pub proof"))
        self.assertIsNotNone(r)
        body_line_start = code.rfind("\n", 0, r) + 1
        self.assertEqual(code[body_line_start:r].strip(), "")
        self.assertIn("let n = scalars.len();", code[r:])


class AdmitProofFnBodies(unittest.TestCase):
    """`admit_proof_fn_bodies` replaces proof fn bodies with a type-correct
    admit() skeleton, keeping signatures + clauses, skipping
    axiom_*/exec/spec fns."""

    def test_simple_admit(self):
        result = admit_proof_fn_bodies(
            "proof fn foo() {\n    some_proof_code();\n}")
        self.assertIn("admit()", result)
        self.assertNotIn("some_proof_code", result)

    def test_preserves_requires_ensures(self):
        code = (
            "proof fn lemma(x: int)\n"
            "    requires\n"
            "        x > 0,\n"
            "    ensures\n"
            "        x + 1 > 1,\n"
            "{\n"
            "    // complex proof\n"
            "    assert(x + 1 > 1);\n"
            "}"
        )
        result = admit_proof_fn_bodies(code)
        for s in ("requires", "x > 0", "ensures", "x + 1 > 1", "admit()"):
            self.assertIn(s, result)
        self.assertNotIn("complex proof", result)

    def test_bool_return_type(self):
        code = (
            "proof fn check(x: int) -> (b: bool)\n"
            "    ensures b == (x > 0),\n"
            "{\n"
            "    x > 0\n"
            "}"
        )
        result = admit_proof_fn_bodies(code)
        self.assertIn("admit();", result)
        self.assertIn("true", result)

    def test_int_return_type(self):
        code = (
            "proof fn compute(x: int) -> (n: int)\n"
            "    ensures n >= 0,\n"
            "{\n"
            "    x.abs()\n"
            "}"
        )
        result = admit_proof_fn_bodies(code)
        self.assertIn("admit();", result)
        self.assertIn("\n    0\n", result)

    def test_unit_return_type(self):
        result = admit_proof_fn_bodies(
            "proof fn lemma() {\n    some_proof();\n}")
        self.assertIn("{\n    admit()\n}", result)

    def test_unnamed_return_falls_through(self):
        # Documented & kept boundary: only the named `-> (n: T)` form is
        # detected; an unnamed `-> bool` falls through to a bare admit()
        # (no trailing value). See `_admit_body_for_return`.
        result = admit_proof_fn_bodies(
            "proof fn f() -> bool {\n    real();\n}")
        self.assertIn("{\n    admit()\n}", result)
        self.assertNotIn("true", result)
        self.assertNotIn("real()", result)

    def test_multiple_proof_fns(self):
        code = (
            "proof fn a() {\n    proof_a();\n}\n\n"
            "proof fn b() {\n    proof_b();\n}\n"
        )
        result = admit_proof_fn_bodies(code)
        self.assertEqual(result.count("admit()"), 2)
        self.assertNotIn("proof_a", result)
        self.assertNotIn("proof_b", result)

    def test_skips_non_proof_fns(self):
        # The whole point of the mode-aware admitter: exec bodies survive.
        code = (
            "pub fn exec_fn() { runtime_code(); }\n\n"
            "proof fn lemma() { proof_code(); }\n"
        )
        result = admit_proof_fn_bodies(code)
        self.assertIn("runtime_code", result)    # exec fn body preserved
        self.assertNotIn("proof_code", result)   # proof fn body admitted
        self.assertIn("admit()", result)

    def test_skips_axiom_fns(self):
        # axiom_* bodies are trusted and must be preserved.
        code = "proof fn axiom_trust() {\n    trusted_axiom_body();\n}\n"
        result = admit_proof_fn_bodies(code)
        self.assertIn("trusted_axiom_body", result)
        self.assertNotIn("admit()", result)

    def test_forall_clause_not_admitted(self):
        code = (
            "pub proof fn lemma(digits: Seq<Seq<i8>>)\n"
            "    requires\n"
            "        forall|k: int|\n"
            "            0 <= k < digits.len() ==> {\n"
            "                &&& digits[k].len() == 64\n"
            "            },\n"
            "{\n"
            "    // proof body\n"
            "}"
        )
        result = admit_proof_fn_bodies(code)
        self.assertIn("digits[k].len() == 64", result)  # clause preserved
        self.assertNotIn("proof body", result)
        self.assertIn("admit()", result)


class AdmitProofBlocks(unittest.TestCase):
    """`admit_proof_blocks` hollows inline `proof { ... }` blocks inside
    exec fns to `{ admit(); }`, preserving surrounding exec code."""

    def test_simple_proof_block(self):
        code = (
            "fn exec_fn() {\n"
            "    let x = 1;\n"
            "    proof {\n"
            "        assert(x > 0);\n"
            "    }\n"
            "    let y = 2;\n"
            "}"
        )
        result = admit_proof_blocks(code)
        self.assertIn("{ admit(); }", result)
        self.assertNotIn("assert(x > 0)", result)
        self.assertIn("let x = 1;", result)
        self.assertIn("let y = 2;", result)


class FieldFloorPromptScope(unittest.TestCase):
    """The field-floor cut (peel --classify-floor field: the whole above-field
    correctness cone — ristretto/scalar lemma dirs deleted + all 4 APIs stripped)
    must get a data-driven, editable-list-is-truth prompt, NOT the curated
    decompress full-stack text. The decompress text asserts 'only these ~15
    lemmas deleted; ristretto_lemmas frozen', which contradicts the field-floor
    cut and made the agent declare the tree corrupted / invent other workers
    (peel_corefloor_002/003). Pin the split so it can't silently regress."""

    _BASE = Path("/wt/curve25519-dalek/src")

    def _block(self, rels, mode):
        import run
        allow = [self._BASE / r for r in rels]
        return run.build_experiment_block(
            self._BASE / "ristretto.rs", allow, mode=mode)

    def test_field_floor_mode_gets_field_floor_prompt(self):
        # The dedicated field-floor mode → field-floor rung, never the decompress
        # enumeration that (falsely) freezes ristretto_lemmas.
        out = self._block([
            "ristretto.rs", "edwards.rs", "montgomery.rs", "scalar.rs",
            "lemmas/edwards_lemmas/straus_lemmas.rs",
            "lemmas/ristretto_lemmas/batch_compress_lemmas.rs",
            "lemmas/scalar_lemmas_/naf_lemmas.rs",
            "lemmas/scalar_byte_lemmas/scalar_to_bytes_lemmas.rs",
        ], mode="field-floor")
        self.assertIn("**field-floor** rung", out)
        self.assertNotIn("ENTIRE decompress proof tree", out)
        self.assertNotIn("ristretto lemma layer", out)  # the false "frozen" claim
        self.assertIn("only agent", out)                # counters the worker hallucination
        self.assertIn("Ignore any prior memory", out)
        self.assertIn("top-level API proof bodies", out)
        self.assertIn("scope, and only their contracts are frozen", out)
        self.assertIn("not spawn helper agents", out)
        self.assertNotIn("Delegate read-heavy exploration", out)

    def test_field_floor_prompt_uses_generic_dependency_order(self):
        out = self._block([
            "lemmas/scalar_byte_lemmas/bytes_to_scalar_lemmas.rs",
            "lemmas/scalar_byte_lemmas/scalar_to_bytes_lemmas.rs",
        ], mode="field-floor")

        self.assertIn("Write ONLY proof artifacts", out)
        self.assertIn("you MAY draft the body with `admit()` to unblock compile", out)
        self.assertIn("COMPLETE requires every such admit discharged into a real proof", out)
        self.assertIn("Current lane packet (strict)", out)
        self.assertIn("current proof lane is exactly the editable list above", out)
        self.assertIn("frozen consumers whose obligations trace directly", out)
        self.assertIn("Do not chase unrelated APIs or broad whole-crate buckets", out)
        self.assertIn("A downstream error is lane signal only if", out)
        self.assertIn("current editable contract", out)
        self.assertIn("Reduced-but-nonzero admits are partial progress", out)
        self.assertIn("Lane bank and integration rhythm", out)
        self.assertIn("Do not create an off-lane stub profile on this first attempt", out)
        self.assertIn("Stubs can mask the flawed-contract failure mode", out)
        self.assertIn("de-stubbed whole-crate integration check must gate any lane bank", out)
        self.assertIn("operator-owned off-lane compile-debt stubs already present", out)
        self.assertIn("leave them alone", out)
        self.assertIn("Do not expand, repair, count, or chase those stubs", out)
        self.assertIn("Lane filter comes before compile resolution", out)
        self.assertIn("dependency trace reaches one of the editable files above", out)
        self.assertIn("do not reconstruct them in this run", out)
        self.assertIn("next-lane compile debt", out)
        self.assertIn("choose a narrower current-lane check", out)
        self.assertIn("direct dependency path", out)
        self.assertNotIn("current proof lane is scalar Montgomery reduction", out)
        self.assertNotIn("editable Montgomery-reduction lemma files", out)
        self.assertNotIn("lane-relevant `scalar.rs` / backend proof blocks", out)
        self.assertNotIn("Pippenger/Straus/digit/radix/NAF", out)
        self.assertIn("apply the lane filter above before deciding which callsites are current", out)
        self.assertIn("Natural-language scheduler (strict)", out)
        self.assertIn("choose the next action by this order", out)
        self.assertIn("do not skip a step", out)
        self.assertIn("current unresolved lane callsites", out)
        self.assertIn("narrower lane check", out)
        self.assertIn("trace the immediate dependency chain", out)
        self.assertIn("Pick the lowest unproved dependency", out)
        self.assertIn("bank that one proof thread before moving upward", out)
        self.assertIn("This is a scheduler, not optional advice", out)
        self.assertIn("Compile resolution is not proof progress", out)
        self.assertIn("minimal lane-filtered signature and contract", out)
        self.assertIn("even if that missing function is a high-level consumer", out)
        self.assertIn("first necessary step before any real leaf signal", out)
        self.assertIn("An unproved body or a too-weak contract is debt", out)
        self.assertIn("Stay on the discovered thread", out)
        self.assertIn("`admit()` is allowed as draft / compile debt", out)
        self.assertIn("keep it minimal to the current unresolved callsites", out)
        self.assertIn("never count it as proof progress", out)
        self.assertIn("blocks COMPLETE until discharged one thread at a time", out)
        self.assertIn("sprawling new admits across the cone", out)
        self.assertNotIn("Write ONLY proofs:", out)
        self.assertNotIn("and a real proof. You choose", out)
        self.assertNotIn("Dependency-order hint (recommended)", out)
        self.assertNotIn("following it rigidly", out)
        self.assertNotIn("Do not add admitted skeletons", out)
        self.assertNotIn("do not leave new non-axiom admits as placeholders", out)
        self.assertNotIn("Default field-floor proof order", out)
        self.assertNotIn("If the target is `scalar.rs`", out)
        self.assertNotIn("Do not make Pippenger", out)
        self.assertNotIn("field-adjacent lemmas first", out)

    def test_scalar_montgomery_structural_lane_is_lane_first(self):
        out = self._block([
            "lemmas/scalar_lemmas_/montgomery_reduce_lemmas.rs",
            "lemmas/scalar_lemmas_/montgomery_reduce_part1_chain_lemmas.rs",
            "lemmas/scalar_lemmas_/montgomery_reduce_part2_chain_lemmas.rs",
            "scalar.rs",
        ], mode="field-floor")

        self.assertIn("**field-floor scalar-Montgomery lane** rung", out)
        self.assertIn("assignment is ONLY the scalar Montgomery reduction lane", out)
        self.assertIn("This lane section wins", out)
        self.assertIn("scalar.rs` is editable for scalar-Montgomery proof blocks", out)
        self.assertIn("Scalar API proof blocks are allowed when they trace to this lane", out)
        self.assertIn("frozen compile shield, out of scope, and non-scoreable", out)
        self.assertIn("Lane acceptance, not crate banking", out)
        self.assertIn("a scoped module/file green is only", out)
        self.assertIn("every editable lane/thread file", out)
        self.assertIn("zero non-axiom admits and zero in-scope source errors", out)
        self.assertIn("lane-local `proof_delta`, not", out)
        self.assertIn("`BANKED_COMPLETE`", out)
        self.assertIn("Prior Montgomery proofs are seed evidence, not a cache", out)
        self.assertIn("Re-verify every reused proof against the current", out)
        self.assertIn("Convergence framing", out)
        self.assertIn("assisted convergence with operator", out)
        self.assertIn("Section 11 unaided-convergence", out)
        self.assertIn("Use scoped Montgomery/scalar checks for steering", out)
        self.assertIn("Natural-language scheduler (strict)", out)
        self.assertIn("This is a scheduler, not optional advice", out)
        self.assertNotIn("hardest and BROADEST", out)
        self.assertNotIn("reconstruct ALL", out)
        self.assertNotIn("whole cone is the task", out)
        self.assertNotIn("EVERY editable file below is yours to complete", out)

    def test_scalar_montgomery_thread_packet_is_lane_first(self):
        out = self._block([
            "lemmas/scalar_lemmas_/montgomery_reduce_lemmas.rs",
        ], mode="field-floor")

        self.assertIn("**field-floor scalar-Montgomery lane** rung", out)
        self.assertIn("Editable lane/thread files", out)
        self.assertIn("Single-thread convergence rule", out)
        self.assertIn("Bank one current proof thread", out)
        self.assertNotIn("hardest and BROADEST", out)
        self.assertNotIn("reconstruct ALL", out)
        self.assertNotIn("whole cone is the task", out)

    def test_field_floor_lists_all_editable_lemma_files(self):
        # Data-driven: the prompt enumerates the editable lemma + API files, not
        # a hardcoded named subset.
        out = self._block([
            "scalar.rs", "lemmas/scalar_byte_lemmas/scalar_to_bytes_lemmas.rs",
        ], mode="field-floor")
        self.assertIn("scalar_to_bytes_lemmas.rs", out)  # listed under (A)
        self.assertIn("scalar.rs", out)                  # listed under (B)

    def test_field_floor_mode_detects_number_theory_floor(self):
        # Manifests still dispatch as mode="field-floor"; run.py must infer the
        # deeper number-theory floor from editable field-layer files.
        out = self._block([
            "ristretto.rs",
            "lemmas/field_lemmas/add_lemmas.rs",
            "backend/serial/u64/field.rs",
        ], mode="field-floor")
        self.assertIn("**number-theory-floor** rung", out)
        self.assertIn("Field layer also peeled", out)
        self.assertNotIn("**field-floor** rung", out)
        self.assertNotIn("EVERYTHING below the field layer is frozen", out)
        self.assertNotIn("The L4 field proof layer", out)

    def test_field_floor_mode_detects_trusted_core_floor(self):
        # Trusted-core includes specs/common/backend/helper files. The prompt
        # must not tell the agent those editable files are frozen.
        out = self._block([
            "ristretto.rs",
            "specs/edwards_specs.rs",
            "lemmas/common_lemmas/to_nat_lemmas.rs",
            "backend/serial/u64/scalar.rs",
            "traits.rs",
        ], mode="field-floor")
        self.assertIn("**trusted-core** rung", out)
        self.assertIn("Entire in-repo proof layer peeled", out)
        self.assertIn("spec-module proof lemmas", out)
        self.assertIn("MAY add new real helper", out)
        self.assertNotIn("**field-floor** rung", out)
        self.assertNotIn("EVERYTHING below the field layer is frozen", out)
        self.assertNotIn("common_lemmas/*` substrate, the backend", out)

    def test_curated_decompress_fullstack_unchanged(self):
        # The 5-file decompress rung under bridge-full keeps its hardcoded
        # enumeration — the new field-floor mode must not capture it.
        out = self._block([
            "ristretto.rs", "edwards.rs", "montgomery.rs",
            "lemmas/edwards_lemmas/decompress_lemmas.rs",
            "lemmas/edwards_lemmas/curve_equation_lemmas.rs",
        ], mode="bridge-full")
        self.assertIn("ENTIRE decompress proof tree", out)
        self.assertNotIn("**field-floor** rung", out)

    def test_default_rendered_prompt_keeps_target_scope(self):
        import run

        tool_flags = ",".join(run.AGENT_TOOL_FLAGS)
        self.assertNotIn("TodoWrite", tool_flags)
        self.assertNotIn("Task", tool_flags)
        self.assertNotIn("Agent", tool_flags)

        out = run.render_prompt(
            target=self._BASE / "ristretto.rs",
            project=self._BASE.parent,
            module="ristretto",
            spec_snapshot=Path("/tmp/spec.json"),
            catalog_cache=Path("/tmp/catalog.json"),
            results_root=Path("/tmp/results"),
            failure_block="",
        )
        self.assertIn("Work only on the target file", out)
        self.assertIn("Edit only the target file", out)
        self.assertIn("Read the target file", out)
        self.assertIn("You may use `admit()` as a TEMPORARY checkpoint", out)
        self.assertIn("## General proof-craft rules", out)
        self.assertIn("Never add a new `axiom_*`", out)
        self.assertIn("use `use_type_invariant(...)`", out)
        self.assertIn("add and prove a small shared helper", out)
        self.assertIn("explicit `decreases`", out)
        self.assertIn("Separate bit-level facts from arithmetic facts", out)
        self.assertIn("Write a short checklist", out)
        self.assertNotIn("TodoWrite", out)
        self.assertNotIn("Delegate read-heavy exploration", out)
        self.assertIn(f"Skill scripts: `{run.HERE / 'skills'}`", out)
        self.assertIn(f"Read {run.HERE / 'skills' / 'SKILL.md'}", out)
        self.assertIn("$SPEC_SNAPSHOT", out)
        self.assertIn("do not use broad", out)
        self.assertIn("pkill", out)
        self.assertIn("shell `timeout`", out)
        self.assertIn("verus_check.py --timeout N", out)
        self.assertIn("cargo-verus focus", out)
        self.assertIn("module filters into", out)
        self.assertIn("available modules are", out)
        self.assertIn("2>&1 | python", out)
        self.assertIn("JSONDecodeError", out)
        self.assertIn("sleep N; cat", out)
        self.assertIn("tasks/*.output", out)
        self.assertIn(f"pass the target as `{self._BASE / 'ristretto.rs'}`", out)
        self.assertIn(f"the project as `{self._BASE.parent}`", out)
        self.assertIn("Do not shorten them to `src/...` or `--project .`", out)
        self.assertIn("rg -n PATTERN src -g '*.rs'", out)
        self.assertIn("grep --include=*.rs", out)
        self.assertIn("--include='*.rs'", out)
        self.assertIn("remembered dalek file paths", out)
        self.assertIn("rg --files src", out)
        self.assertIn("search by symbol", out)
        self.assertIn("Root source discovery at the current Cargo project", out)
        self.assertIn("global filesystem searches", out)
        self.assertIn("find /", out)
        self.assertIn("attached `///` doc", out)
        self.assertIn("Orphaned docs/attributes", out)
        self.assertIn("unexpected token, expected ;", out)
        self.assertIn("error_texts[]", out)
        self.assertIn("not a reason to churn visibility", out)
        self.assertIn("large source files", out)
        self.assertIn("numeric", out)
        self.assertIn("read the exact file window", out)
        self.assertIn("/tmp/vcheck.json", out)
        self.assertNotIn("python3 skills/", out)
        self.assertNotIn("{SKILLS_ROOT}", out)
        self.assertNotIn("{SKILL_DOC}", out)
        self.assertIn("If even one NON-AXIOM `admit()` remains in the target", out)
        self.assertIn(
            f"python3 {run.HERE / 'skills' / 'admit_inventory.py'} {self._BASE / 'ristretto.rs'}",
            out,
        )

    def test_whole_crate_rendered_prompt_uses_editable_scope(self):
        import run

        out = run.render_prompt(
            target=self._BASE / "ristretto.rs",
            project=self._BASE.parent,
            module="ristretto",
            spec_snapshot=Path("/tmp/spec.json"),
            catalog_cache=Path("/tmp/catalog.json"),
            results_root=Path("/tmp/results"),
            failure_block="",
            experiment_block="## EXPERIMENT MODE\nEditable files are listed here.",
            whole_crate_assignment=True,
            verus_rlimit=80.0,
        )
        self.assertIn("target path is the harness anchor", out)
        self.assertIn("your job is every editable file", out)
        self.assertIn("editable list, not the target path", out)
        self.assertIn("Read the failing editable file(s)", out)
        self.assertIn("Write a short checklist", out)
        self.assertNotIn("TodoWrite", out)
        self.assertNotIn("Delegate read-heavy exploration", out)
        self.assertIn(f"Skill scripts: `{run.HERE / 'skills'}`", out)
        self.assertIn("$SPEC_SNAPSHOT", out)
        self.assertIn("do not use broad", out)
        self.assertIn("pkill", out)
        self.assertIn("shell `timeout`", out)
        self.assertIn("verus_check.py --timeout N", out)
        self.assertIn("cargo-verus focus", out)
        self.assertIn("module filters into", out)
        self.assertIn("available modules are", out)
        self.assertIn("2>&1 | python", out)
        self.assertIn("JSONDecodeError", out)
        self.assertIn("sleep N; cat", out)
        self.assertIn("tasks/*.output", out)
        self.assertIn(f"pass the target as `{self._BASE / 'ristretto.rs'}`", out)
        self.assertIn(f"the project as `{self._BASE.parent}`", out)
        self.assertIn("Do not shorten them to `src/...` or `--project .`", out)
        self.assertIn("rg -n PATTERN src -g '*.rs'", out)
        self.assertIn("grep --include=*.rs", out)
        self.assertIn("--include='*.rs'", out)
        self.assertIn("remembered dalek file paths", out)
        self.assertIn("rg --files src", out)
        self.assertIn("search by symbol", out)
        self.assertIn("Root source discovery at the current Cargo project", out)
        self.assertIn("global filesystem searches", out)
        self.assertIn("find /", out)
        self.assertIn("attached `///` doc", out)
        self.assertIn("Orphaned docs/attributes", out)
        self.assertIn("unexpected token, expected ;", out)
        self.assertIn("error_texts[]", out)
        self.assertIn("not a reason to churn visibility", out)
        self.assertIn("large source files", out)
        self.assertIn("numeric", out)
        self.assertIn("read the exact file window", out)
        self.assertNotIn("python3 skills/", out)
        self.assertNotIn("{SKILLS_ROOT}", out)
        self.assertNotIn("{SKILL_DOC}", out)
        self.assertIn("target path plus every other editable file", out)
        self.assertIn("--whole-crate --timeout", out)
        self.assertNotIn("--rlimit 80.0", out)
        self.assertIn("any editable file listed in", out)
        self.assertIn("full editable", out)
        self.assertIn("--siblings <editable-a.rs>", out)
        self.assertIn("no new `admit()`", out)
        self.assertIn("let Verus report the remaining obligations", out)
        self.assertIn("## General proof-craft rules", out)
        self.assertIn("Never add a new `axiom_*`", out)
        self.assertIn("use `use_type_invariant(...)`", out)
        self.assertIn("add and prove a small shared helper", out)
        self.assertIn("explicit `decreases`", out)
        self.assertIn("Separate bit-level facts from arithmetic facts", out)
        self.assertNotIn("You may use `admit()` as a TEMPORARY checkpoint", out)
        self.assertNotIn("Work only on the target file", out)
        self.assertNotIn("Edit only the target file", out)
        self.assertNotIn("Read the target file", out)
        self.assertNotIn("remains in the target (or in any", out)
        self.assertNotIn(
            f"python3 {run.HERE / 'skills' / 'admit_inventory.py'} {self._BASE / 'ristretto.rs'}\n",
            out,
        )


class FeedbackVisibilityHelpers(unittest.TestCase):
    """The corefloor_006 plateau was a blindness bug: whole-crate errors stored
    first-20 (all one module) and rendered from a wrong key, and no hard-admit
    inventory. Pin the two helpers that restore sight."""

    def test_diversify_spreads_across_files(self):
        import run
        msgs = ([{"file": "edwards.rs", "line": i, "data": "e"} for i in range(50)]
                + [{"file": "scalar.rs", "line": 1, "data": "s"},
                   {"file": "ristretto.rs", "line": 1, "data": "r"}])
        out = run._diversify_messages(msgs, cap=24, per_file=4)
        files = {m["file"] for m in out}
        # all three modules represented, edwards not allowed to crowd them out
        self.assertEqual(files, {"edwards.rs", "scalar.rs", "ristretto.rs"})
        self.assertEqual(sum(1 for m in out if m["file"] == "edwards.rs"), 4)

    def test_diversify_handles_non_dicts_and_cap(self):
        import run
        out = run._diversify_messages(["x", "y", "z"], cap=2)
        self.assertEqual(len(out), 2)

    def test_format_diagnostics_for_memory_preserves_locations(self):
        import run
        out = run._format_diagnostics_for_memory([
            {
                "file": "curve25519-dalek/src/edwards.rs",
                "line": 329,
                "column": 13,
                "data": "postcondition not satisfied",
            },
            {
                "file": "curve25519-dalek/src/montgomery.rs",
                "line": "1035",
                "column": "14",
                "data": "assertion failed",
            },
            {"data": "timed out"},
            "raw text",
        ], "stderr tail")
        self.assertEqual(out.splitlines(), [
            "curve25519-dalek/src/edwards.rs:329:13: postcondition not satisfied",
            "curve25519-dalek/src/montgomery.rs:1035:14: assertion failed",
            "timed out",
            "raw text",
            "stderr tail",
        ])

    def test_format_diagnostics_for_memory_deduplicates_rendered_lines(self):
        import run
        out = run._format_diagnostics_for_memory([
            {"file": "a.rs", "line": 1, "column": 2, "data": "failed"},
            {"file": "a.rs", "line": 1, "column": 2, "data": "failed"},
            {"data": "failed"},
        ], "failed")
        self.assertEqual(out.splitlines(), [
            "a.rs:1:2: failed",
            "failed",
        ])

    def test_admit_inventory_groups_by_file_sorted_by_count(self):
        import run
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src" / "lemmas"
            src.mkdir(parents=True)
            a = src / "a.rs"
            b = Path(d) / "src" / "anchor.rs"
            a.write_text(
                "proof fn lemma_p() { admit() }\n"
                "proof fn lemma_q() { admit() }\n")
            b.write_text("pub fn f() {}\n")  # anchor has zero hard admits
            block = run.build_admit_inventory_block(b, [a])
            self.assertIn("Remaining hard admits: 2", block)
            # P3: project-relative path (from src/), not a bare basename
            self.assertIn("`lemmas/a.rs`: 2", block)
            self.assertIn("lemma_p", block)
            self.assertNotIn("anchor.rs", block)  # zero-admit file omitted

    def test_admit_inventory_empty_when_no_hard_admits(self):
        import run
        with tempfile.TemporaryDirectory() as d:
            b = Path(d) / "clean.rs"
            b.write_text("pub fn f() {}\n")
            self.assertEqual(run.build_admit_inventory_block(b, []), "")

    def test_zero_admit_whole_crate_feedback_targets_verus_errors(self):
        import run
        self.assertIn(
            "whole-crate verification errors",
            run._continue_work_tail("field-floor", 0),
        )
        self.assertIn(
            "remaining admits",
            run._continue_work_tail("field-floor", 2),
        )

    def test_zero_admit_whole_crate_plateau_does_not_say_work_admits(self):
        import run
        msg = run._plateau_directive_text(3, 0, "field-floor")
        self.assertIn("whole-crate verification", msg)
        self.assertIn("Top-level API files in the editable list are in scope", msg)
        self.assertNotIn("remaining hard admits", msg)

    def test_zero_admit_whole_crate_plateau_metric_uses_source_span_errors(self):
        import run
        verus_result = {
            "okay": False,
            "messages": [
                {"severity": "error", "file": "edwards.rs", "line": 10},
                {"severity": "note", "data": "hint"},
                {"severity": "error", "file": "scalar.rs", "line": 20},
                {"severity": "error", "data": "verus timed out after 300s and was killed"},
            ],
        }
        self.assertEqual(
            run._plateau_progress_metric("field-floor", 0, verus_result),
            ("whole-crate source-span Verus errors", 2),
        )

    def test_resource_limit_diagnostic_is_not_source_span_progress(self):
        import run
        verus_result = {
            "okay": False,
            "messages": [
                {
                    "severity": "error",
                    "file": "src/scalar.rs",
                    "line": 10,
                    "data": "assert_nonlinear_by: Resource limit (rlimit) exceeded",
                },
            ],
        }

        self.assertEqual(
            run._diagnostic_kind(verus_result["messages"][0]),
            "resource-limit",
        )
        self.assertEqual(
            run._diagnostic_kind_counts(verus_result),
            {"resource-limit": 1},
        )
        self.assertEqual(run._verification_error_count(verus_result), 0)

    def test_complete_gate_blocks_unreplayed_resource_limit_group(self):
        import run
        from lib.results import RoundResult

        rr = RoundResult(
            round_number=1,
            end_reason="COMPLETE",
            returncode=0,
            duration_seconds=1.0,
            verus_okay=True,
            diagnostic_kind_counts={"resource-limit": 1},
        )

        self.assertFalse(run._complete_verus_gate_okay(rr))
        self.assertNotEqual(
            run._final_end_reason(
                run._complete_verus_gate_okay(rr) and 0 == 0,
                rr.end_reason,
            ),
            "COMPLETE",
        )

    def test_harness_whole_crate_command_uses_default_rlimit_first(self):
        import run
        target = Path("/wt/curve25519-dalek/src/ristretto.rs")
        project = Path("/wt/curve25519-dalek")

        cmd = run._harness_verus_command(
            target, project, "field-floor", verus_rlimit=80.0)

        self.assertIn("--whole-crate", cmd)
        self.assertIn("--timeout", cmd)
        self.assertNotIn("--rlimit", cmd)

    def test_harness_module_command_keeps_configured_rlimit(self):
        import run
        target = Path("/wt/curve25519-dalek/src/ristretto.rs")
        project = Path("/wt/curve25519-dalek")

        cmd = run._harness_verus_command(
            target, project, "spec-proof", verus_rlimit=80.0)

        self.assertNotIn("--whole-crate", cmd)
        self.assertIn("--rlimit", cmd)
        self.assertIn("80.0", cmd)

    def test_sibling_verify_skips_whole_crate_modes(self):
        import run

        self.assertFalse(run._should_run_sibling_verify(True, "field-floor"))
        self.assertFalse(run._should_run_sibling_verify(True, "bridge-specs"))
        self.assertFalse(run._should_run_sibling_verify(True, "bridge-full"))
        self.assertTrue(run._should_run_sibling_verify(True, "spec-proof"))
        self.assertFalse(run._should_run_sibling_verify(False, "spec-proof"))

    def test_active_edit_omitted_admit_files_detects_deadlock(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.rs"
            active = root / "active.rs"
            frozen_dep = root / "frozen_dep.rs"
            target.write_text("proof fn target_done() {}\n")
            active.write_text("proof fn active_done() {}\n")
            frozen_dep.write_text("proof fn still_hard() { admit(); }\n")

            omitted = run._active_edit_omitted_admit_files(
                target, [active, frozen_dep], [active])

        self.assertEqual(omitted, [frozen_dep.resolve()])

    def test_active_edit_omitted_admit_files_ignores_axiom_admits(self):
        import run

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.rs"
            active = root / "active.rs"
            axiom_dep = root / "axiom_dep.rs"
            target.write_text("proof fn target_done() {}\n")
            active.write_text("proof fn active_done() {}\n")
            axiom_dep.write_text("proof fn axiom_allowed() { admit(); }\n")

            omitted = run._active_edit_omitted_admit_files(
                target, [active, axiom_dep], [active])

        self.assertEqual(omitted, [])

    def test_verus_rlimit_default_matches_cli_default(self):
        import inspect
        import run

        self.assertEqual(
            inspect.signature(run.run_task).parameters["verus_rlimit"].default,
            run._DEFAULT_VERUS_RLIMIT,
        )
        self.assertEqual(
            inspect.signature(run.render_prompt).parameters["verus_rlimit"].default,
            run._DEFAULT_VERUS_RLIMIT,
        )

    def test_plateau_metric_includes_dep_sweep_errors(self):
        import run
        plateau_result = {
            "okay": False,
            "messages": [
                {"severity": "error", "data": "whole-crate timed out"},
            ],
        }
        dep_result = {
            "okay": False,
            "messages": [
                {"severity": "error", "file": "src/edwards.rs", "line": i + 1}
                for i in range(58)
            ],
        }
        run._merge_plateau_verus_result(plateau_result, dep_result)
        self.assertEqual(
            run._plateau_progress_metric("field-floor", 0, plateau_result),
            ("whole-crate source-span Verus errors", 58),
        )

    def test_timeout_only_whole_crate_plateau_is_indeterminate(self):
        import run
        verus_result = {
            "okay": False,
            "messages": [
                {"severity": "error",
                 "data": "verus timed out after 300s and was killed"},
                {"severity": "error",
                 "data": "could not compile `curve25519-dalek` (lib) due to 1 previous error"},
            ],
        }
        self.assertTrue(
            run._plateau_metric_indeterminate("field-floor", 0, verus_result)
        )

    def test_nonzero_admit_plateau_metric_stays_admit_keyed(self):
        import run
        verus_result = {
            "okay": False,
            "messages": [{"severity": "error"} for _ in range(12)],
        }
        self.assertEqual(
            run._plateau_progress_metric("field-floor", 3, verus_result),
            ("hard admits", 3),
        )
        self.assertEqual(
            run._plateau_progress_metric("single-file", 0, verus_result),
            ("hard admits", 0),
        )

    def test_zero_admit_error_lows_reset_plateau(self):
        import run
        name = None
        best = None
        since = 0
        # resume12-like trajectory: the admit count is flat at 0, but whole-crate
        # errors keep dropping. The old admit-only guard would stop at round 7;
        # the error-aware guard is only two rounds past the last new low.
        for errors in [119, 85, 85, 68, 64, 64, 64]:
            metric_name, metric_value = run._plateau_progress_metric(
                "field-floor",
                0,
                {
                    "okay": False,
                    "messages": [
                        {"severity": "error", "file": "src/edwards.rs", "line": i + 1}
                        for i in range(errors)
                    ],
                },
            )
            name, best, since = run._update_plateau_progress(
                name, best, since, metric_name, metric_value)
        self.assertEqual(name, "whole-crate source-span Verus errors")
        self.assertEqual(best, 64)
        self.assertEqual(since, 2)

    def test_round_history_includes_mechanical_failure_queue(self):
        import json
        import run
        with tempfile.TemporaryDirectory() as d:
            tdir = Path(d)
            for r in ("round_0", "round_1"):
                (tdir / "snapshots" / r).mkdir(parents=True)

            target = tdir / "src" / "ristretto.rs"
            edwards = tdir / "src" / "edwards.rs"
            montgomery = tdir / "src" / "montgomery.rs"
            for p in (target, edwards, montgomery):
                p.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("pub fn anchor() {\n}\n")
            edwards.write_text("proof fn step_1() {\n    assert(false);\n}\n")
            montgomery.write_text(
                "proof fn differential_add_and_double() {\n"
                "    assert(false);\n"
                "}\n"
            )
            for r in ("round_0", "round_1"):
                (tdir / "snapshots" / r / "ristretto.rs").write_text(target.read_text())
                (tdir / "snapshots" / r / "edwards.rs").write_text(edwards.read_text())
                (tdir / "snapshots" / r / "montgomery.rs").write_text(montgomery.read_text())

            (tdir / "round_1.json").write_text(json.dumps({
                "verus_okay": False,
                "verus_errors": [
                    {"severity": "error", "file": "src/edwards.rs", "line": 2,
                     "column": 5, "data": "postcondition not satisfied"},
                    {"severity": "error", "file": "src/montgomery.rs", "line": 2,
                     "column": 5, "data": "precondition not satisfied"},
                    {"severity": "error",
                     "data": "verus timed out after 300s and was killed"},
                    {"severity": "error",
                     "data": "could not find module `step1_lemmas`"},
                    {"severity": "error",
                     "data": "could not compile `curve25519-dalek` (lib) due to 1 previous error"},
                ],
            }))
            block = run.build_round_history_block(
                tdir,
                2,
                target=target,
                filter_target_errors=False,
                work_files=[target, edwards, montgomery],
            )
            self.assertIn("Whole-crate failure queue", block)
            self.assertIn("Gate scope, not target-local scope", block)
            self.assertIn("Editable-scope status", block)
            self.assertIn("Labels: `in_scope_incomplete`", block)
            self.assertIn("editable file allowed by rule 4", block)
            self.assertNotIn("target file, or in a sibling", block)
            self.assertIn("[in_scope_incomplete] `edwards.rs::step_1`", block)
            self.assertIn(
                "[in_scope_incomplete] "
                "`montgomery.rs::differential_add_and_double`",
                block,
            )
            self.assertIn("`edwards.rs`: source_errors=1", block)
            self.assertIn("`montgomery.rs`: source_errors=1", block)
            self.assertIn("Meta/build diagnostics", block)
            self.assertIn("timeout", block)
            self.assertIn("missing-module", block)

    def test_round_history_work_files_excludes_frozen_target_in_whole_crate(self):
        import run
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "src" / "backend" / "serial" / "u64" / "scalar.rs"
            lemma = root / "src" / "lemmas" / "scalar_byte_lemmas" / "bytes_to_scalar_lemmas.rs"
            target.parent.mkdir(parents=True, exist_ok=True)
            lemma.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("proof fn from_bytes_wide() {\n    assert(false);\n}\n")
            lemma.write_text(
                "proof fn lemma_words64_from_bytes_to_nat_step() {\n}\n"
            )

            work_files = run._round_history_work_files(
                "field-floor", [lemma])
            self.assertEqual(work_files, [lemma])

            block = run.build_failure_queue_block(
                [
                    {
                        "severity": "error",
                        "file": str(target),
                        "line": 2,
                        "column": 5,
                        "data": (
                            "precondition for "
                            "lemma_words64_from_bytes_to_nat_step was not satisfied"
                        ),
                    },
                ],
                work_files=work_files,
            )

        self.assertIn("[off_lane_caused_by_this_lane]", block)
        self.assertIn("scalar.rs", block)
        self.assertIn("bytes_to_scalar_lemmas.rs`: source_errors=0", block)
        self.assertNotIn("`scalar.rs`: source_errors=", block)

    def test_failure_queue_prioritizes_editable_and_traced_consumer_errors(self):
        import run
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            scalar = root / "src" / "scalar.rs"
            edwards = root / "src" / "edwards.rs"
            ristretto = root / "src" / "ristretto.rs"
            scalar.parent.mkdir(parents=True, exist_ok=True)
            scalar.write_text(
                "proof fn lemma_r4_bound_from_canonical() {\n"
                "    admit();\n"
                "}\n"
            )
            edwards.write_text("proof fn frozen_consumer() {}\n")
            ristretto.write_text("proof fn unrelated_consumer() {}\n")

            block = run.build_failure_queue_block(
                [
                    {
                        "severity": "error",
                        "file": str(ristretto),
                        "line": 2,
                        "column": 5,
                        "data": "postcondition not satisfied",
                    },
                    {
                        "severity": "error",
                        "file": str(scalar),
                        "line": 2,
                        "column": 5,
                        "data": "assertion failed",
                    },
                    {
                        "severity": "error",
                        "file": str(edwards),
                        "line": 2,
                        "column": 5,
                        "data": (
                            "precondition for "
                            "lemma_r4_bound_from_canonical was not satisfied"
                        ),
                    },
                ],
                work_files=[scalar],
            )

        editable_i = block.index("[in_scope_incomplete] `scalar.rs::")
        traced_i = block.index("[off_lane_caused_by_this_lane] `edwards.rs::")
        other_i = block.index("[off_lane_other] `ristretto.rs::")
        self.assertLess(editable_i, traced_i)
        self.assertLess(traced_i, other_i)
        self.assertIn("`scalar.rs`: source_errors=1, non_axiom_admits=1", block)

    def test_target_noadmit_error_kept_only_when_filter_off(self):
        # P1: a target-file error inside a fn with NO admit() (a stripped API
        # proof) must survive into round_history for whole-crate cuts
        # (filter_target_errors=False) and is dropped by the admit()-keyed
        # filter otherwise.
        import json
        import run
        with tempfile.TemporaryDirectory() as d:
            tdir = Path(d)
            for r in ("round_0", "round_1"):
                (tdir / "snapshots" / r).mkdir(parents=True)
            body = "pub fn decompress()\n  ensures true\n{\n  let x = 1;\n}\n"
            for r in ("round_0", "round_1"):
                (tdir / "snapshots" / r / "ristretto.rs").write_text(body)
            target = tdir / "ristretto.rs"
            target.write_text(body)
            (tdir / "round_1.json").write_text(json.dumps({
                "verus_okay": False,
                "verus_errors": [{"file": "src/ristretto.rs", "line": 4,
                                  "column": 3, "data": "precondition not satisfied"}],
            }))
            off = run.build_round_history_block(
                tdir, 2, target=target, filter_target_errors=False)
            on = run.build_round_history_block(
                tdir, 2, target=target, filter_target_errors=True)
            self.assertIn("precondition not satisfied", off)
            self.assertNotIn("precondition not satisfied", on)


class SealedGitRecovery(unittest.TestCase):
    """On a sealed (orphan-HEAD) worktree, HEAD-relative git commands can't leak
    the original, so the git-recovery gate must NOT flag them — else a diagnostic
    `git diff` discards a whole round of progress (corefloor_006_resume3 r2:
    47→8 admits thrown away). Only explicit non-HEAD references still leak."""

    def _seg(self, cmd, sealed):
        import run
        return run._git_segment_recovers_source(cmd, sealed=sealed)

    def test_sealed_allows_exact_head_and_worktree_reads(self):
        # Exact-HEAD / working-tree / index reads only — the actual resume3 r2
        # commands (diff, checkout/restore from HEAD, show HEAD:).
        for cmd in ["git diff", "git diff HEAD", "git status",
                    "git log --oneline", "git diff --stat",
                    "git show HEAD:src/montgomery.rs",
                    "git diff -- src/montgomery.rs",
                    "git checkout -- src/edwards.rs",
                    "git checkout HEAD -- src/edwards.rs",
                    "git restore src/edwards.rs",
                    "git diff src/main.rs"]:   # path containing 'main' ≠ ref
            with self.subTest(cmd=cmd):
                self.assertFalse(self._seg(cmd, sealed=True), cmd)

    def test_sealed_blocks_non_head_revs_and_raw_reads(self):
        for cmd in ["git show 103b92b9:src/edwards.rs",
                    "git diff 2cb19d28abcd",
                    "git checkout 103b92b9 -- src/edwards.rs",
                    "git diff main", "git show main:src/edwards.rs",
                    "git diff origin/main", "git show refs/heads/x:f",
                    "git diff HEAD~1", "git show HEAD^:src/x.rs",
                    "git log -p", "git log -p --all",
                    "git show HEAD@{1}:src/x.rs",
                    "git cat-file -p HEAD:src/x.rs",
                    "git worktree add /tmp/x 103b92b9",
                    "git archive HEAD", "git reflog",
                    "git fsck --no-reflogs --unreachable --no-progress"]:
                with self.subTest(cmd=cmd):
                    self.assertTrue(self._seg(cmd, sealed=True), cmd)

    def test_reflog_blocked_in_both_modes(self):
        self.assertTrue(self._seg("git reflog", sealed=True))
        self.assertTrue(self._seg("git reflog", sealed=False))

    def test_fsck_blocked_in_both_modes(self):
        self.assertTrue(self._seg("git fsck --unreachable", sealed=True))
        self.assertTrue(self._seg("git fsck --unreachable", sealed=False))

    def test_git_global_options_with_args_do_not_hide_fsck(self):
        for cmd in [
            "git -c color.ui=false fsck --unreachable",
            "git -c core.quotePath=false fsck --unreachable",
            "git --config-env foo=BAR fsck --unreachable",
            "git --exec-path=/tmp/git-core fsck --unreachable",
        ]:
            with self.subTest(cmd=cmd):
                self.assertTrue(self._seg(cmd, sealed=True), cmd)
                self.assertTrue(self._seg(cmd, sealed=False), cmd)

    def test_git_global_options_keep_safe_status_safe(self):
        for cmd in [
            "git -c color.ui=false status",
            "git --no-pager status --short",
            "git --literal-pathspecs diff src/main.rs",
        ]:
            with self.subTest(cmd=cmd):
                self.assertFalse(self._seg(cmd, sealed=True), cmd)

    def test_unreachable_object_parser_flags_source_recovery_oracle(self):
        import run
        out = (
            "notice: HEAD points to an unborn branch\n"
            "unreachable tree 1111111111111111111111111111111111111111\n"
            "unreachable commit d425909458093b077eea9cc347c30c10cb869cab\n"
        )
        self.assertEqual(
            run._git_unreachable_object_lines(out),
            [
                "unreachable tree 1111111111111111111111111111111111111111",
                "unreachable commit d425909458093b077eea9cc347c30c10cb869cab",
            ],
        )

    def test_sealed_git_object_audit_reports_unreachable_wip_commit(self):
        import run
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            src = repo / "curve25519-dalek" / "src"
            src.mkdir(parents=True)
            proof = src / "edwards.rs"
            proof.write_text("proof fn proven() {}\n")
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            commit_cmd = [
                "git", "-C", str(repo), "-c", "user.name=test",
                "-c", "user.email=test@example.com", "commit", "-q",
            ]
            subprocess.run(commit_cmd + ["-m", "baseline"], check=True)
            base_branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            proof.write_text("proof fn proven() { assert(true); }\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(commit_cmd + ["-m", "WIP on master"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "checkout", "--orphan", "sealed"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "-C", str(repo), "rm", "-qrf", "."], check=True)
            proof.parent.mkdir(parents=True, exist_ok=True)
            proof.write_text("proof fn proven() { admit(); }\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                commit_cmd + ["-m", "peeled init state (history sealed)"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", base_branch],
                check=True,
                capture_output=True,
                text=True,
            )

            leaks, err = run._sealed_git_object_leaks(repo)

        self.assertIsNone(err)
        self.assertTrue(any("unreachable commit" in leak for leak in leaks), leaks)

    def test_alternate_repo_reads_blocked_in_both_modes(self):
        # -C / --git-dir / --work-tree point git at ANOTHER checkout (e.g. the
        # unsealed source repo), so the seal on THIS worktree is irrelevant.
        for cmd in [
            "git -C /private/tmp/dalek-baf show 103b92b9:src/edwards.rs",
            "git -C /private/tmp/dalek-spec-strip show HEAD:src/edwards.rs",
            "git --git-dir=/src/.git show HEAD:src/edwards.rs",
            "git --work-tree=/x --git-dir=/y show HEAD:f",
            "git -C ../other diff",
        ]:
            with self.subTest(cmd=cmd):
                self.assertTrue(self._seg(cmd, sealed=True), cmd)
                self.assertTrue(self._seg(cmd, sealed=False), cmd)

    def test_unsealed_unchanged(self):
        # Without the seal, HEAD-relative diff/show DO leak (HEAD = the answer).
        self.assertTrue(self._seg("git diff", sealed=False))
        self.assertTrue(self._seg("git show HEAD:src/edwards.rs", sealed=False))
        self.assertFalse(self._seg("git status", sealed=False))

    def test_the_exact_resume3_command_is_now_safe(self):
        # The literal segment that discarded resume3's 47->8 round.
        cmd = 'git diff src/lemmas/scalar_lemmas_/montgomery_reduce_lemmas.rs'
        self.assertTrue(self._seg(cmd, sealed=False))   # old behavior: flagged
        self.assertFalse(self._seg(cmd, sealed=True))   # sealed: allowed


class RetryMemoryTaint(unittest.TestCase):
    def test_trace_tainted_runs_do_not_seed_retry_memory(self):
        import run
        for reason in ("GIT_RECOVERY", "git_recovery", "USER_INTERRUPTED",
                       "user_interrupted", "INTERRUPTED_SIGNAL",
                       "interrupted_signal", "PROCESS_CROSSTALK",
                       "process_crosstalk"):
            with self.subTest(reason=reason):
                self.assertFalse(run._should_persist_retry_memory(reason))
        for reason in ("LIMIT", "TRANSPORT_ERROR", "RETRY_EXHAUSTED",
                       "FALSE_CONTRACT", "NEEDS_DECOMP", "SPEC_DRIFT",
                       "AXIOM_DRIFT", "TOOLING_DRIFT", "FROZEN_EDIT",
                       "FORBIDDEN_CONSTRUCT",
                       "SIBLING_VERUS_FAIL"):
            with self.subTest(reason=reason):
                self.assertTrue(run._should_persist_retry_memory(reason))


class ProcessCrosstalkDetection(unittest.TestCase):
    def _raw(self, td: str, blocks: list[dict]) -> Path:
        raw = Path(td) / "round_1.jsonl"
        with raw.open("w") as f:
            for block in blocks:
                f.write(json.dumps({
                    "type": "assistant",
                    "message": {"content": [block]},
                }) + "\n")
        return raw

    def test_detects_broad_process_control(self):
        import run
        with tempfile.TemporaryDirectory() as td:
            raw = self._raw(td, [{
                "type": "tool_use",
                "name": "Bash",
                "input": {
                    "command": (
                        "pkill -f \"cargo-verus\"; "
                        "pgrep -f verus_check.py || true")
                },
            }])

            hits = run.detect_process_crosstalk(raw)

            self.assertEqual(len(hits), 1)
            self.assertIn("broad process control", hits[0])

    def test_detects_background_verifier_and_shared_tmp_output(self):
        import run
        with tempfile.TemporaryDirectory() as td:
            raw = self._raw(td, [{
                "type": "tool_use",
                "name": "Bash",
                "input": {
                    "command": (
                        "python3 /repo/skills/verus_check.py t.rs --project p "
                        "> /tmp/vcheck.json"),
                    "run_in_background": True,
                },
            }])

            hits = run.detect_process_crosstalk(raw)

            self.assertEqual(len(hits), 1)
            self.assertIn("background verifier", hits[0])
            self.assertIn("shared /tmp verifier output", hits[0])

    def test_detects_shell_timeout_wrapped_verifier(self):
        import run
        with tempfile.TemporaryDirectory() as td:
            raw = self._raw(td, [{
                "type": "tool_use",
                "name": "Bash",
                "input": {
                    "command": (
                        "cd /project && timeout 590 python "
                        "/repo/skills/verus_check.py src/ristretto.rs "
                        "--project /project")
                },
            }])

            hits = run.detect_process_crosstalk(raw)

            self.assertEqual(len(hits), 1)
            self.assertIn("shell timeout verifier wrapper", hits[0])

    def test_allows_timeout_wrapped_verus_check_help(self):
        import run
        with tempfile.TemporaryDirectory() as td:
            raw = self._raw(td, [{
                "type": "tool_use",
                "name": "Bash",
                "input": {
                    "command": (
                        "cd /work/curve25519-dalek && timeout 30 python3 "
                        "/opt/harness/skills/verus_check.py --help "
                        "2>/dev/null; echo \"exit: $?\"")
                },
            }])

            self.assertEqual(run.detect_process_crosstalk(raw), [])

    def test_allows_foreground_skill_and_project_tmp_paths(self):
        import run
        with tempfile.TemporaryDirectory() as td:
            raw = self._raw(td, [{
                "type": "tool_use",
                "name": "Bash",
                "input": {
                    "command": (
                        "python3 /repo/skills/verus_check.py "
                        "/tmp/dalek-admits-layerA/curve25519-dalek/src/x.rs "
                        "--project /tmp/dalek-admits-layerA/curve25519-dalek"
                    )
                },
            }])

            self.assertEqual(run.detect_process_crosstalk(raw), [])

    def _raw_lines(self, td: str, lines: list[dict]) -> Path:
        """Write arbitrary stream events (assistant + user) to a round jsonl."""
        raw = Path(td) / "round_1.jsonl"
        with raw.open("w") as f:
            for ev in lines:
                f.write(json.dumps(ev) + "\n")
        return raw

    def test_ignores_hook_blocked_tool_use(self):
        # A forbidden Bash tool_use that the PreToolUse verifier-policy hook
        # BLOCKED (exit 2) is present in the stream as an attempted tool_use,
        # followed by an error tool_result carrying the hook block marker. The
        # command never executed, so it must NOT be a terminal crosstalk hit.
        import run
        forbidden = {
            "command": ("python3 /repo/skills/verus_check.py t.rs --project p "
                        "> /tmp/vcheck.json"),
            "run_in_background": True,
        }
        with tempfile.TemporaryDirectory() as td:
            raw = self._raw_lines(td, [
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Bash",
                     "input": forbidden},
                ]}},
                {"type": "user", "message": {"content": [
                    # Exact runtime wrapper shape (verified against the smoke
                    # stream): "PreToolUse:Bash hook error: [<cmd>]: <stderr>".
                    {"type": "tool_result", "tool_use_id": "tu_1",
                     "is_error": True,
                     "content": ("PreToolUse:Bash hook error: [python3 "
                                 "/repo/skills/verus_check.py t.rs]: BLOCKED by "
                                 "verifier policy (background verifier, shared "
                                 "/tmp verifier output). Per prompt.md: run ...")},
                ]}},
            ])
            self.assertEqual(run.detect_process_crosstalk(raw), [])

    def test_bare_block_phrase_without_hook_wrapper_still_flags(self):
        # Hardening (codex 20:29): an executed/bypassed command could echo our
        # own stderr phrase "BLOCKED by verifier policy" into its tool_result to
        # hide from the audit. Suppression requires the runtime PreToolUse hook
        # wrapper; the bare phrase alone must NOT suppress the crosstalk hit.
        import run
        forbidden = {
            "command": ("python3 /repo/skills/verus_check.py t.rs --project p "
                        "> /tmp/vcheck.json"),
            "run_in_background": True,
        }
        with tempfile.TemporaryDirectory() as td:
            raw = self._raw_lines(td, [
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Bash",
                     "input": forbidden},
                ]}},
                {"type": "user", "message": {"content": [
                    # No "PreToolUse:Bash hook error" wrapper -> not hook-blocked.
                    {"type": "tool_result", "tool_use_id": "tu_1",
                     "is_error": True,
                     "content": "BLOCKED by verifier policy (background verifier)"},
                ]}},
            ])
            hits = run.detect_process_crosstalk(raw)
            self.assertEqual(len(hits), 1)
            self.assertIn("background verifier", hits[0])

    def test_still_flags_executed_command_with_no_hook_block(self):
        # The SAME forbidden command that actually executed (no hook-block
        # tool_result, e.g. the hook was absent or bypassed) stays a terminal
        # crosstalk hit — the audit backstop is preserved.
        import run
        forbidden = {
            "command": ("python3 /repo/skills/verus_check.py t.rs --project p "
                        "> /tmp/vcheck.json"),
            "run_in_background": True,
        }
        with tempfile.TemporaryDirectory() as td:
            raw = self._raw_lines(td, [
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Bash",
                     "input": forbidden},
                ]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "tu_1",
                     "content": "ok\n"},
                ]}},
            ])
            hits = run.detect_process_crosstalk(raw)
            self.assertEqual(len(hits), 1)
            self.assertIn("background verifier", hits[0])

    def test_final_gate_and_retry_memory_preserve_process_crosstalk(self):
        import run
        self.assertEqual(
            run._final_end_reason(True, "PROCESS_CROSSTALK"),
            "PROCESS_CROSSTALK",
        )
        self.assertFalse(run._should_persist_retry_memory("process_crosstalk"))


class TaskResultFinalTelemetry(unittest.TestCase):
    def test_final_error_count_ignores_warning_diagnostics(self):
        import run

        self.assertEqual(
            run._stored_verus_error_count([
                {"severity": "warning", "data": "unexpected cfg"},
                {"severity": "note", "data": "instantiation hint"},
                {"severity": "error", "data": "postcondition not satisfied"},
                {"data": "legacy untyped diagnostic"},
            ]),
            2,
        )

    def test_verus_error_count_prefers_raw_checker_count(self):
        import run

        self.assertEqual(
            run._verus_error_count({
                "okay": False,
                "error_count": 369,
                "messages": [
                    {"severity": "error", "data": "sample 1"},
                    {"severity": "error", "data": "sample 2"},
                ],
            }),
            369,
        )

    def test_build_wrapper_does_not_mask_source_span_proof_failures(self):
        import run

        verus_result = {
            "okay": False,
            "messages": [
                {
                    "severity": "error",
                    "file": "curve25519-dalek/src/ristretto.rs",
                    "line": 653,
                    "column": 9,
                    "data": "assertion failed",
                },
                {
                    "severity": "error",
                    "file": "",
                    "line": 0,
                    "column": 0,
                    "data": (
                        "could not compile `curve25519-dalek` (lib) "
                        "due to 1 previous error"
                    ),
                },
            ],
        }

        self.assertEqual(run._diagnostic_kind_counts(verus_result)["build-wrapper"], 1)
        self.assertEqual(run._verification_error_count(verus_result), 1)
        self.assertFalse(run._compile_blocked_or_indeterminate(verus_result))

    def test_resolve_error_is_compile_blocker_not_proof_obligation(self):
        import run

        verus_result = {
            "okay": False,
            "messages": [
                {
                    "severity": "error",
                    "file": "curve25519-dalek/src/backend/serial/u64/field.rs",
                    "line": 171,
                    "column": 13,
                    "data": "cannot find function `lemma_mul_le_mul` in this scope",
                },
                {
                    "severity": "error",
                    "file": "",
                    "line": 0,
                    "column": 0,
                    "data": (
                        "could not compile `curve25519-dalek` (lib) "
                        "due to 1 previous error"
                    ),
                },
            ],
        }

        self.assertEqual(run._diagnostic_kind_counts(verus_result)["compile"], 1)
        self.assertEqual(run._verification_error_count(verus_result), 0)
        self.assertTrue(run._compile_blocked_or_indeterminate(verus_result))

    def test_pure_verus_panic_is_indeterminate_not_proof_obligation(self):
        import run

        verus_result = {
            "okay": False,
            "messages": [
                {
                    "severity": "error",
                    "file": "curve25519-dalek/src/backend/serial/u64/field.rs",
                    "line": 738,
                    "column": 17,
                    "data": (
                        "Verus internal panic while interpreting this "
                        "expression; reduce the expression or isolate the "
                        "proof step"
                    ),
                },
            ],
        }

        self.assertEqual(run._diagnostic_kind_counts(verus_result)["panic"], 1)
        self.assertEqual(run._verification_error_count(verus_result), 0)
        self.assertTrue(run._compile_blocked_or_indeterminate(verus_result))

    def test_verus_panic_does_not_mask_real_proof_obligations(self):
        import run

        verus_result = {
            "okay": False,
            "messages": [
                {
                    "severity": "error",
                    "file": "curve25519-dalek/src/backend/serial/u64/field.rs",
                    "line": 738,
                    "column": 17,
                    "data": (
                        "Verus internal panic while interpreting this "
                        "expression"
                    ),
                },
                {
                    "severity": "error",
                    "file": "curve25519-dalek/src/ristretto.rs",
                    "line": 653,
                    "column": 9,
                    "data": "assertion failed",
                },
            ],
        }

        self.assertEqual(run._diagnostic_kind_counts(verus_result)["panic"], 1)
        self.assertEqual(run._verification_error_count(verus_result), 1)
        self.assertFalse(run._compile_blocked_or_indeterminate(verus_result))

    def test_final_gate_fields_are_top_level_json(self):
        from lib.results import RoundResult, TaskResult, write_json

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "result.json"
            result = TaskResult(
                task_id="ristretto",
                run_id="run",
                target_path="/tmp/ristretto.rs",
                module_path="ristretto",
                success=False,
                end_reason="LIMIT",
                rounds_used=1,
                duration_seconds=3.0,
                round_results=[
                    RoundResult(
                        round_number=1,
                        end_reason="LIMIT",
                        returncode=0,
                        duration_seconds=3.0,
                        verus_okay=False,
                        verus_errors=[{"data": "postcondition not satisfied"}],
                    )
                ],
                final_verus_okay=False,
                final_admits_remaining=4,
                final_hard_admits_remaining=3,
                final_intentional_axiom_admits=1,
                final_error_count=1,
                final_spec_drift_count=0,
                experiment_provenance="lane-isolation/operator-stubbed",
            )

            write_json(out, result)
            data = json.loads(out.read_text())

            self.assertEqual(data["final_verus_okay"], False)
            self.assertEqual(data["final_admits_remaining"], 4)
            self.assertEqual(data["final_hard_admits_remaining"], 3)
            self.assertEqual(data["final_intentional_axiom_admits"], 1)
            self.assertEqual(data["final_error_count"], 1)
            self.assertEqual(data["final_spec_drift_count"], 0)
            self.assertEqual(
                data["experiment_provenance"],
                "lane-isolation/operator-stubbed",
            )


class AgentBashEnvTests(unittest.TestCase):
    """A1: Bash tool commands start in the Cargo project root while the parent
    Claude process remains free to use the scratch cwd that avoids CLAUDE.md
    auto-discovery."""

    def test_noninteractive_bash_starts_in_project_root(self):
        import run
        import shutil as _shutil

        bash = _shutil.which("bash")
        if not bash:
            self.skipTest("bash is required to exercise BASH_ENV")

        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            project = base / "project root"
            scratch = base / "scratch"
            out_dir = base / "task"
            project.mkdir()
            scratch.mkdir()

            bash_env = run._write_agent_bash_env(out_dir, project)
            # Pin overwrite behavior too: result dirs can be reused on reruns.
            self.assertEqual(run._write_agent_bash_env(out_dir, project), bash_env)

            env = {
                "BASH_ENV": str(bash_env),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            proc = subprocess.run(
                [bash, "-c", "pwd"],
                cwd=str(scratch),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(Path(proc.stdout.strip()).resolve(), project.resolve())
            self.assertEqual(Path.cwd(), old_cwd)


class RunTaskShutilScope(unittest.TestCase):
    """`run_task` calls `shutil.copy2` in rollback paths (GIT_RECOVERY discard,
    budget-bail). A function-local `import shutil` anywhere in run_task rebinds
    `shutil` as a LOCAL for the whole function, so an earlier `shutil.copy2`
    raises UnboundLocalError — a crash that only fires on a real mid-run git
    peek, escaping pre-launch smoke tests. Pin it: `shutil` must resolve to the
    module global, never a run_task local (a `import shutil as _shutil` alias
    under a different name is fine)."""

    def test_shutil_is_not_a_runtask_local(self):
        import run
        self.assertNotIn(
            "shutil", run.run_task.__code__.co_varnames,
            "run_task binds `shutil` as a local — a bare `import shutil` inside "
            "run_task shadows the module global, so shutil.copy2 in the "
            "git-recovery / budget rollback raises UnboundLocalError.")


if __name__ == "__main__":
    unittest.main()
