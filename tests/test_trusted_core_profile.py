from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lib import provenance
from usage_audit import audit_run
from trusted_core_profile import (
    _assert_registration_predecessor_consistency,
    advance_campaign_state,
    build_lineage_context,
    harness_source_receipt,
    validate_root_replay,
    validate_terminal,
)


class TrustedCoreTerminalProfileTests(unittest.TestCase):
    def test_harness_receipt_covers_top_level_and_nested_executables(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "skills" / "nested").mkdir(parents=True)
            (root / "skills" / "SKILL.md").write_text("skill\n")
            (root / "skills" / "nested" / "helper.py").write_text("one\n")
            (root / "launch.sh").write_text("#!/bin/sh\n")
            first = harness_source_receipt(root)
            (root / "skills" / "nested" / "helper.py").write_text("two\n")
            second = harness_source_receipt(root)
            paths = {entry["path"] for entry in second["files"]}
            self.assertIn("launch.sh", paths)
            self.assertIn("skills/nested/helper.py", paths)
            self.assertNotEqual(first["tree_hash"], second["tree_hash"])

    def test_registration_predecessor_accounting_mismatch_fails_closed(self):
        predecessor = {"receipt_id": "promotion", "tree_hash": "tree"}
        terminal = {"receipt_id": "terminal"}
        state = {"receipt_id": "state", "recorded_cost_usd": 12.5}
        registration = {"predecessor_accounting": {
            "promotion_receipt_id": "promotion",
            "terminal_validation_receipt_id": "terminal",
            "campaign_state_receipt_id": "state",
            "bank_tree_hash": "wrong",
            "accounted_cost_usd": 12.5,
        }}
        with self.assertRaisesRegex(ValueError, "bank_tree_hash mismatch"):
            _assert_registration_predecessor_consistency(
                registration, predecessor, terminal, state,
            )

    def _fixture(self, root: Path) -> tuple[dict, dict, dict]:
        (root / "source.rs").write_text("proof fn done() {}\n")
        tree = provenance.source_tree_receipt(root)
        context = {
            "schema_version": 1,
            "kind": "scored_lineage_context",
            "scoreable": True,
            "lineage_id": "lineage",
            "campaign_spec_sha256": "campaign",
            "start_receipt_id": "start",
            "launch_registration_id": "launch",
            "sterility_receipt_id": "sterility",
            "predecessor": None,
        }
        context["receipt_id"] = provenance.receipt_id(context)
        gate = {
            "fresh": True,
            "exact_tree_match": True,
            "tree_receipt": tree,
            "verus_result": {"okay": True},
            "vector": {
                "hard_admits": 0,
                "raw_errors": 0,
                "verification_errors": 0,
                "resource_limits": 0,
            },
        }
        promotion = {
            "schema_version": 2,
            "decision": "ACCEPTED",
            "scoreable": True,
            **context,
            "final_tree_receipt": tree,
            "acceptance_gate_receipt": gate,
            "terminal_disposition": {"state": "ACCEPTED", "reusable": True},
            "integrity": {
                "spec_drift_count": 0,
                "new_axioms": [],
                "tooling_changes": [],
                "frozen_changes": [],
                "forbidden": {},
            },
            "operator_events": [],
            "launch_registration_id": "launch",
            "sterility_receipt_id": "sterility",
            "lineage_context_receipt_id": context["receipt_id"],
            "predecessor_receipt_id": None,
        }
        promotion["receipt_id"] = provenance.receipt_id(promotion)
        result = {"promotion_receipt": promotion, "duration_seconds": 10}
        usage = {
            "cost_status": "complete",
            "recorded_cost_usd": 2.5,
            "launch": {
                "status": "sealed",
                "segments": [{
                    "started_at": "2026-07-29T00:00:00Z",
                    "ended_at": "2026-07-29T00:00:20Z",
                }],
            },
        }
        return context, result, usage

    def test_accepts_fresh_exact_green_within_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context, result, usage = self._fixture(root)
            receipt = validate_terminal(
                context=context, result=result, usage_audit=usage,
                project=root, max_cost_usd=3, max_wall_seconds=20,
            )
            self.assertTrue(receipt["okay"])
            self.assertEqual(receipt["decision"], "ACCEPTED")
            self.assertEqual(receipt["receipt_id"], provenance.receipt_id(receipt))

    def test_stale_gate_tree_and_budget_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context, result, usage = self._fixture(root)
            result["promotion_receipt"]["acceptance_gate_receipt"][
                "exact_tree_match"
            ] = False
            result["promotion_receipt"]["receipt_id"] = provenance.receipt_id(
                result["promotion_receipt"],
            )
            with self.assertRaisesRegex(ValueError, "fresh green"):
                validate_terminal(
                    context=context, result=result, usage_audit=usage,
                    project=root, max_cost_usd=3, max_wall_seconds=20,
                )

    def test_nonfinite_accounting_and_ceilings_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for field in ("recorded_cost_usd",):
                context, result, usage = self._fixture(root)
                usage[field] = float("nan")
                with self.assertRaisesRegex(ValueError, "finite"):
                    validate_terminal(
                        context=context, result=result, usage_audit=usage,
                        project=root, max_cost_usd=3, max_wall_seconds=20,
                    )

            context, result, usage = self._fixture(root)
            with self.assertRaisesRegex(ValueError, "finite"):
                validate_terminal(
                    context=context, result=result, usage_audit=usage,
                    project=root, max_cost_usd=float("nan"),
                    max_wall_seconds=20,
                )

            context, result, usage = self._fixture(root)
            usage["cost_status"] = "invented"
            with self.assertRaisesRegex(ValueError, "invalid cost status"):
                validate_terminal(
                    context=context, result=result, usage_audit=usage,
                    project=root, max_cost_usd=3, max_wall_seconds=20,
                )

        terminal = {
            "receipt_id": "terminal",
            "decision": "BANKED_PARTIAL",
            "lineage_id": "lineage",
            "recorded_cost_usd": float("nan"),
            "elapsed_seconds": 1,
            "cost_status": "complete",
        }
        vector = {
            "hard_admits": 1,
            "verification_errors": 1,
            "resource_limits": 0,
            "raw_errors": 1,
        }
        with self.assertRaisesRegex(ValueError, "finite"):
            advance_campaign_state(
                prior={}, terminal=terminal, vector=vector, plateau_k=2,
                max_cost_usd=10, max_wall_seconds=100,
            )

            context, result, usage = self._fixture(root)
            usage["recorded_cost_usd"] = 3.1
            with self.assertRaisesRegex(ValueError, "budget exceeded"):
                validate_terminal(
                    context=context, result=result, usage_audit=usage,
                    project=root, max_cost_usd=3, max_wall_seconds=20,
                )

    def test_unknown_cost_is_reported_not_invented(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context, result, usage = self._fixture(root)
            usage.update(cost_status="unknown", recorded_cost_usd=0)
            receipt = validate_terminal(
                context=context, result=result, usage_audit=usage,
                project=root, max_cost_usd=3, max_wall_seconds=20,
            )
            self.assertEqual(receipt["cost_status"], "unknown")
            self.assertEqual(receipt["recorded_cost_usd"], 0)

    def test_plateau_and_unresolved_cost_stop_before_next_leg(self):
        vector = {
            "hard_admits": 4,
            "verification_errors": 3,
            "resource_limits": 1,
            "raw_errors": 5,
        }
        terminal = {
            "receipt_id": "terminal-1",
            "decision": "BANKED_PARTIAL",
            "lineage_id": "lineage",
            "recorded_cost_usd": 1,
            "elapsed_seconds": 10,
            "cost_status": "complete",
        }
        first = advance_campaign_state(
            prior={}, terminal=terminal, vector=vector, plateau_k=2,
            max_cost_usd=10, max_wall_seconds=100,
        )
        terminal = {**terminal, "receipt_id": "terminal-2"}
        second = advance_campaign_state(
            prior=first, terminal=terminal, vector=vector, plateau_k=2,
            max_cost_usd=10, max_wall_seconds=100,
        )
        terminal = {
            **terminal, "receipt_id": "terminal-3", "cost_status": "unknown",
        }
        third = advance_campaign_state(
            prior=second, terminal=terminal, vector=vector, plateau_k=2,
            max_cost_usd=10, max_wall_seconds=100,
        )
        self.assertTrue(third["stop"])
        self.assertIn("PLATEAU", third["stop_reasons"])
        self.assertIn("COST_UNRESOLVED", third["stop_reasons"])

    def test_context_requires_content_addressed_sterility_and_image_harness(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = {
                "campaign": {
                    "campaign_spec_sha256": "campaign",
                    "manifest_sha256": "manifest",
                },
                "transform_receipt": {},
                "validation": {"post_tree_hash": "tree"},
            }
            start["receipt_id"] = provenance.receipt_id(start)
            sterility = {
                "okay": True,
                "container_image_digest": "image",
                "provider_proxy_policy_sha256": "policy",
                "harness_source_tree_sha256": "harness",
                "in_container_probe": {"okay": True},
            }
            sterility["receipt_id"] = provenance.receipt_id(sterility)
            start_path = root / "start.json"
            sterility_path = root / "sterility.json"
            start_path.write_text(json.dumps(start))
            sterility_path.write_text(json.dumps(sterility))
            campaign = {
                "campaign_id": "id",
                "campaign_spec_sha256": "campaign",
                "manifest_sha256": "manifest",
            }
            with (
                mock.patch(
                    "trusted_core_profile.provenance.validate_campaign_spec",
                    return_value=campaign,
                ),
                mock.patch(
                    "trusted_core_profile.provenance.validated_peel_start_envelope",
                    return_value=start,
                ),
                mock.patch(
                    "trusted_core_profile.provenance.validate_launch_registration",
                    return_value={
                        "registration_id": "registration",
                        "hint_level": "H0",
                        "execution": {"model": "test"},
                    },
                ),
                mock.patch(
                    "trusted_core_profile.provenance.source_tree_receipt",
                    return_value={"tree_hash": "tree"},
                ),
                mock.patch(
                    "trusted_core_profile.harness_source_receipt",
                    return_value={"tree_hash": "harness"},
                ),
                mock.patch(
                    "trusted_core_profile._git_value",
                    return_value="commit",
                ),
            ):
                context = build_lineage_context(
                    campaign_path=root / "campaign.json",
                    start_envelope_path=start_path,
                    registration_path=root / "registration.json",
                    sterility_path=sterility_path,
                    manifest_path=root / "manifest.json",
                    project=root,
                    repo_root=root,
                )
                self.assertTrue(context["scoreable"])

                sterility["harness_source_tree_sha256"] = "old-image-harness"
                sterility["receipt_id"] = provenance.receipt_id(sterility)
                sterility_path.write_text(json.dumps(sterility))
                with self.assertRaisesRegex(ValueError, "pinned image harness"):
                    build_lineage_context(
                        campaign_path=root / "campaign.json",
                        start_envelope_path=start_path,
                        registration_path=root / "registration.json",
                        sterility_path=sterility_path,
                        manifest_path=root / "manifest.json",
                        project=root,
                        repo_root=root,
                    )

                sterility["receipt_id"] = "partial"
                sterility_path.write_text(json.dumps(sterility))
                with self.assertRaisesRegex(ValueError, "sterility envelope content"):
                    build_lineage_context(
                        campaign_path=root / "campaign.json",
                        start_envelope_path=start_path,
                        registration_path=root / "registration.json",
                        sterility_path=sterility_path,
                        manifest_path=root / "manifest.json",
                        project=root,
                        repo_root=root,
                    )

    def test_stopped_or_tampered_campaign_state_cannot_advance(self):
        terminal = {
            "receipt_id": "terminal",
            "decision": "BANKED_PARTIAL",
            "lineage_id": "lineage",
            "recorded_cost_usd": 0,
            "elapsed_seconds": 1,
            "cost_status": "complete",
        }
        vector = {
            "hard_admits": 1,
            "verification_errors": 1,
            "resource_limits": 0,
            "raw_errors": 1,
        }
        prior = advance_campaign_state(
            prior={}, terminal=terminal, vector=vector, plateau_k=2,
            max_cost_usd=10, max_wall_seconds=100,
        )
        prior["stop"] = True
        prior["receipt_id"] = provenance.receipt_id(prior)
        with self.assertRaisesRegex(ValueError, "requires a stop"):
            advance_campaign_state(
                prior=prior, terminal=terminal, vector=vector, plateau_k=2,
                max_cost_usd=10, max_wall_seconds=100,
            )
        prior["stop"] = False
        with self.assertRaisesRegex(ValueError, "content ID"):
            advance_campaign_state(
                prior=prior, terminal=terminal, vector=vector, plateau_k=2,
                max_cost_usd=10, max_wall_seconds=100,
            )

    def test_root_replay_preserves_original_lineage_across_new_seal(self):
        campaign = {
            "campaign_id": "campaign",
            "campaign_spec_sha256": "campaign-sha",
            "manifest_sha256": "manifest",
            "source_commit": "commit",
            "source_tree": "source-tree",
            "expected_pre_tree_hash": "pre",
            "expected_post_peel_tree_hash": "post",
            "manifest_file_count": 86,
            "deleted_named_lemma_count": 815,
        }
        validation = {
            "manifest_sha256": "manifest",
            "source_commit": "commit",
            "source_tree": "source-tree",
            "pre_tree_hash": "pre",
            "post_tree_hash": "post",
            "editable_file_count": 86,
            "changed_file_count": 86,
            "observed_deleted_name_count": 815,
            "observed_proof_strip_count": 193,
        }
        authority = {
            "kind": "validated_peel_start",
            "campaign": campaign,
            "validation": validation,
            "transform_receipt": {"sealed_head": "old-seal"},
        }
        authority["receipt_id"] = provenance.receipt_id(authority)
        replay = {
            **authority,
            "transform_receipt": {"sealed_head": "new-seal"},
        }
        replay["receipt_id"] = provenance.receipt_id(replay)
        receipt = validate_root_replay(authority, replay)
        self.assertEqual(
            receipt["authority_start_receipt_id"], authority["receipt_id"],
        )
        self.assertNotEqual(authority["receipt_id"], replay["receipt_id"])

        replay["validation"] = {**validation, "post_tree_hash": "tampered"}
        replay["receipt_id"] = provenance.receipt_id(replay)
        with self.assertRaisesRegex(ValueError, "post_tree_hash"):
            validate_root_replay(authority, replay)

    def test_reconciled_cost_reissues_state_and_unblocks_continuation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "source.rs").write_text("proof fn partial() { admit(); }\n")
            tree = provenance.source_tree_receipt(root)
            start = {
                "kind": "validated_peel_start",
                "campaign": {
                    "campaign_spec_sha256": "campaign",
                    "manifest_sha256": "manifest",
                },
                "validation": {"post_tree_hash": tree["tree_hash"]},
                "transform_receipt": {},
            }
            start["receipt_id"] = provenance.receipt_id(start)
            lineage = provenance.derive_lineage_id(
                "campaign", start["receipt_id"],
            )
            context = {
                "schema_version": 1,
                "kind": "scored_lineage_context",
                "scoreable": True,
                "lineage_id": lineage,
                "campaign_spec_sha256": "campaign",
                "start_receipt_id": start["receipt_id"],
                "launch_registration_id": "launch",
                "sterility_receipt_id": "sterility-prior",
                "expected_tree_hash": tree["tree_hash"],
                "predecessor": None,
                "taints": [],
            }
            context["receipt_id"] = provenance.receipt_id(context)
            gate = {
                "fresh": True,
                "exact_tree_match": True,
                "tree_receipt": tree,
                "verus_result": {"okay": False},
                "vector": {
                    "hard_admits": 1,
                    "verification_errors": 1,
                    "resource_limits": 0,
                    "raw_errors": 1,
                },
                "diagnostic_inventory": [{
                    "file": "source.rs", "kind": "verification",
                    "line": 1, "column": 1, "data": "test failure",
                }],
            }
            promotion = {
                "schema_version": 2,
                "decision": "BANKED_PARTIAL",
                "scoreable": True,
                "lineage_id": lineage,
                "campaign_spec_sha256": "campaign",
                "start_receipt_id": start["receipt_id"],
                "launch_registration_id": "launch",
                "sterility_receipt_id": "sterility-prior",
                "lineage_context_receipt_id": context["receipt_id"],
                "predecessor_receipt_id": None,
                "final_tree_receipt": tree,
                "banking_gate_receipt": gate,
                "terminal_disposition": {
                    "state": "BANKED_PARTIAL",
                    "reusable": True,
                },
                "integrity": {
                    "spec_drift_count": 0,
                    "new_axioms": [],
                    "tooling_changes": [],
                    "frozen_changes": [],
                    "forbidden": {},
                },
                "operator_events": [],
            }
            promotion["receipt_id"] = provenance.receipt_id(promotion)
            result = {"promotion_receipt": promotion, "duration_seconds": 10}
            results_root = root / "results"
            raw_dir = results_root / "run" / "task" / "claude_raw"
            raw_dir.mkdir(parents=True)
            (raw_dir / "round_1.jsonl").write_text(
                json.dumps({"type": "assistant", "message": "partial"}) + "\n"
            )
            (raw_dir / "round_1.lifecycle.json").write_text(json.dumps({
                "status": "agent_exited",
            }))
            usage = audit_run(
                results_root, "run",
                started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:00:20Z",
                launcher_rc=0,
                project=str(root),
                launch_instance_id="leg-1",
            )
            self.assertEqual(usage["cost_status"], "unknown")
            self.assertEqual(usage["counts"]["unresolved_streams"], 1)
            unresolved_terminal = validate_terminal(
                context=context, result=result, usage_audit=usage,
                project=root, max_cost_usd=10, max_wall_seconds=100,
            )
            stopped = advance_campaign_state(
                prior={}, terminal=unresolved_terminal,
                vector=gate["vector"], plateau_k=2,
                max_cost_usd=10, max_wall_seconds=100,
            )
            self.assertIn("COST_UNRESOLVED", stopped["stop_reasons"])

            usage = audit_run(
                results_root, "run",
                started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:00:20Z",
                launcher_rc=0,
                project=str(root),
                launch_instance_id="leg-1",
                equivalent_conservative_cost_usd=Decimal("2.50"),
                equivalent_conservative_source="oauth-usage-receipt-1",
                equivalent_conservative_method="visible tariff x2 + allowance",
                equivalent_conservative_evidence_sha256="a" * 64,
            )
            self.assertEqual(
                usage["cost_status"], "equivalent_conservative")
            conservative_terminal = validate_terminal(
                context=context, result=result, usage_audit=usage,
                project=root, max_cost_usd=10, max_wall_seconds=100,
            )
            self.assertEqual(conservative_terminal["recorded_cost_usd"], 0)
            self.assertEqual(conservative_terminal["accounted_cost_usd"], 2.5)
            self.assertEqual(
                conservative_terminal["cost_evidence"]["evidence_sha256"],
                "a" * 64,
            )
            conservative_state = advance_campaign_state(
                prior={}, terminal=conservative_terminal,
                vector=gate["vector"], plateau_k=2,
                max_cost_usd=10, max_wall_seconds=100,
            )
            self.assertFalse(conservative_state["stop"])
            self.assertNotIn(
                "COST_UNRESOLVED", conservative_state["stop_reasons"])
            self.assertEqual(conservative_state["recorded_cost_usd"], 2.5)

            usage = audit_run(
                results_root, "run",
                started_at="2026-07-29T00:00:00Z",
                ended_at="2026-07-29T00:00:20Z",
                launcher_rc=0,
                project=str(root),
                launch_instance_id="leg-1",
                reconciled_cost_usd=Decimal("1.68"),
                reconciliation_source="provider-receipt-1",
            )
            self.assertEqual(usage["cost_status"], "reconciled")
            self.assertEqual(usage["counts"]["unresolved_streams"], 1)
            reconciled_terminal = validate_terminal(
                context=context, result=result, usage_audit=usage,
                project=root, max_cost_usd=10, max_wall_seconds=100,
            )
            self.assertEqual(reconciled_terminal["recorded_cost_usd"], 0)
            self.assertEqual(reconciled_terminal["accounted_cost_usd"], 1.68)
            resumed = advance_campaign_state(
                prior={}, terminal=reconciled_terminal,
                vector=gate["vector"], plateau_k=2,
                max_cost_usd=10, max_wall_seconds=100,
            )
            self.assertFalse(resumed["stop"])
            self.assertEqual(resumed["recorded_cost_usd"], 1.68)

            start_path = root / "start.json"
            sterility_path = root / "sterility.json"
            predecessor_path = root / "promotion.json"
            terminal_path = root / "terminal.json"
            state_path = root / "state.json"
            start_path.write_text(json.dumps(start))
            sterility = {
                "okay": True,
                "container_image_digest": "image",
                "provider_proxy_policy_sha256": "policy",
                "harness_source_tree_sha256": "harness",
                "in_container_probe": {"okay": True},
            }
            sterility["receipt_id"] = provenance.receipt_id(sterility)
            sterility_path.write_text(json.dumps(sterility))
            predecessor_path.write_text(json.dumps(promotion))
            terminal_path.write_text(json.dumps(reconciled_terminal))
            state_path.write_text(json.dumps(resumed))
            campaign = {
                "campaign_id": "id",
                "campaign_spec_sha256": "campaign",
                "manifest_sha256": "manifest",
            }
            with (
                mock.patch(
                    "trusted_core_profile.provenance.validate_campaign_spec",
                    return_value=campaign,
                ),
                mock.patch(
                    "trusted_core_profile.provenance.validate_launch_registration",
                    return_value={
                        "registration_id": "launch",
                        "hint_level": "H0",
                        "execution": {"model": "test"},
                        "predecessor_accounting": {
                            "promotion_receipt_id": promotion["receipt_id"],
                            "terminal_validation_receipt_id": reconciled_terminal["receipt_id"],
                            "campaign_state_receipt_id": resumed["receipt_id"],
                            "bank_tree_hash": tree["tree_hash"],
                            "accounted_cost_usd": resumed["recorded_cost_usd"],
                        },
                    },
                ),
                mock.patch(
                    "trusted_core_profile.provenance.source_tree_receipt",
                    return_value={"tree_hash": tree["tree_hash"]},
                ),
                mock.patch(
                    "trusted_core_profile.harness_source_receipt",
                    return_value={"tree_hash": "harness"},
                ),
                mock.patch(
                    "trusted_core_profile._git_value",
                    return_value="commit",
                ),
            ):
                next_context = build_lineage_context(
                    campaign_path=root / "campaign.json",
                    start_envelope_path=start_path,
                    registration_path=root / "registration.json",
                    sterility_path=sterility_path,
                    manifest_path=root / "manifest.json",
                    project=root,
                    repo_root=root,
                    predecessor_path=predecessor_path,
                    predecessor_terminal_path=terminal_path,
                    campaign_state_path=state_path,
                    frontier_sidecar_path=root / "frontier.json",
                )
            self.assertEqual(
                next_context["predecessor"]["campaign_state_receipt_id"],
                resumed["receipt_id"],
            )
            frontier = next_context["predecessor"]["frontier"]
            self.assertEqual(frontier["tree_hash"], tree["tree_hash"])
            self.assertEqual(
                frontier["owner_queue"],
                [{"file": "source.rs", "kind": "verification", "count": 1}],
            )
            self.assertEqual(
                provenance.sha256_file(root / "frontier.json"),
                frontier["sidecar_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
