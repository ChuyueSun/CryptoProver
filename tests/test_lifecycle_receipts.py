"""Regression tests for runner lifecycle receipts and stream parsing."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import run  # noqa: E402
import run_layer  # noqa: E402
import usage_audit  # noqa: E402


class LifecycleReceiptTests(unittest.TestCase):
    def test_deadline_wait_is_bounded_by_remaining_wall_time(self):
        now = 10_000.0
        self.assertIsNone(run._bounded_wait_timeout(None, now=now))
        self.assertAlmostEqual(run._bounded_wait_timeout(now + 0.25, now=now), 0.25)
        self.assertAlmostEqual(run._bounded_wait_timeout(now - 1, now=now), 0.01)

    def test_terminal_result_survives_later_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "round.jsonl"
            raw.write_text("\n".join([
                json.dumps({"type": "assistant", "message": "working"}),
                json.dumps({"type": "result", "result": "END_REASON:LIMIT", "usage": {"output_tokens": 7}}),
                json.dumps({"type": "task_updated", "task_id": "after-result"}),
            ]) + "\n")
            result, provenance = run._last_claude_result_event(raw)
            self.assertEqual(run.end_reason_from_result(result), "LIMIT")
            self.assertEqual(result["usage"]["output_tokens"], 7)
            self.assertTrue(provenance["result_followed_by_metadata"])
            self.assertEqual(provenance["last_event_type"], "task_updated")

    def test_codex_result_is_normalized_and_carries_exact_thread(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "codex.jsonl"
            raw.write_text("\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"id": "item-1", "type": "agent_message",
                             "text": "work done\nEND_REASON:COMPLETE"},
                }),
                json.dumps({
                    "type": "turn.completed",
                    "usage": {"input_tokens": 11, "cached_input_tokens": 7,
                              "output_tokens": 5, "reasoning_output_tokens": 3},
                }),
            ]) + "\n")
            result, provenance = run._last_agent_result_event(raw, "codex")
            self.assertEqual(run.end_reason_from_result(result), "COMPLETE")
            self.assertEqual(result["_session_id"], "thread-123")
            self.assertEqual(result["usage"]["cache_read_input_tokens"], 7)
            self.assertEqual(provenance["backend"], "codex")
            self.assertTrue(provenance["last_result_seen"])
            usage = run._normalized_agent_usage(result)
            self.assertIsNone(usage["total_cost_usd"])
            self.assertFalse(usage["cost_reported"])
            self.assertIsNone(usage["cost_source"])
            raw_usage = run.summarize_raw_usage(raw)
            self.assertEqual(raw_usage["result_events"], 1)
            self.assertEqual(raw_usage["result_cost_unreported_events"], 1)
            self.assertIsNone(raw_usage["result_total_cost_usd_max"])
            self.assertFalse(raw_usage["result_cost_complete"])

    def test_reported_numeric_zero_is_distinct_from_missing_cost(self):
        usage = run._normalized_agent_usage({
            "usage": {"input_tokens": 3},
            "total_cost_usd": 0.0,
        })
        self.assertEqual(usage["total_cost_usd"], 0.0)
        self.assertTrue(usage["cost_reported"])
        self.assertEqual(usage["cost_source"], "provider_terminal_event")

    def test_layer_usage_preserves_unknown_cost_and_reported_lower_bound(self):
        rounds = [
            SimpleNamespace(claude_usage={
                "total_cost_usd": 2.5,
                "cost_reported": True,
                "input_tokens": 10,
            }),
            SimpleNamespace(claude_usage={
                "total_cost_usd": None,
                "cost_reported": False,
                "output_tokens": 7,
            }),
        ]
        totals = run_layer._aggregate_usage_receipts(rounds)
        self.assertEqual(totals["recorded_cost_usd"], 2.5)
        self.assertEqual(totals["unknown_cost_rounds"], 1)
        self.assertFalse(totals["cost_complete"])
        self.assertIsNone(totals["cost_usd"])
        self.assertEqual(totals["input_tokens"], 10)
        self.assertEqual(totals["output_tokens"], 7)

    def test_layer_usage_accepts_legacy_numeric_cost_receipt(self):
        rounds = [
            SimpleNamespace(claude_usage={"total_cost_usd": 1.25}),
            SimpleNamespace(claude_usage={"total_cost_usd": 0.0}),
        ]
        totals = run_layer._aggregate_usage_receipts(rounds)
        self.assertTrue(totals["cost_complete"])
        self.assertEqual(totals["cost_usd"], 1.25)
        self.assertEqual(totals["unknown_cost_rounds"], 0)

    def test_layer_summary_never_prints_exception_as_exact_zero(self):
        label = run_layer._summary_cost_label({
            "cost_status": "unknown",
            "cost_usd": None,
            "recorded_cost_usd": 0.0,
            "unknown_cost_rounds": 0,
            "unknown_cost_modules": 1,
        })
        self.assertIn("unknown", label)
        self.assertIn("module result(s) unreceipted", label)
        self.assertNotEqual(label, "$0.00")
        self.assertEqual(
            run_layer._summary_cost_label({"cost_status": "not_run"}),
            "n/a (not run)",
        )

    def test_codex_command_uses_json_and_explicit_resume_thread(self):
        fresh = run._build_agent_command(
            "codex", "prove it", "ignored-on-create", False, "gpt-test", None)
        self.assertEqual(fresh[:2], ["codex", "exec"])
        self.assertIn("--json", fresh)
        self.assertIn("--ignore-user-config", fresh)
        self.assertNotIn("ignored-on-create", fresh)
        self.assertEqual(fresh[-1], "prove it")

        resumed = run._build_agent_command(
            "codex", "unused", "thread-123", True, None, "continue safely")
        self.assertEqual(resumed[:3], ["codex", "exec", "resume"])
        self.assertEqual(resumed[-2:], ["thread-123", "continue safely"])

    def test_codex_error_event_preserves_rate_limit_status(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "codex-error.jsonl"
            raw.write_text("\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thread-429"}),
                json.dumps({"type": "error", "message": "HTTP 429 rate limit"}),
            ]) + "\n")
            result, _ = run._last_agent_result_event(raw, "codex")
            self.assertTrue(result["is_error"])
            self.assertEqual(result["api_error_status"], 429)

    def test_codex_command_events_feed_existing_integrity_audits(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "codex.jsonl"
            item = {"id": "cmd-1", "type": "command_execution",
                    "command": "git show HEAD:src/secret.rs"}
            raw.write_text("\n".join([
                json.dumps({"type": "item.started", "item": item}),
                json.dumps({"type": "item.completed", "item": item}),
            ]) + "\n")
            self.assertEqual(run.count_agent_actions(raw), 1)
            self.assertTrue(run.detect_git_recovery(raw))

    def test_gate_receipt_signs_all_gate_components_and_persists_final_vector(self):
        anchor = ["python3", "verus_check.py", "anchor.rs"]
        dep = ["python3", "verus_check.py", "dep.rs"]
        self.assertNotEqual(
            run._gate_signature_for_commands([anchor])["signature"],
            run._gate_signature_for_commands([anchor, dep])["signature"],
        )
        with tempfile.TemporaryDirectory() as td:
            result = {"okay": False, "messages": [], "error_count": 4}
            receipt = run._gate_receipt(
                Path(td), 1, {"tree_hash": "tree-a"}, [anchor, dep], result, 1,
            )
            result["error_count"] = 9
            receipt["verus_result"] = result
            receipt["vector"] = run._gate_vector(result)
            run._persist_gate_receipt(receipt)
            persisted = json.loads(Path(receipt["receipt_path"]).read_text())
            self.assertEqual(persisted["gate_commands"], [anchor, dep])
            self.assertEqual(persisted["verus_result"]["error_count"], 9)
            self.assertNotIn("receipt_path", persisted)

    def _usage_fixture(
        self,
        root: Path,
        raw_events: list[dict | str] | None,
        *,
        round_cost: float | None = None,
        lifecycle_status: str = "agent_exited",
    ) -> None:
        task = root / "run-1" / "target"
        raw_dir = task / "claude_raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / "round_1.lifecycle.json").write_text(json.dumps({
            "round_number": 1,
            "status": lifecycle_status,
        }))
        if raw_events is not None:
            lines = [
                event if isinstance(event, str) else json.dumps(event)
                for event in raw_events
            ]
            (raw_dir / "round_1.jsonl").write_text("\n".join(lines) + "\n")
        usage = {}
        if round_cost is not None:
            usage = {
                "total_cost_usd": round_cost,
                "cost_reported": True,
                "cost_source": "provider_terminal_event",
            }
        (task / "round_1.json").write_text(json.dumps({
            "round_number": 1,
            "claude_usage": usage,
        }))

    def test_usage_audit_complete_provider_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results"
            self._usage_fixture(
                root,
                [{"type": "result", "total_cost_usd": 2.5}],
                round_cost=2.5,
            )
            audit = usage_audit.audit_run(
                root, "run-1", started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:10:00Z", launcher_rc=0,
            )
            self.assertEqual(audit["cost_status"], "complete")
            self.assertEqual(audit["recorded_cost_usd"], 2.5)
            self.assertEqual(audit["counts"]["unresolved_streams"], 0)

    def test_usage_audit_multiple_results_uses_max_but_stays_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results"
            self._usage_fixture(
                root,
                [
                    {"type": "result", "total_cost_usd": 2.0},
                    {"type": "result", "total_cost_usd": 3.0},
                ],
                round_cost=3.0,
            )
            audit = usage_audit.audit_run(
                root, "run-1", started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:10:00Z", launcher_rc=0,
            )
            self.assertEqual(audit["cost_status"], "lower_bound")
            self.assertEqual(audit["recorded_cost_usd"], 3.0)
            self.assertIn(
                "multiple_terminal_events=2",
                audit["streams"][0]["unresolved_reasons"],
            )

    def test_usage_audit_round_without_raw_is_a_lower_bound(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results"
            self._usage_fixture(root, None, round_cost=1.25)
            audit = usage_audit.audit_run(
                root, "run-1", started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:10:00Z", launcher_rc=0,
            )
            self.assertEqual(audit["cost_status"], "lower_bound")
            self.assertEqual(audit["recorded_cost_usd"], 1.25)
            self.assertEqual(
                audit["counts"]["round_or_lifecycle_without_raw"], 1)

    def test_usage_audit_malformed_stream_cannot_claim_complete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results"
            self._usage_fixture(
                root,
                [
                    "{not-json",
                    {"type": "result", "total_cost_usd": 4.0},
                ],
                round_cost=4.0,
            )
            audit = usage_audit.audit_run(
                root, "run-1", started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:10:00Z", launcher_rc=0,
            )
            self.assertEqual(audit["cost_status"], "lower_bound")
            self.assertIn(
                "raw_parse_errors=1",
                audit["streams"][0]["unresolved_reasons"],
            )

    def test_usage_audit_unreceipted_stream_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results"
            self._usage_fixture(
                root,
                [{"type": "assistant", "message": "work was interrupted"}],
            )
            audit = usage_audit.audit_run(
                root, "run-1", started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:10:00Z", launcher_rc=1,
            )
            self.assertEqual(audit["cost_status"], "unknown")
            self.assertEqual(audit["recorded_cost_usd"], 0.0)
            self.assertIn(
                "raw_stream_without_terminal_event",
                audit["streams"][0]["unresolved_reasons"],
            )

    def test_usage_audit_pending_lifecycle_keeps_receipt_as_lower_bound(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results"
            self._usage_fixture(
                root,
                [{"type": "result", "total_cost_usd": 1.75}],
                round_cost=1.75,
                lifecycle_status="agent_started",
            )
            audit = usage_audit.audit_run(
                root, "run-1", started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:10:00Z", launcher_rc=1,
            )
            self.assertEqual(audit["cost_status"], "lower_bound")
            self.assertEqual(audit["recorded_cost_usd"], 1.75)
            self.assertIn(
                "lifecycle_not_exited=agent_started",
                audit["streams"][0]["unresolved_reasons"],
            )

    def test_usage_audit_reconciliation_cannot_undercut_receipt_floor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results"
            self._usage_fixture(
                root,
                [{"type": "result", "total_cost_usd": 3.0}],
                round_cost=3.0,
            )
            audit = usage_audit.audit_run(
                root, "run-1", started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:10:00Z", launcher_rc=0,
                reconciled_cost_usd=Decimal("2.99"),
                reconciliation_source="provider-export.csv",
            )
            self.assertEqual(audit["cost_status"], "conflict")
            self.assertEqual(audit["reconciliation"]["status"], "conflict")
            self.assertLess(audit["reconciliation"]["gap_usd"], 0)

    def test_usage_audit_resume_archives_prior_attempt_without_losing_cost(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "results"
            self._usage_fixture(
                root,
                [{"type": "result", "total_cost_usd": 2.5}],
                round_cost=2.5,
            )
            first = usage_audit.audit_run(
                root, "run-1", started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:10:00Z", launcher_rc=42,
                launch_instance_id="launch-a",
            )
            self.assertEqual(first["recorded_cost_usd"], 2.5)
            archived = usage_audit.archive_task_receipts(
                root, "run-1", "target", "launch-b")
            self.assertIsNotNone(archived)
            self._usage_fixture(
                root,
                [{"type": "result", "total_cost_usd": 1.5}],
                round_cost=1.5,
            )
            resumed = usage_audit.audit_run(
                root, "run-1", started_at="2026-07-29T01:00:00Z",
                ended_at="2026-07-29T01:10:00Z", launcher_rc=0,
                launch_instance_id="launch-b",
            )
            self.assertEqual(resumed["cost_status"], "complete")
            self.assertEqual(resumed["recorded_cost_usd"], 4.0)
            self.assertEqual(len(resumed["launch"]["segments"]), 2)
            self.assertEqual(resumed["tasks"][0]["stream_attempts"], 2)
            self.assertTrue(any(
                stream["source"].startswith("archive:")
                for stream in resumed["streams"]
            ))


if __name__ == "__main__":
    unittest.main()
