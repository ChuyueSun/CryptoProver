"""spec_check._extract_sigs must ignore `fn NAME` text inside comments and
string literals, and require `fn` to be a standalone token.

Regression for the false-positive SPEC_DRIFT that killed peel_corefloor_005: a
comment `// ...a recursive defn equals a large polynomial` registered a phantom
`fn equals`; the instant the agent edited that comment (scalar.rs is editable in
the field-floor cut) the phantom vanished and the spec-integrity gate reported
drift, terminating a 40-round marathon on round 1.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_SC_PATH = Path(__file__).resolve().parents[1] / "skills" / "spec_check.py"
_spec = importlib.util.spec_from_file_location("spec_check", _SC_PATH)
spec_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spec_check)


class ExtractSigsIgnoresCommentsAndStrings(unittest.TestCase):
    def test_defn_in_comment_is_not_a_phantom_fn(self):
        # The exact shape that broke peel_corefloor_005.
        sigs = spec_check._extract_sigs(
            "// which shows that a recursive defn equals a large polynomial\n"
            "pub open spec fn real_one(x: int) -> bool { x > 0 }\n"
        )
        self.assertNotIn("equals", sigs)
        self.assertIn("real_one", sigs)

    def test_fn_word_in_prose_comment_is_not_a_phantom(self):
        sigs = spec_check._extract_sigs(
            "// call fn helper before the loop\n"
            "proof fn lemma_real() ensures true {}\n"
        )
        self.assertNotIn("helper", sigs)
        self.assertIn("lemma_real", sigs)

    def test_fn_in_string_literal_is_not_a_phantom(self):
        sigs = spec_check._extract_sigs(
            'fn real() { let _ = "a fn fake here"; }\n'
        )
        self.assertNotIn("fake", sigs)
        self.assertIn("real", sigs)

    def test_identifier_ending_in_fn_is_not_a_phantom(self):
        # `\bfn` must not match the `fn` inside an identifier like `myfn`.
        sigs = spec_check._extract_sigs("let myfn = compute(x);\n")
        self.assertEqual(sigs, {})

    def test_real_signatures_still_extracted(self):
        sigs = spec_check._extract_sigs(
            "pub fn a() {}\n"
            "spec fn b() -> bool { true }\n"
            "proof fn c() ensures true {}\n"
        )
        self.assertEqual({"a", "b", "c"}, set(sigs))


if __name__ == "__main__":
    unittest.main()
