"""Discovery-brief editable-set filter.

Pins the fix for the self-reinforcing FROZEN_EDIT loop: when an experiment
editable set is supplied, an edit to a file *outside* that set (a frozen file
the agent illegally touched) must NOT be re-recommended as "start here" — it is
split into a do-not-touch warning instead. With no editable set, behaviour is
unchanged (every edited file is "start here").

Run: python3 -m unittest tests.test_discovery_brief
"""
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import discovery_brief as db  # noqa: E402


def _assistant_edit(path: str) -> str:
    """One stream-json assistant line carrying an Edit tool_use on `path`."""
    return json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": path}},
        ]},
    })


class EditableFilter(unittest.TestCase):
    def _mine(self, edited_abs, project, editable=None):
        with tempfile.TemporaryDirectory() as d:
            tdir = Path(d)
            raw = tdir / "claude_raw"
            raw.mkdir()
            (raw / "round_1.jsonl").write_text(
                "\n".join(_assistant_edit(p) for p in edited_abs))
            return db.mine(tdir, project, editable=editable)

    def test_none_editable_preserves_all_edits(self):
        proj = Path("/proj")
        out = self._mine(
            ["/proj/src/montgomery.rs", "/proj/src/lemmas/frozen_lemmas.rs"],
            proj, editable=None)
        self.assertEqual(set(out["edits"]),
                         {"src/montgomery.rs", "src/lemmas/frozen_lemmas.rs"})
        self.assertEqual(out["frozen_edits"], [])

    def test_editable_set_splits_frozen_edit_out(self):
        proj = Path("/proj")
        editable = {"src/montgomery.rs"}
        out = self._mine(
            ["/proj/src/montgomery.rs", "/proj/src/lemmas/frozen_lemmas.rs"],
            proj, editable=editable)
        # The editable file is "start here"; the frozen file is quarantined.
        self.assertEqual(out["edits"], ["src/montgomery.rs"])
        self.assertEqual(out["frozen_edits"], ["src/lemmas/frozen_lemmas.rs"])

    def test_render_warns_on_frozen_edits(self):
        rendered = db.render({
            "reads": [], "edits": ["src/montgomery.rs"],
            "frozen_edits": ["src/lemmas/frozen_lemmas.rs"], "searches": [],
        })
        self.assertIn("start here", rendered)
        self.assertIn("src/montgomery.rs", rendered)
        self.assertIn("do NOT touch", rendered)
        self.assertIn("frozen_lemmas.rs", rendered)

    def test_render_no_frozen_section_when_empty(self):
        rendered = db.render({
            "reads": [], "edits": ["src/montgomery.rs"],
            "frozen_edits": [], "searches": [],
        })
        self.assertNotIn("do NOT touch", rendered)


if __name__ == "__main__":
    unittest.main()
