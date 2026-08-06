import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "launch.sh"
SPEC_PROOF_LAUNCH = ROOT / "launch_spec_proof.sh"
PROOFONLY_LAUNCH = ROOT / "run_proofonly_layers.sh"
RUN_LAYER = ROOT / "run_layer.py"
RACE = ROOT / "race.py"
DEMO_DECOMPRESS = ROOT / "demo_decompress.sh"
SPECGEN = ROOT / "launch_specgen.sh"


class LaunchShellTests(unittest.TestCase):
    def _launch_without_targets(self, run_id: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            project = work / "project"
            project.mkdir()
            return subprocess.run(
                [
                    "bash", str(LAUNCH), "--run-id", run_id,
                    "--project", str(project),
                ],
                cwd=work,
                env={**os.environ, "PYTHON": sys.executable},
                text=True,
                capture_output=True,
                check=False,
            )

    def test_run_id_uses_canonical_validator_before_path_construction(self):
        bad_ids = (
            "../escape", "/absolute", ".hidden", "-flag", "a..b",
            "has/slash", "has\\backslash", "white space", "semi;colon",
            "line\nbreak", "café", "x" * 129,
        )
        for run_id in bad_ids:
            with self.subTest(run_id=run_id):
                proc = self._launch_without_targets(run_id)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("invalid run id", proc.stderr)
                self.assertNotIn("no targets given", proc.stderr)

        valid = self._launch_without_targets("rerun_20260805.1")
        self.assertEqual(valid.returncode, 2)
        self.assertNotIn("invalid run id", valid.stderr)
        self.assertIn("no targets given", valid.stderr)

    def test_result_json_path_is_data_not_python_source(self):
        source = LAUNCH.read_text()
        self.assertIn("d = json.load(open(sys.argv[1]))", source)
        self.assertIn("' \"$result_json\")", source)
        self.assertNotIn("json.load(open('$result_json'))", source)

    def test_spec_proof_run_id_is_validated_before_log_path(self):
        env = {**os.environ, "PYTHON": sys.executable}
        invalid = subprocess.run(
            ["bash", str(SPEC_PROOF_LAUNCH), "--run-id", "../escape"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("invalid run id", invalid.stderr)
        self.assertNotIn("--project not a dir", invalid.stderr)

        valid = subprocess.run(
            ["bash", str(SPEC_PROOF_LAUNCH), "--run-id", "spec-proof_1"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(valid.returncode, 2)
        self.assertNotIn("invalid run id", valid.stderr)
        self.assertIn("--project not a dir", valid.stderr)

    def test_spec_proof_has_no_personal_vstd_default(self):
        source = SPEC_PROOF_LAUNCH.read_text()
        self.assertNotIn("/Users/", source)
        self.assertIn('VSTD="${VSTD_ROOT:-}"', source)
        self.assertIn('"$PYTHON" "$MVP_ROOT/run.py"', source)

    def test_proofonly_validates_run_id_before_tmp_paths(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            proc = subprocess.run(
                [
                    "bash", str(PROOFONLY_LAUNCH), "--project", str(project),
                    "--run-id", "../escape", "--dry-run",
                ],
                cwd=td,
                env={**os.environ, "PYTHON": sys.executable},
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid run id", proc.stderr)
        self.assertNotIn("failed to resolve layers", proc.stderr)

    def test_proofonly_resolves_modules_from_script_dir_not_cwd(self):
        run_id = f"test_{uuid.uuid4().hex}"
        targets = Path("/tmp") / f"targets_{run_id}"
        error_log = Path(f"{targets}.err")
        try:
            with tempfile.TemporaryDirectory() as td:
                project = Path(td) / "project"
                project.mkdir()
                proc = subprocess.run(
                    [
                        "bash", str(PROOFONLY_LAUNCH), "--project", str(project),
                        "--layers", "NOT_A_LAYER", "--run-id", run_id, "--dry-run",
                    ],
                    cwd=td,
                    env={**os.environ, "PYTHON": sys.executable},
                    text=True,
                    capture_output=True,
                    check=False,
                )
            self.assertEqual(proc.returncode, 3)
            self.assertIn("unknown layer set", proc.stderr)
            self.assertNotIn("run this from the repo root", error_log.read_text())
        finally:
            targets.unlink(missing_ok=True)
            error_log.unlink(missing_ok=True)

    def test_run_layer_reports_unsupported_system_python_before_results(self):
        system_python = Path("/usr/bin/python3")
        if not system_python.exists():
            self.skipTest("no system python available")
        version = subprocess.run(
            [str(system_python), "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
            text=True,
            capture_output=True,
            check=True,
        )
        if version.stdout.strip() != "True":
            self.skipTest("system python already satisfies the 3.11 requirement")

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            project = work / "project"
            results = work / "results"
            project.mkdir()
            proc = subprocess.run(
                [
                    str(system_python), str(RUN_LAYER), "L0",
                    "--project", str(project), "--results", str(results),
                    "--run-id", "safe-run",
                ],
                cwd=work,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertFalse(results.exists())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Python 3.11+ required", proc.stderr)
        self.assertNotIn("ModuleNotFoundError", proc.stderr)

    def test_race_reports_unsupported_system_python_before_results(self):
        system_python = Path("/usr/bin/python3")
        if not system_python.exists():
            self.skipTest("no system python available")
        version = subprocess.run(
            [str(system_python), "-c", "import sys; print(sys.version_info[:2] < (3, 11))"],
            text=True,
            capture_output=True,
            check=True,
        )
        if version.stdout.strip() != "True":
            self.skipTest("system python already satisfies the 3.11 requirement")

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            project = work / "project"
            target = project / "target.rs"
            results = work / "results"
            project.mkdir()
            target.write_text("// runtime preflight probe\n")
            proc = subprocess.run(
                [
                    str(system_python), str(RACE), str(target),
                    "--project", str(project), "--results", str(results),
                    "--run-id", "safe-run",
                ],
                cwd=work,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertFalse(results.exists())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Python 3.11+ required", proc.stderr)
        self.assertNotIn("ModuleNotFoundError", proc.stderr)

    def test_race_cli_does_not_relabel_later_runtime_failures(self):
        import race

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            project = work / "project"
            target = project / "target.rs"
            project.mkdir()
            target.write_text("// exception-scope probe\n")
            argv = [
                "race.py", str(target), "--project", str(project),
                "--run-id", "safe-run",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                race, "race_task", side_effect=RuntimeError("git boom")
            ), self.assertRaisesRegex(RuntimeError, "git boom"):
                race.main()

    def test_demo_decompress_validates_run_id_before_demo_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            proc = subprocess.run(
                [
                    "bash", str(DEMO_DECOMPRESS), "--formal-spec",
                    "--run-id", "../escape",
                ],
                cwd=work,
                env={
                    **os.environ,
                    "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
                    "DALEK_UV_PY_BIN": "",
                    "DALEK_VERUS_DIR": "",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertFalse((work / "results").exists())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid run id", proc.stderr)
        self.assertNotIn("cargo-verus not on PATH", proc.stderr)

    def test_demo_decompress_has_no_personal_tool_defaults(self):
        source = DEMO_DECOMPRESS.read_text()
        self.assertNotIn("/Users/", source)
        self.assertIn('UV_PY_BIN="${DALEK_UV_PY_BIN:-}"', source)
        self.assertIn('VERUS_DIR="${DALEK_VERUS_DIR:-}"', source)
        self.assertIn('VSTD="${DALEK_VSTD:-}"', source)
        self.assertIn('[ -n "$VSTD" ] && CMD+=', source)

    def test_specgen_validates_run_id_before_clean_start(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            proc = subprocess.run(
                [
                    "bash", str(SPECGEN), "--formal-spec",
                    "--run-id", "../escape",
                ],
                cwd=work,
                env={
                    **os.environ,
                    "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
                    "DALEK_UV_PY_BIN": "",
                    "DALEK_VERUS_DIR": "",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertFalse((work / "results").exists())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid run id", proc.stderr)
        self.assertNotIn("cargo-verus not on PATH", proc.stderr)
        self.assertNotIn("hard-reset", proc.stderr)

    def test_specgen_bootstrap_is_guarded_and_recoverable(self):
        source = SPECGEN.read_text()
        self.assertNotIn("/Users/", source)
        self.assertNotIn('rm -rf "$GITROOT"', source)
        self.assertIn("quarantine_bootstrap_destination", source)
        self.assertIn("path.is_relative_to(candidate)", source)
        self.assertIn('mv "$GITROOT" "$quarantine"', source)
        self.assertIn('VSTD="${DALEK_VSTD:-}"', source)
        self.assertIn('[ -n "$VSTD" ] && CMD+=', source)

    def test_specgen_refuses_bootstrap_parent_of_source_repo(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            candidate = work / "candidate"
            source_repo = candidate / "source"
            fake_bin = work / "bin"
            source_repo.mkdir(parents=True)
            fake_bin.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(source_repo)], check=True,
                           capture_output=True)
            (source_repo / "README").write_text("guard fixture\n")
            subprocess.run(
                [
                    "git", "-C", str(source_repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "add", "README",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(source_repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-m", "fixture",
                ],
                check=True,
                capture_output=True,
            )
            for command in ("cargo-verus", "claude"):
                stub = fake_bin / command
                stub.write_text("#!/usr/bin/env bash\nexit 0\n")
                stub.chmod(0o755)
            proc = subprocess.run(
                [
                    "bash", str(SPECGEN), "--formal-spec", "--run-id", "safe-run",
                    "--bootstrap", "--skip-warm",
                ],
                cwd=work,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{Path(sys.executable).parent}:{os.environ['PATH']}",
                    "DALEK_UV_PY_BIN": "",
                    "DALEK_VERUS_DIR": "",
                    "DALEK_GITROOT": str(candidate),
                    "DALEK_PROJECT": str(candidate / "worktree" / "curve25519-dalek"),
                    "DALEK_SRCREPO": str(source_repo),
                    "DALEK_SRCREF": "main",
                    "DALEK_VSTD": "",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertTrue(candidate.exists())
            self.assertTrue(source_repo.exists())
            self.assertFalse(list(work.glob("candidate.stale.*")))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("unsafe --bootstrap destination", proc.stderr)


if __name__ == "__main__":
    unittest.main()
