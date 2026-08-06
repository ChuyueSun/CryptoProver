import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MONITOR = ROOT / "monitor_run.sh"


class MonitorShellTests(unittest.TestCase):
    def test_once_renders_tool_summary_with_configured_python(self):
        with tempfile.TemporaryDirectory() as td:
            result_dir = Path(td) / "task"
            raw = result_dir / "claude_raw"
            raw.mkdir(parents=True)
            events = [
                {
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use", "name": "Bash",
                        "input": {"command": "python3 skills/verus_check.py target.rs"},
                    }]},
                },
                {
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use", "name": "Agent",
                        "input": {"description": "check proof"},
                    }]},
                },
            ]
            (raw / "round_1.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )
            proc = subprocess.run(
                ["bash", str(MONITOR), str(result_dir), "--once"],
                cwd=ROOT,
                env={**os.environ, "PYTHON": sys.executable},
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("2 tool calls", proc.stdout)
        self.assertIn("verus_check.py", proc.stdout)
        self.assertIn("subagents: 1", proc.stdout)

    def test_monitor_keeps_offsets_and_incomplete_line_position(self):
        source = MONITOR.read_text()
        self.assertNotIn("/Users/", source)
        self.assertIn("positions = {}", source)
        self.assertIn('if not line.endswith("\\n"):', source)
        self.assertIn("stream.seek(line_start)", source)
        self.assertIn("except BrokenPipeError:", source)
        self.assertEqual(source.count("exec \"$PYBIN\""), 1)


if __name__ == "__main__":
    unittest.main()
