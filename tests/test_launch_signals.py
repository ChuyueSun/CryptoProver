"""Regression: launch.sh must react to TERM promptly mid-target and must
not orphan the run.py child.

Bash defers trap execution while a foreground child runs. The launcher must
background run.py and wait on it so the trap can forward TERM and reap it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STUB_RUN_PY = """\
import os, signal, sys, time
from pathlib import Path
state = Path(os.environ["LAUNCH_TEST_STATE"])
(state / "child.pid").write_text(str(os.getpid()))
def onterm(signum, frame):
    (state / "child.got-term").write_text("1")
    sys.exit(143)
signal.signal(signal.SIGTERM, onterm)
(state / "child.started").write_text("1")
deadline = time.time() + 60
while time.time() < deadline:
    time.sleep(0.1)
"""


def _wait_for(path: Path, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


@unittest.skipIf(sys.platform == "win32", "POSIX signals required")
class LaunchSignalHandling(unittest.TestCase):
    def test_term_mid_target_exits_promptly_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(td)
            (sandbox / "run.py").write_text(STUB_RUN_PY)
            (sandbox / "launch.sh").symlink_to(ROOT / "launch.sh")
            (sandbox / "usage_audit.py").symlink_to(ROOT / "usage_audit.py")
            (sandbox / "lib").symlink_to(ROOT / "lib", target_is_directory=True)
            project = sandbox / "proj"
            (project / "src").mkdir(parents=True)
            (project / "Cargo.toml").write_text("[package]\nname = 'p'\n")
            (project / "src" / "t.rs").write_text("fn main() {}\n")
            state = sandbox / "state"
            state.mkdir()

            env = dict(os.environ)
            env["LAUNCH_TEST_STATE"] = str(state)
            env["PYTHON"] = sys.executable
            proc = subprocess.Popen(
                [
                    "bash", str(sandbox / "launch.sh"),
                    "--run-id", "sigtest",
                    "--project", str(project),
                    "--results", str(sandbox / "results"),
                    "src/t.rs",
                ],
                cwd=sandbox,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                self.assertTrue(
                    _wait_for(state / "child.started"),
                    "stub run.py never started — launcher preflight failed",
                )
                child_pid = int((state / "child.pid").read_text())

                os.kill(proc.pid, signal.SIGTERM)
                started = time.time()
                rc = proc.wait(timeout=15)
                elapsed = time.time() - started
                self.assertLess(
                    elapsed, 15.0,
                    "launcher deferred the TERM trap behind the child",
                )
                self.assertEqual(rc, 143)
                self.assertTrue(
                    _wait_for(state / "child.got-term", 5),
                    "TERM was not forwarded to the run.py child",
                )
                deadline = time.time() + 5
                alive = True
                while time.time() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        alive = False
                        break
                    time.sleep(0.05)
                self.assertFalse(alive, "run.py child survived as an orphan")
            finally:
                if proc.poll() is None:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
