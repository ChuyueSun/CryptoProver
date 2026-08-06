import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "docker" / "trusted_core_supervisor.py"


class TrustedCoreSupervisorTests(unittest.TestCase):
    @staticmethod
    def _seal_package(package: dict) -> None:
        package.pop("package_id", None)
        package["package_id"] = hashlib.sha256(json.dumps(
            package, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()

    def _fixture(self, root: Path, mode: str, *, audit_complete: bool = True):
        immutable = root / "registration.json"
        immutable.write_text(json.dumps({
            "budget": {
                "max_cost_usd": 100,
                "max_wall_seconds": 10000,
            },
        }))
        launch = root / "launch.py"
        launch.write_text(
            "import json,pathlib,sys\n"
            "run_id,root,mode=sys.argv[1:4]\n"
            "base=pathlib.Path(root)/run_id/'ristretto'\n"
            "base.mkdir(parents=True)\n"
            "decision='UNCOMMITTED_CANDIDATE'\n"
            "end='RATE_LIMITED'\n"
            "reusable=False\n"
            "if mode=='bank': decision='BANKED_PARTIAL'; end='LIMIT'; reusable=True\n"
            "if mode=='dominated': decision='BANK_DOMINATED'; end='LIMIT'\n"
            "if mode in ('complete','complete_fail'): decision='ACCEPTED'; end='COMPLETE'; reusable=True\n"
            "if mode=='unknown': end='LIMIT'\n"
            "if mode=='hang': end='RATE_LIMIT_OR_HANG'\n"
            "if mode=='premodel': end='PREMODEL_GATE_INDETERMINATE'\n"
            "result={'end_reason':end,'promotion_receipt':{'decision':decision,"
            "'terminal_disposition':{'reusable':reusable}}}\n"
            "(base/'result.json').write_text(json.dumps(result))\n"
            f"audit={{'cost_status':{'complete' if audit_complete else 'unknown'!r},"
            "'recorded_cost_usd':1.0,'counts':{'unresolved_streams':0},"
            "'launch':{'status':'sealed','segments':[{'started_at':"
            "'2026-08-05T00:00:00Z','ended_at':'2026-08-05T00:00:10Z'}]}}\n"
            "if mode=='premodel': audit={'cost_status':'not_run',"
            "'recorded_cost_usd':0,'counts':{'stream_attempts':0,"
            "'provider_cost_events':0,'unresolved_streams':0},"
            "'launch':{'status':'sealed','segments':[{'started_at':"
            "'2026-08-05T00:00:00Z','ended_at':'2026-08-05T00:00:10Z'}]}}\n"
            "(base.parent/'usage_audit.json').write_text(json.dumps(audit))\n"
            "import time\n"
            "raw=base/'claude_raw'; raw.mkdir(); "
            "(raw/'round_1.jsonl').write_text(json.dumps({'resetsAt':int(time.time())+3600})+'\\n')\n"
            "sys.exit(42 if mode=='rate' else (45 if mode=='complete_fail' else 0))\n"
        )
        campaign = root / "campaign.json"
        campaign.write_text(json.dumps({"stop": False, "stop_reasons": []}))
        package = {
            "schema_version": 1,
            "run_id_prefix": "tc-auto",
            "prelaunch_argv": ["python3", "-c", "pass"],
            "launch_argv": [
                "python3", str(launch), "{run_id}", str(root), mode,
                "--launch-registration", str(immutable),
            ],
            "result_path": str(root / "{run_id}" / "ristretto" / "result.json"),
            "usage_audit_path": str(root / "{run_id}" / "usage_audit.json"),
            "raw_glob": str(root / "{run_id}" / "ristretto" / "claude_raw" / "*.jsonl"),
            "campaign_state_path": str(campaign),
            "immutable_inputs": [{
                "path": str(immutable),
                "sha256": hashlib.sha256(immutable.read_bytes()).hexdigest(),
            }],
        }
        self._seal_package(package)
        package_path = root / "package.json"
        package_path.write_text(json.dumps(package))
        return package_path

    def _run(self, root: Path, package: Path):
        return subprocess.run(
            ["python3", str(SUPERVISOR), "--package", str(package),
             "--state", str(root / "state.json"),
             "--ledger", str(root / "ledger.jsonl"),
             "--lock", str(root / "supervisor.lock"), "--once",
             "--initial-backoff", "1", "--jitter", "0"],
            capture_output=True, text=True,
        )

    def test_rate_limit_is_sealed_and_scheduled_at_provider_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = self._run(root, self._fixture(root, "rate"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["status"], "waiting")
            self.assertIsInstance(state["provider_reset_epoch"], int)
            self.assertGreaterEqual(
                state["next_attempt_epoch"], state["provider_reset_epoch"],
            )
            events = [json.loads(line) for line in (root / "ledger.jsonl").read_text().splitlines()]
            self.assertEqual([event["event"] for event in events], ["LAUNCH", "DECISION", "WAIT"])
            self.assertEqual(events[-1]["next_action"], "RETRY")

    def test_model_tool_input_cannot_choose_provider_reset_time(self):
        import sys
        import time

        sys.path.insert(0, str(ROOT / "docker"))
        try:
            from trusted_core_supervisor import _find_reset_epoch
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "round.jsonl"
            raw.write_text(json.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "input": {"reset_epoch": int(time.time()) + 13 * 86400},
                }]},
            }) + "\n")
            self.assertIsNone(_find_reset_epoch([raw]))

            raw.write_text(json.dumps({
                "type": "rate_limit_event",
                "rate_limit_info": {"resetsAt": int(time.time()) + 3600},
            }) + "\n")
            self.assertIsInstance(_find_reset_epoch([raw]), int)

    def test_rate_limit_with_unresolved_accounting_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = self._run(
                root, self._fixture(root, "rate", audit_complete=False),
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("accounting is not complete", completed.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["status"], "error")

    def test_restart_preserves_future_wait_without_relaunching(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._fixture(root, "rate")
            first = self._run(root, package)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self._run(root, package)
            self.assertEqual(second.returncode, 0, second.stderr)
            events = (root / "ledger.jsonl").read_text().splitlines()
            self.assertEqual(len(events), 3)

    def test_rate_limit_or_hang_is_retryable_with_complete_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = self._run(root, self._fixture(root, "hang"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["status"], "waiting")
            self.assertEqual(state["next_trigger"], "RATE_LIMIT_OR_HANG")

    def test_premodel_indeterminate_is_retryable_without_provider_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = self._run(root, self._fixture(root, "premodel"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["status"], "waiting")
            self.assertEqual(
                state["next_trigger"], "PREMODEL_GATE_INDETERMINATE",
            )

    def test_premodel_indeterminate_rejects_nonzero_provider_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._fixture(root, "premodel")
            launch = root / "launch.py"
            launch.write_text(
                launch.read_text().replace(
                    "'recorded_cost_usd':0",
                    "'recorded_cost_usd':0.01",
                )
            )
            completed = self._run(root, package)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("zero-provider accounting", completed.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["status"], "error")

    def test_complete_requires_reusable_accepted_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = self._run(root, self._fixture(root, "complete"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads((root / "state.json").read_text())["status"], "complete",
            )

    def test_failed_launcher_cannot_promote_complete_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = self._run(root, self._fixture(root, "complete_fail"))
            self.assertEqual(completed.returncode, 2)
            self.assertIn("launcher exited 45", completed.stderr)
            self.assertEqual(
                json.loads((root / "state.json").read_text())["status"], "error",
            )

    def test_prior_retry_cost_counts_against_complete_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._fixture(root, "complete")
            registration = root / "registration.json"
            registration.write_text(json.dumps({
                "budget": {
                    "max_cost_usd": 10,
                    "max_wall_seconds": 10000,
                },
            }))
            package_value = json.loads(package.read_text())
            package_value["immutable_inputs"][0]["sha256"] = hashlib.sha256(
                registration.read_bytes()
            ).hexdigest()
            self._seal_package(package_value)
            package.write_text(json.dumps(package_value))
            (root / "state.json").write_text(json.dumps({
                "schema_version": 1,
                "attempt": 1,
                "package_path": str(package),
                "status": "ready",
                "supervised_cost_usd": 9.5,
                "supervised_wall_seconds": 20,
            }))
            completed = self._run(root, package)
            self.assertEqual(completed.returncode, 3, completed.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["status"], "stop")
            self.assertEqual(state["supervised_cost_usd"], 10.5)
            self.assertIn("COST_CEILING", state["terminal"]["stop_reasons"])

    def test_bank_without_generator_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = self._run(root, self._fixture(root, "bank"))
            self.assertEqual(completed.returncode, 2)
            self.assertIn("lacks next_package_argv", completed.stderr)
            self.assertEqual(
                json.loads((root / "state.json").read_text())["status"], "error",
            )

    def test_unknown_nonreusable_terminal_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = self._run(root, self._fixture(root, "unknown"))
            self.assertEqual(completed.returncode, 3, completed.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["status"], "stop")
            self.assertEqual(state["attempt"], 1)
            events = [json.loads(line) for line in (root / "ledger.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["next_action"], "STOP")

    def test_dominated_bank_stops_without_successor_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = self._run(root, self._fixture(root, "dominated"))
            self.assertEqual(completed.returncode, 3, completed.stderr)
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["status"], "stop")
            events = [
                json.loads(line)
                for line in (root / "ledger.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events[-1]["next_action"], "STOP")
            self.assertEqual(
                events[-1]["trigger"]["decision"], "BANK_DOMINATED",
            )

    def test_immutable_input_mismatch_blocks_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._fixture(root, "rate")
            (root / "registration.json").write_text("changed\n")
            completed = self._run(root, package)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("immutable input mismatch", completed.stderr)
            events = [json.loads(line) for line in (root / "ledger.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["event"], "ERROR")
            self.assertEqual(events[-1]["next_action"], "STOP_FAIL_CLOSED")

    def test_package_command_mutation_between_retries_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._fixture(root, "rate")
            first = self._run(root, package)
            self.assertEqual(first.returncode, 0, first.stderr)
            state_path = root / "state.json"
            state = json.loads(state_path.read_text())
            state["next_attempt_epoch"] = 0
            state_path.write_text(json.dumps(state))

            mutated = json.loads(package.read_text())
            mutated["launch_argv"][4] = "complete"
            self._seal_package(mutated)
            package.write_text(json.dumps(mutated))
            second = self._run(root, package)

            self.assertEqual(second.returncode, 2)
            self.assertIn("persisted package_id", second.stderr)
            final = json.loads(state_path.read_text())
            self.assertEqual(final["status"], "error")
            events = [json.loads(line) for line in
                      (root / "ledger.jsonl").read_text().splitlines()]
            self.assertEqual(
                [event["event"] for event in events].count("LAUNCH"), 1,
            )


class TaxonomyParity(unittest.TestCase):
    """The supervisor's retry/stop sets must track run.py's taxonomy.

    Hand-copied string sets are the drift class that made the supervisor stop
    forever on RATE_LIMIT_OR_HANG while listing a label nothing emits (F3,
    2026-08-03). run.py's module constants are the single source of truth.
    """

    def test_supervisor_sets_track_runner_taxonomy(self):
        import sys

        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "docker"))
        try:
            import run
            import trusted_core_supervisor as supervisor
            from lib import taxonomy
        finally:
            sys.path.pop(0)
            sys.path.pop(0)
        # Identity, not equality-between-copies: both consumers must bind the
        # single shared taxonomy module.
        self.assertIs(supervisor.taxonomy, taxonomy)
        self.assertIs(run.taxonomy, taxonomy)
        self.assertIs(run.AUTORETRY_END_REASONS, taxonomy.AUTORETRY_END_REASONS)
        self.assertEqual(
            supervisor.RETRYABLE_TRANSPORT, set(taxonomy.AUTORETRY_END_REASONS),
        )
        self.assertEqual(
            supervisor.STOP_REASONS, set(taxonomy.STOP_END_REASONS),
        )
        source = (ROOT / "docker" / "trusted_core_supervisor.py").read_text()
        self.assertNotIn('RETRYABLE_TRANSPORT = {', source)
        self.assertNotIn('STOP_REASONS = {', source)
        emitted_terminals = (
            {"COMPLETE"}
            | set(taxonomy.INFRA_END_REASONS)
            | set(taxonomy.CHEAT_END_REASONS)
            | {"FALSE_CONTRACT"}
        )
        for reason in supervisor.STOP_REASONS:
            self.assertIn(reason, emitted_terminals)


if __name__ == "__main__":
    unittest.main()
