"""Unit tests for the PreToolUse verifier-policy hook matcher.

Covers the exact gcp13 + e2e1 Bash shapes that ignored prompt.md's verifier
rules and tripped the post-round PROCESS_CROSSTALK gate, plus the legitimate
foreground `verus_check.py` shapes that must stay allowed. The hook
(`lib/verifier_policy_hook.py`) blocks the former at the tool call so the round
never burns budget up to the crosstalk gate.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "lib"))
import verifier_policy_hook  # noqa: E402
from verifier_policy_hook import evaluate  # noqa: E402


class VerifierPolicyHookMatcher(unittest.TestCase):
    # (label, tool_name, tool_input, expect_block)
    TABLE = [
        # --- real gcp13 shapes (raw round_1.jsonl) ---
        ("gcp13 timeout-wrapped verus_check", "Bash",
         {"command": "timeout 300 python3 /h/skills/verus_check.py "
                     "src/lemmas/scalar_lemmas_/montgomery_reduce_part1_chain_lemmas.rs "
                     "--project /p"}, True),
        ("gcp13 timeout + bg + /tmp", "Bash",
         {"command": "timeout 400 python3 /h/skills/verus_check.py x --project /p "
                     "> /tmp/p1c.json 2>&1", "run_in_background": True}, True),
        ("gcp13 bg whole-crate -> /tmp/whole_crate.json", "Bash",
         {"command": "python3 verus_check.py --whole-crate > /tmp/whole_crate.json",
          "run_in_background": True}, True),
        # --- real e2e1 shape ---
        ("e2e1 bg cargo verus | grep | head", "Bash",
         {"command": "cd /work/curve25519-dalek && cargo verus verify 2>&1 "
                     "| grep -E '^(error|warning|note)' | head -20",
          "run_in_background": True}, True),
        # --- other forbidden classes ---
        ("broad pkill verifier", "Bash", {"command": "pkill -9 -f rust_verify"}, True),
        ("pgrep -f", "Bash", {"command": "pgrep -f run.py"}, True),
        ("raw cargo-verus | grep (no bg)", "Bash",
         {"command": "cargo verus verify -p curve25519-dalek | grep error"}, True),
        # bare raw cargo-verus (no pipe) — prompt.md:78-81 forbids it outright
        ("bare cargo verus verify -p", "Bash",
         {"command": "cargo verus verify -p curve25519-dalek"}, True),
        ("bare cargo-verus verify -p", "Bash",
         {"command": "cargo-verus verify -p curve25519-dalek"}, True),
        ("bare cargo verus after cd", "Bash",
         {"command": "cd /work/curve25519-dalek && cargo verus verify "
                     "--verify-module ristretto"}, True),
        ("direct verus with tail", "Bash",
         {"command": "cd /work/curve25519-dalek && timeout 180 verus src/lib.rs "
                     "--crate-type=lib --verify-module lemmas::edwards_lemmas::constants_lemmas "
                     "2>&1 | tail -25"}, True),
        ("direct verus no pipe", "Bash",
         {"command": "cd /work/curve25519-dalek && verus src/lib.rs --crate-type=lib"},
         True),
        ("verus_check tail slice", "Bash",
         {"command": "python3 /opt/harness/skills/verus_check.py "
                     "/work/curve25519-dalek/src/ristretto.rs --project /work/curve25519-dalek "
                     "2>&1 | tail -80"}, True),
        ("verus_check grep slice", "Bash",
         {"command": "python3 /opt/harness/skills/verus_check.py x --project /p "
                     "| grep 'error_count'"}, True),
        ("verifier output to /tmp/foo.log", "Bash",
         {"command": "python3 verus_check.py x 2> /tmp/foo.log"}, True),
        ("run007 merged-stderr json parser", "Bash",
         {"command": "python3 /opt/harness/skills/verus_check.py "
                     "/work/curve25519-dalek/src/ristretto.rs "
                     "--project /work/curve25519-dalek --timeout 120 2>&1 "
                     "| python3 -c \"import sys,json; d=json.load(sys.stdin); "
                     "print(d.get('summary',''))\""}, True),
        ("pipe ampersand json parser", "Bash",
         {"command": "python3 /opt/harness/skills/verus_check.py x |& "
                     "python3 -c 'import json,sys; json.load(sys.stdin)'"}, True),
        ("admit_inventory merged-stderr json parser", "Bash",
         {"command": "python3 /opt/harness/skills/admit_inventory.py "
                     "/work/curve25519-dalek/src/ristretto.rs 2>&1 "
                     "| python3 -c \"import sys,json; json.load(sys.stdin)\""}, True),
        ("raw grep admit wc count", "Bash",
         {"command": "grep -r \"admit()\" /work/curve25519-dalek/src/lemmas/ "
                     "--include=\"*.rs\" | wc -l"}, True),
        ("raw rg admit count", "Bash",
         {"command": "rg -c 'admit\\(\\)' /work/curve25519-dalek/src -g '*.rs'"},
         True),
        # --- stage3 A6 r16 loophole: stdout redirect to NON-/tmp file ---
        ("a6r16 whole-crate stdout -> /work/wc.json", "Bash",
         {"command": "python3 /opt/harness/skills/verus_check.py "
                     "/work/curve25519-dalek/src/ristretto.rs "
                     "--project /work/curve25519-dalek --whole-crate > /work/wc.json"},
         True),
        ("verifier stdout append -> /work log", "Bash",
         {"command": "python3 verus_check.py x --project /p >> /work/wc.log"}, True),
        ("verifier both streams -> file (&>)", "Bash",
         {"command": "python3 verus_check.py x --project /p &> /work/all.txt"}, True),
        ("spec_check stdout -> cwd file", "Bash",
         {"command": "python3 /opt/harness/skills/spec_check.py verify x "
                     "--against snap.json > out.json"}, True),
        # explicit fd-1 stdout forms (codex 02:37 objection: 1> bypassed bare->)
        ("verifier explicit 1> no space", "Bash",
         {"command": "python3 verus_check.py x --project /p 1>/work/wc.json"}, True),
        ("verifier explicit 1> with space", "Bash",
         {"command": "python3 verus_check.py x --project /p 1> /work/wc.json"}, True),
        ("verifier explicit 1>> append", "Bash",
         {"command": "python3 verus_check.py x --project /p 1>> /work/wc.log"}, True),
        # --- legitimate, must be ALLOWED ---
        ("verifier stdout -> /dev/null", "Bash",
         {"command": "python3 verus_check.py x --project /p > /dev/null"}, False),
        ("verifier stderr-only -> /work file", "Bash",
         {"command": "python3 verus_check.py x --project /p 2> /work/err.log"}, False),
        ("verifier stderr append -> /work file", "Bash",
         {"command": "python3 verus_check.py x --project /p 2>>/work/err.log"}, False),
        ("verifier noclobber stdout redirect", "Bash",
         {"command": "python3 verus_check.py x --project /p >| /work/out.json"}, True),
        ("verifier explicit-fd noclobber redirect", "Bash",
         {"command": "python3 verus_check.py x --project /p 1>| /work/out.json"}, True),
        ("verifier merged redirect shorthand", "Bash",
         {"command": "python3 verus_check.py x --project /p >& /work/out.json"}, True),
        ("hash inside token cannot hide pipeline", "Bash",
         {"command": "python3 verus_check.py foo#bar --project /p | head -1"}, True),
        ("real shell comment hides non-executed pipeline", "Bash",
         {"command": "python3 verus_check.py x --project /p # | head -1"}, False),
        ("quoted hash is query data", "Bash",
         {"command": "python3 verus_check.py 'foo # bar' --project /p"}, False),
        ("semantic search quoted Rust arrow", "Bash",
         {"command": "python3 /opt/harness/skills/search_semantic.py "
                     "\"u64 -> nat\" --project /p"}, False),
        ("semantic search quoted alternation", "Bash",
         {"command": "python3 /opt/harness/skills/search_semantic.py "
                     "\"pow2 | bitvector\" --project /p"}, False),
        ("quoted command substitution cannot hide verifier pipeline", "Bash",
         {"command": "printf '%s\\n' \"$(python3 /opt/harness/skills/"
                     "verus_check.py x --project /p | head -1)\""}, True),
        ("non-verifier redirect untouched", "Bash",
         {"command": "echo hi > /work/x.json"}, False),
        ("fg verus_check with --timeout", "Bash",
         {"command": "python3 skills/verus_check.py ristretto --project /p --timeout 400"}, False),
        ("fg verus_check module", "Bash",
         {"command": "python3 skills/verus_check.py src/x.rs --project /p --module x"}, False),
        ("fg verus_check stdout-only json parser", "Bash",
         {"command": "python3 /opt/harness/skills/verus_check.py x --project /p "
          "| python3 -c 'import json,sys; json.load(sys.stdin)'"},
         True),
        ("fg verus_check help slice", "Bash",
         {"command": "python3 /opt/harness/skills/verus_check.py --help 2>&1 | head -40"},
         False),
        ("read harness /tmp/claude- scratch", "Bash",
         {"command": "cat /tmp/claude-501/foo.json"}, False),
        ("grep source tree", "Bash", {"command": "grep -rn lemma_foo src/"}, False),
        ("grep source in /opt/verus with head", "Bash",
         {"command": "grep -rn \"pub.*fn use_type_invariant\\|use_type_invariant.*=\" "
                     "/opt/verus -r --include=\"*.rs\" | head -5"},
         False),
        ("rg admit line search", "Bash",
         {"command": "rg -n 'admit\\(\\)' /work/curve25519-dalek/src -g '*.rs'"},
         False),
        ("admit inventory", "Bash",
         {"command": "python3 /opt/harness/skills/admit_inventory.py "
                     "/work/curve25519-dalek/src/ristretto.rs --siblings "
                     "/work/curve25519-dalek/src/scalar.rs"}, False),
        ("admit inventory stdout-only json parser", "Bash",
         {"command": "python3 /opt/harness/skills/admit_inventory.py "
                     "/work/curve25519-dalek/src/ristretto.rs "
                     "| python3 -c \"import sys,json; json.load(sys.stdin)\""},
         False),
        ("non-Bash tool ignored", "Read", {"file_path": "/p/src/x.rs"}, False),
        ("plain timeout, no verifier", "Bash", {"command": "timeout 5 sleep 3"}, False),
        ("empty command", "Bash", {"command": ""}, False),
        # --- output-shape rules are segment-scoped (F4): a pipe/redirect in
        # an unrelated control segment of a compound command is plain shell ---
        ("pipe in unrelated segment before verifier", "Bash",
         {"command": "grep -n lemma src/x.rs | head -5; "
                     "python3 /opt/harness/skills/verus_check.py f --project /p"}, False),
        ("redirect in unrelated segment before verifier", "Bash",
         {"command": "echo note > /work/notes.txt && "
                     "python3 /opt/harness/skills/verus_check.py f --project /p"}, False),
        ("verifier segment piped stays blocked in compound", "Bash",
         {"command": "echo start; python3 /opt/harness/skills/verus_check.py f "
                     "--project /p | head -3"}, True),
    ]

    def test_table(self):
        for label, tool, inp, expect_block in self.TABLE:
            with self.subTest(label=label):
                reasons = evaluate(tool, inp)
                self.assertEqual(bool(reasons), expect_block,
                                 f"{label}: reasons={reasons}")

    def test_corrective_message_uses_absolute_skill_path(self):
        # prompt.md:66-70 requires absolute skill paths; the hand-back message
        # must name a real absolute <harness>/skills/verus_check.py, not a
        # relative `skills/verus_check.py`.
        path = verifier_policy_hook._VERUS_CHECK
        self.assertTrue(os.path.isabs(path), f"not absolute: {path}")
        self.assertTrue(path.endswith(os.path.join("skills", "verus_check.py")), path)
        rendered = verifier_policy_hook._MSG.format(
            reasons="raw cargo-verus substitution (use verus_check.py)",
            verus_check=path)
        self.assertIn(path, rendered)
        self.assertNotIn("`python3 skills/verus_check.py", rendered)

    def test_pre_edit_guard_blocks_diagnostics_until_active_file_diff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src.rs"
            src.write_text("proof fn lemma_x() {}\n")
            subprocess.run(["git", "init"], cwd=root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "add", "src.rs"], cwd=root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["git", "-c", "user.email=t@example.com", "-c", "user.name=T",
                 "commit", "-m", "init"],
                cwd=root, check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            env = {
                "DALEK_PRE_EDIT_DIAGNOSTIC_BLOCK": "1",
                "DALEK_AGENT_PROJECT_ROOT": str(root),
                "DALEK_AGENT_TARGET_PATH": str(src),
                "DALEK_AGENT_ACTIVE_EDIT_PATHS": str(src),
            }
            verus_cmd = {
                "command": f"python3 /opt/harness/skills/verus_check.py {src} --project {root}"
            }
            search_cmd = {
                "command": "python3 /opt/harness/skills/search_semantic.py 'mul ladder'"
            }

            with mock.patch.dict(os.environ, env, clear=False):
                reasons = evaluate("Bash", verus_cmd)
                self.assertIn("pre-edit proof-thread diagnostic before active source diff", reasons)
                self.assertIn(
                    "pre-edit proof-thread diagnostic before active source diff",
                    evaluate("Bash", search_cmd),
                )
                self.assertEqual(evaluate("Bash", {"command": "rg -n lemma src.rs"}), [])

                src.write_text("proof fn lemma_x() { assert(true); }\n")
                self.assertEqual(evaluate("Bash", verus_cmd), [])

                subprocess.run(
                    ["git", "add", "src.rs"], cwd=root, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self.assertEqual(
                    evaluate("Bash", verus_cmd), [],
                    "a staged-only active edit must satisfy the guard",
                )

            subprocess.run(
                ["git", "reset", "--hard", "HEAD"], cwd=root, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            new_src = root / "new_lemma.rs"
            new_src.write_text("proof fn lemma_new() {}\n")
            new_env = {
                **env,
                "DALEK_AGENT_TARGET_PATH": str(new_src),
                "DALEK_AGENT_ACTIVE_EDIT_PATHS": str(new_src),
            }
            new_cmd = {
                "command": f"python3 /opt/harness/skills/verus_check.py "
                           f"{new_src} --project {root}",
            }
            with mock.patch.dict(os.environ, new_env, clear=False):
                self.assertEqual(
                    evaluate("Bash", new_cmd), [],
                    "an untracked active source edit must satisfy the guard",
                )

    def test_runner_owned_whole_crate_and_timeout_cap_are_enforced(self):
        env = {
            "DALEK_RUNNER_OWNS_WHOLE_CRATE": "1",
            "DALEK_AGENT_MAX_VERIFIER_TIMEOUT": "300",
        }
        def cap_tripped(reasons):
            return any(
                r.startswith("agent verifier timeout exceeds runner policy cap")
                for r in reasons
            )

        with mock.patch.dict(os.environ, env, clear=False):
            whole = evaluate("Bash", {
                "command": "python3 /opt/harness/skills/verus_check.py x --project /p --whole-crate"
            })
            slow = evaluate("Bash", {
                "command": "python3 /opt/harness/skills/verus_check.py x --project /p --timeout 301"
            })
            focused = evaluate("Bash", {
                "command": "python3 /opt/harness/skills/verus_check.py x --project /p --timeout 300"
            })
            # Both argparse spellings must respect the cap: `--timeout=N` is
            # equivalent to `--timeout N` to verus_check.py's parser (F6).
            equals_slow = evaluate("Bash", {
                "command": "python3 /opt/harness/skills/verus_check.py x --project /p --timeout=1800"
            })
            equals_ok = evaluate("Bash", {
                "command": "python3 /opt/harness/skills/verus_check.py x --project /p --timeout=300"
            })
            # A non-literal value cannot be checked statically — fail closed.
            variable = evaluate("Bash", {
                "command": "python3 /opt/harness/skills/verus_check.py x --project /p --timeout $N"
            })
        self.assertIn("whole-crate verifier is runner-owned for this experiment", whole)
        self.assertTrue(cap_tripped(slow), slow)
        self.assertEqual(focused, [])
        self.assertTrue(cap_tripped(equals_slow), equals_slow)
        self.assertEqual(equals_ok, [])
        self.assertTrue(cap_tripped(variable), variable)


if __name__ == "__main__":
    unittest.main()
