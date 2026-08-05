"""Regression: launch.sh must react to TERM promptly mid-target and must
not orphan the run.py child (T8 F7).

Bash defers trap execution while a FOREGROUND child runs, so the pre-fix
launcher recorded a TERM but did not act on it until the current (possibly
multi-hour) run.py target completed — and never signaled the child at all.
The fix backgrounds run.py and `wait`s on it (wait IS interruptible by
traps); the trap forwards TERM to the child and reaps it before exiting.

This drives the REAL launch.sh from a sandbox: `run.py` is resolved
relative to the launcher's cwd, so a stub run.py stands in for the
orchestrator, while `usage_audit.py` (stdlib-only) is the real one so the
mandatory cost-accounting preflight runs unmodified. Both halves that
codex's T8 verdict asked for are asserted: prompt launcher exit AND child
cleanup (no orphan).
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

# Sleeps far longer than the test's exit deadline: a prompt launcher exit is
# only possible if the trap fired mid-child instead of waiting the child out.
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
            project = sandbox / "proj"
            (project / "src").mkdir(parents=True)
            (project / "Cargo.toml").write_text("[package]\nname = 'p'\n")
            (project / "src" / "t.rs").write_text("fn main() {}\n")
            state = sandbox / "state"
            state.mkdir()

            env = dict(os.environ)
            env["LAUNCH_TEST_STATE"] = str(state)
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
                # Pre-fix, bash sat on the deferred trap for the child's
                # remaining ~60s sleep; prompt exit proves wait+trap works.
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
