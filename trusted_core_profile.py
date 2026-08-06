#!/usr/bin/env python3
"""Fail-closed composition helpers for a scored Trust Core campaign."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import provenance

_HARNESS_TOP_FILES = (
    "run.py", "prompt.md", "prompt_decompose.md", "peel.py", "admit.py",
    "strip_specs.py", "usage_audit.py", "trusted_core_profile.py",
    "skills/SKILL.md",
)
_HARNESS_DIRS = ("lib", "skills", "docker", "scripts")


def _finite_number(value: Any, label: str, *, positive: bool = False) -> float:
    """Parse a receipt number without allowing NaN/Infinity ceiling bypasses."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        qualifier = "positive finite" if positive else "non-negative finite"
        raise ValueError(f"{label} must be a {qualifier} number")
    return number


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON receipt: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"receipt is not an object: {path}")
    return value


def _assert_registration_predecessor_consistency(
    registration: dict[str, Any], predecessor: dict[str, Any],
    predecessor_terminal: dict[str, Any], campaign_state: dict[str, Any],
) -> None:
    """Reject documentary predecessor fields that contradict authority."""
    registered = registration.get("predecessor_accounting")
    if not isinstance(registered, dict):
        raise ValueError("registration lacks predecessor_accounting")
    expected_fields = {
        "promotion_receipt_id": predecessor["receipt_id"],
        "terminal_validation_receipt_id": predecessor_terminal["receipt_id"],
        "campaign_state_receipt_id": campaign_state["receipt_id"],
        "bank_tree_hash": predecessor["tree_hash"],
        "accounted_cost_usd": campaign_state.get("recorded_cost_usd"),
    }
    for field, expected in expected_fields.items():
        if registered.get(field) != expected:
            raise ValueError(
                f"registration predecessor_accounting.{field} mismatch: "
                f"{registered.get(field)!r} != {expected!r}"
            )


def _git_value(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"cannot resolve Git identity under {root}") from exc


def harness_source_receipt(repo_root: Path) -> dict[str, Any]:
    """Hash the executable harness surface, excluding ledgers/results/docs."""
    paths = [
        *(repo_root / name for name in _HARNESS_TOP_FILES),
        *repo_root.glob("*.py"), *repo_root.glob("*.sh"),
        *repo_root.glob("prompt*.md"),
    ]
    for directory in _HARNESS_DIRS:
        paths.extend(sorted((repo_root / directory).glob("**/*.py")))
        paths.extend(sorted((repo_root / directory).glob("**/*.sh")))
        paths.extend(sorted((repo_root / directory).glob("**/*.json")))
    entries = []
    for path in sorted(set(paths)):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError(f"harness source must not be a symlink: {path}")
        if not path.is_file():
            raise ValueError(f"required harness source is missing: {path}")
        data = path.read_bytes()
        entries.append({
            "path": path.relative_to(repo_root).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    tree_hash = hashlib.sha256(
        provenance.canonical_json_bytes(entries)
    ).hexdigest()
    return {"schema_version": 1, "tree_hash": tree_hash, "files": entries}


def docker_start_envelope(
    *,
    peel_json_path: Path,
    manifest_path: Path,
    campaign_path: Path,
    project: Path,
    repo_root: Path,
) -> dict[str, Any]:
    """Rebind the canonical peel receipt to the actual isolated Docker seal."""
    peel = _json(peel_json_path)
    receipt = dict(peel.get("transform_receipt") or {})
    if receipt.get("kind") != "canonical_peel_start":
        raise ValueError("peel JSON lacks a canonical start receipt")
    workspace = provenance.workspace_root(project)
    receipt["sealed_head"] = _git_value(workspace, "rev-parse", "HEAD^{commit}")
    receipt["sealed_head_tree"] = _git_value(workspace, "rev-parse", "HEAD^{tree}")
    post = provenance.source_tree_receipt(project)
    post.pop("workspace_root", None)
    receipt["post_tree_receipt"] = post
    receipt["post_seal_tree_unchanged"] = True
    receipt["sealed_worktree_clean"] = not bool(
        _git_value(workspace, "status", "--porcelain")
    )
    receipt["receipt_id"] = provenance.receipt_id(receipt)
    return provenance.validated_peel_start_envelope(
        receipt,
        manifest_path=manifest_path,
        project=project,
        tool_root=repo_root,
        campaign_spec_path=campaign_path,
    )


def build_lineage_context(
    *,
    campaign_path: Path,
    start_envelope_path: Path,
    registration_path: Path,
    sterility_path: Path,
    manifest_path: Path,
    project: Path,
    repo_root: Path,
    predecessor_path: Path | None = None,
    predecessor_terminal_path: Path | None = None,
    campaign_state_path: Path | None = None,
    frontier_sidecar_path: Path | None = None,
) -> dict[str, Any]:
    campaign = provenance.validate_campaign_spec(
        campaign_path, repo_root=repo_root, require_launch_budget=False,
    )
    stored_start = _json(start_envelope_path)
    if stored_start.get("receipt_id") != provenance.receipt_id(stored_start):
        raise ValueError("start envelope content ID mismatch")
    if predecessor_path is None:
        rebuilt_start = provenance.validated_peel_start_envelope(
            stored_start.get("transform_receipt") or {},
            manifest_path=manifest_path,
            project=project,
            tool_root=repo_root,
            campaign_spec_path=campaign_path,
        )
        if rebuilt_start != stored_start:
            raise ValueError("start envelope does not match current canonical peel")
    else:
        stored_campaign = stored_start.get("campaign") or {}
        if (
            stored_campaign.get("campaign_spec_sha256")
            != campaign["campaign_spec_sha256"]
            or stored_campaign.get("manifest_sha256") != campaign["manifest_sha256"]
        ):
            raise ValueError("predecessor start envelope campaign mismatch")
    sterility = _json(sterility_path)
    if sterility.get("receipt_id") != provenance.receipt_id(sterility):
        raise ValueError("sterility envelope content ID mismatch")
    if sterility.get("okay") is not True:
        raise ValueError("sterility envelope is not okay")
    probe = sterility.get("in_container_probe") or {}
    if probe.get("okay") is not True:
        raise ValueError("in-container sterility probe is not okay")
    host_harness_tree = harness_source_receipt(repo_root)["tree_hash"]
    registration = provenance.validate_launch_registration(
        registration_path,
        campaign=campaign,
        actual_image_digest=sterility.get("container_image_digest"),
        actual_harness_commit=_git_value(repo_root, "rev-parse", "HEAD^{commit}"),
        actual_harness_tree_sha256=host_harness_tree,
        actual_proxy_policy_sha256=sterility.get(
            "provider_proxy_policy_sha256"
        ),
    )
    if sterility.get("harness_source_tree_sha256") != host_harness_tree:
        raise ValueError("pinned image harness bytes do not match the registered host harness")
    start_id = stored_start["receipt_id"]
    lineage_id = provenance.derive_lineage_id(
        campaign["campaign_spec_sha256"], start_id,
    )
    predecessor = None
    if predecessor_path is not None:
        if (
            predecessor_terminal_path is None
            or campaign_state_path is None
            or frontier_sidecar_path is None
        ):
            raise ValueError(
                "predecessor promotion requires terminal validation, campaign "
                "state, and a frontier sidecar"
            )
        raw_predecessor = _json(predecessor_path)
        if not isinstance(raw_predecessor.get("lineage_context_receipt_id"), str):
            raise ValueError("predecessor lacks a lineage-context binding")
        if not isinstance(raw_predecessor.get("sterility_receipt_id"), str):
            raise ValueError("predecessor lacks a sterility binding")
        predecessor = provenance.reusable_seed_authority(predecessor_path)
        if predecessor["lineage_id"] != lineage_id:
            raise ValueError("predecessor belongs to another lineage")
        if predecessor.get("campaign_spec_sha256") != campaign[
            "campaign_spec_sha256"
        ]:
            raise ValueError("predecessor belongs to another campaign")
        predecessor_terminal = _json(predecessor_terminal_path)
        if (
            predecessor_terminal.get("receipt_id")
            != provenance.receipt_id(predecessor_terminal)
            or predecessor_terminal.get("kind")
            != "trusted_core_terminal_validation"
            or predecessor_terminal.get("okay") is not True
            or predecessor_terminal.get("promotion_receipt_id")
            != predecessor["receipt_id"]
            or predecessor_terminal.get("lineage_id") != lineage_id
            or predecessor_terminal.get("decision") != raw_predecessor.get("decision")
        ):
            raise ValueError("predecessor terminal validation chain mismatch")
        campaign_state = _json(campaign_state_path)
        legs = campaign_state.get("legs") or []
        if (
            campaign_state.get("receipt_id") != provenance.receipt_id(campaign_state)
            or campaign_state.get("kind") != "trusted_core_campaign_state"
            or campaign_state.get("lineage_id") != lineage_id
            or campaign_state.get("stop") is True
            or not legs
            or legs[-1].get("terminal_receipt_id")
            != predecessor_terminal["receipt_id"]
        ):
            raise ValueError("predecessor campaign state chain mismatch")
        gate_key = (
            "acceptance_gate_receipt"
            if raw_predecessor.get("decision") == "ACCEPTED"
            else "banking_gate_receipt"
        )
        frontier_gate = raw_predecessor.get(gate_key) or {}
        frontier_vector = frontier_gate.get("vector")
        diagnostic_inventory = frontier_gate.get("diagnostic_inventory")
        if not isinstance(frontier_vector, dict) or not isinstance(
            diagnostic_inventory, list
        ):
            raise ValueError("predecessor canonical gate lacks frontier evidence")
        owner_queue = provenance.diagnostic_owner_queue(diagnostic_inventory)

        _assert_registration_predecessor_consistency(
            registration, predecessor, predecessor_terminal, campaign_state,
        )
        sidecar = {
            "schema_version": 1,
            "kind": "scored_predecessor_frontier",
            "source_promotion_receipt_id": predecessor["receipt_id"],
            "tree_hash": predecessor["tree_hash"],
            "vector": dict(frontier_vector),
            "diagnostic_inventory": diagnostic_inventory,
        }
        sidecar["receipt_id"] = provenance.receipt_id(sidecar)
        provenance.write_immutable_json(frontier_sidecar_path, sidecar)
        predecessor = {
            **predecessor,
            "terminal_validation_receipt_id": predecessor_terminal["receipt_id"],
            "campaign_state_receipt_id": campaign_state["receipt_id"],
            "frontier": {
                "schema_version": 1,
                "tree_hash": predecessor["tree_hash"],
                "vector": dict(frontier_vector),
                "owner_queue": owner_queue,
                "sidecar_name": frontier_sidecar_path.name,
                "sidecar_sha256": provenance.sha256_file(frontier_sidecar_path),
                "sidecar_receipt_id": sidecar["receipt_id"],
            },
        }
    expected_tree = (
        predecessor["tree_hash"]
        if predecessor is not None
        else stored_start["validation"]["post_tree_hash"]
    )
    actual_tree = provenance.source_tree_receipt(project)["tree_hash"]
    if actual_tree != expected_tree:
        raise ValueError("canonical replay tree does not match its authority")
    context = {
        "schema_version": 1,
        "kind": "scored_lineage_context",
        "scoreable": True,
        "lineage_id": lineage_id,
        "campaign_spec_sha256": campaign["campaign_spec_sha256"],
        "start_receipt_id": start_id,
        "launch_registration_id": registration["registration_id"],
        "sterility_receipt_id": provenance.receipt_id(sterility),
        "hint_level": registration["hint_level"],
        "execution": registration["execution"],
        "expected_tree_hash": expected_tree,
        "predecessor": predecessor,
        "taints": [],
    }
    context["receipt_id"] = provenance.receipt_id(context)
    return context


def validate_root_replay(
    authority: dict[str, Any],
    replay: dict[str, Any],
) -> dict[str, Any]:
    """Bind a fresh canonical peel to the original lineage-root authority."""
    for label, value in (("authority", authority), ("replay", replay)):
        if (
            value.get("kind") != "validated_peel_start"
            or value.get("receipt_id") != provenance.receipt_id(value)
        ):
            raise ValueError(f"{label} start envelope content ID mismatch")
    campaign_fields = (
        "campaign_id", "campaign_spec_sha256", "manifest_sha256",
        "source_commit", "source_tree", "expected_pre_tree_hash",
        "expected_post_peel_tree_hash", "manifest_file_count",
        "deleted_named_lemma_count",
    )
    validation_fields = (
        "manifest_sha256", "source_commit", "source_tree", "pre_tree_hash",
        "post_tree_hash", "editable_file_count", "changed_file_count",
        "observed_deleted_name_count", "observed_proof_strip_count",
    )
    for field in campaign_fields:
        if (authority.get("campaign") or {}).get(field) != (
            replay.get("campaign") or {}
        ).get(field):
            raise ValueError(f"root replay campaign {field} mismatch")
    for field in validation_fields:
        if (authority.get("validation") or {}).get(field) != (
            replay.get("validation") or {}
        ).get(field):
            raise ValueError(f"root replay validation {field} mismatch")
    receipt = {
        "schema_version": 1,
        "kind": "trusted_core_root_replay_validation",
        "okay": True,
        "authority_start_receipt_id": authority["receipt_id"],
        "replay_start_receipt_id": replay["receipt_id"],
        "campaign_spec_sha256": authority["campaign"]["campaign_spec_sha256"],
        "manifest_sha256": authority["validation"]["manifest_sha256"],
        "pre_tree_hash": authority["validation"]["pre_tree_hash"],
        "post_tree_hash": authority["validation"]["post_tree_hash"],
    }
    receipt["receipt_id"] = provenance.receipt_id(receipt)
    return receipt


def validate_terminal(
    *,
    context: dict[str, Any],
    result: dict[str, Any],
    usage_audit: dict[str, Any],
    project: Path,
    max_cost_usd: float,
    max_wall_seconds: float,
) -> dict[str, Any]:
    max_cost_usd = _finite_number(
        max_cost_usd, "maximum cost", positive=True,
    )
    max_wall_seconds = _finite_number(
        max_wall_seconds, "maximum wall time", positive=True,
    )
    if (
        context.get("kind") != "scored_lineage_context"
        or context.get("scoreable") is not True
        or context.get("receipt_id") != provenance.receipt_id(context)
    ):
        raise ValueError("terminal lineage context is not a valid scored receipt")
    promotion = result.get("promotion_receipt") or {}
    if promotion.get("receipt_id") != provenance.receipt_id(promotion):
        raise ValueError("terminal promotion receipt content ID mismatch")
    for key in ("lineage_id", "campaign_spec_sha256", "start_receipt_id"):
        if promotion.get(key) != context.get(key):
            raise ValueError(f"terminal promotion {key} mismatch")
    if promotion.get("launch_registration_id") != context.get(
        "launch_registration_id"
    ):
        raise ValueError("terminal promotion launch registration mismatch")
    if promotion.get("sterility_receipt_id") != context.get(
        "sterility_receipt_id"
    ):
        raise ValueError("terminal promotion sterility receipt mismatch")
    if promotion.get("lineage_context_receipt_id") != context.get("receipt_id"):
        raise ValueError("terminal promotion lineage context mismatch")
    expected_predecessor = (context.get("predecessor") or {}).get("receipt_id")
    if promotion.get("predecessor_receipt_id") != expected_predecessor:
        raise ValueError("terminal promotion predecessor mismatch")
    integrity = promotion.get("integrity") or {}
    if (
        integrity.get("spec_drift_count")
        or integrity.get("new_axioms")
        or integrity.get("tooling_changes")
        or integrity.get("frozen_changes")
        or integrity.get("forbidden")
        or promotion.get("operator_events")
    ):
        raise ValueError("terminal promotion carries integrity or operator taint")
    tree_hash = provenance.source_tree_receipt(project)["tree_hash"]
    if (promotion.get("final_tree_receipt") or {}).get("tree_hash") != tree_hash:
        raise ValueError("terminal promotion does not cover current source")
    decision = promotion.get("decision")
    if decision == "ACCEPTED":
        gate = promotion.get("acceptance_gate_receipt") or {}
        if (
            promotion.get("scoreable") is not True
            or (promotion.get("terminal_disposition") or {}).get("state")
            != "ACCEPTED"
            or gate.get("fresh") is not True
            or gate.get("exact_tree_match") is not True
            or (gate.get("tree_receipt") or {}).get("tree_hash") != tree_hash
            or (gate.get("verus_result") or {}).get("okay") is not True
            or (gate.get("vector") or {}).get("hard_admits") != 0
        ):
            raise ValueError("ACCEPTED lacks a fresh green exact-tree gate")
    elif decision == "BANKED_PARTIAL":
        gate = promotion.get("banking_gate_receipt") or {}
        if (
            promotion.get("scoreable") is not True
            or (promotion.get("terminal_disposition") or {}).get("state")
            != "BANKED_PARTIAL"
            or gate.get("fresh") is not True
            or gate.get("exact_tree_match") is not True
            or (gate.get("tree_receipt") or {}).get("tree_hash") != tree_hash
        ):
            raise ValueError("BANKED_PARTIAL lacks a fresh exact-tree gate")
    else:
        raise ValueError("terminal result is not reusable")
    cost_status = usage_audit.get("cost_status")
    recorded_cost = _finite_number(
        usage_audit.get("recorded_cost_usd", 0), "recorded cost",
    )
    accounted_cost = recorded_cost
    cost_evidence = None
    if cost_status == "complete":
        counts = usage_audit.get("counts") or {}
        if not isinstance(counts, dict):
            raise ValueError("usage audit counts are malformed")
        if counts.get("unresolved_streams", 0) != 0:
            raise ValueError("complete usage audit carries unresolved streams")
    elif cost_status == "reconciled":
        reconciliation = usage_audit.get("reconciliation") or {}
        if not isinstance(reconciliation, dict) or (
            reconciliation.get("status") != "accepted"
        ):
            raise ValueError("usage reconciliation is not accepted")
        if "reconciled_cost_usd" not in reconciliation:
            raise ValueError(
                "usage reconciliation lacks a numeric accounted cost"
            )
        accounted_cost = _finite_number(
            reconciliation["reconciled_cost_usd"], "reconciled cost",
        )
        if accounted_cost < recorded_cost:
            raise ValueError("usage reconciliation undercuts recorded receipts")
    elif cost_status == "equivalent_conservative":
        conservative = usage_audit.get("equivalent_conservative") or {}
        if not isinstance(conservative, dict) or (
            conservative.get("status") != "accepted"
        ):
            raise ValueError(
                "conservative equivalent-cost receipt is not accepted"
            )
        if "accounted_cost_usd" not in conservative:
            raise ValueError(
                "conservative equivalent-cost receipt lacks a numeric debit"
            )
        accounted_cost = _finite_number(
            conservative["accounted_cost_usd"], "conservative debit",
        )
        if accounted_cost < recorded_cost:
            raise ValueError(
                "conservative equivalent-cost debit undercuts recorded receipts"
            )
        if (
            not conservative.get("source")
            or not conservative.get("method")
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(conservative.get("evidence_sha256") or ""),
            )
        ):
            raise ValueError(
                "conservative equivalent-cost receipt lacks durable evidence"
            )
        cost_evidence = conservative
    else:
        raise ValueError(
            "terminal cost is unresolved; require complete, reconciled, or "
            "equivalent_conservative accounting"
        )
    launch = usage_audit.get("launch")
    if not isinstance(launch, dict):
        raise ValueError("usage audit launch envelope is malformed")
    segments = launch.get("segments")
    elapsed = 0.0
    if not isinstance(segments, list) or not segments:
        raise ValueError("usage audit lacks launch timing segments")
    for segment in segments:
        try:
            started = datetime.fromisoformat(
                str(segment["started_at"]).replace("Z", "+00:00")
            )
            ended = datetime.fromisoformat(
                str(segment["ended_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "usage audit has an incomplete launch timing segment"
            ) from exc
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        duration = (ended - started).total_seconds()
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("usage audit launch timing is negative")
        elapsed += duration
    elapsed = _finite_number(elapsed, "usage audit wall time")
    harness_elapsed = _finite_number(
        result.get("duration_seconds", 0), "harness duration",
    )
    if harness_elapsed > elapsed + 2.0:
        raise ValueError("harness duration exceeds launcher wall-clock receipt")
    if launch.get("status") != "sealed":
        raise ValueError("usage audit launch envelope is not sealed")
    # This per-leg comparison is an early sanity bound. The authoritative
    # multi-leg campaign ceiling is enforced cumulatively by
    # advance_campaign_state.
    if accounted_cost > max_cost_usd or elapsed > max_wall_seconds:
        raise ValueError("pre-registered campaign budget exceeded")
    validation = {
        "schema_version": 1,
        "kind": "trusted_core_terminal_validation",
        "okay": True,
        "decision": decision,
        "lineage_id": context["lineage_id"],
        "promotion_receipt_id": promotion["receipt_id"],
        "tree_hash": tree_hash,
        "usage_audit_receipt_id": provenance.receipt_id(usage_audit),
        "cost_status": cost_status,
        "recorded_cost_usd": recorded_cost,
        "accounted_cost_usd": accounted_cost,
        "elapsed_seconds": elapsed,
    }
    if cost_evidence is not None:
        validation["cost_evidence"] = cost_evidence
    validation["receipt_id"] = provenance.receipt_id(validation)
    return validation


def advance_campaign_state(
    *,
    prior: dict[str, Any],
    terminal: dict[str, Any],
    vector: dict[str, int],
    plateau_k: int,
    max_cost_usd: float,
    max_wall_seconds: float,
) -> dict[str, Any]:
    """Append one leg and enforce Pareto progress, budget, and plateau stops."""
    max_cost_usd = _finite_number(
        max_cost_usd, "maximum cost", positive=True,
    )
    max_wall_seconds = _finite_number(
        max_wall_seconds, "maximum wall time", positive=True,
    )
    if not isinstance(plateau_k, int) or isinstance(plateau_k, bool) or plateau_k <= 0:
        raise ValueError("plateau_k must be a positive integer")
    if prior:
        if prior.get("receipt_id") != provenance.receipt_id(prior):
            raise ValueError("prior campaign state content ID mismatch")
        if prior.get("kind") != "trusted_core_campaign_state":
            raise ValueError("prior campaign state has the wrong kind")
        if prior.get("stop") is True:
            raise ValueError("prior campaign state already requires a stop")
        if prior.get("lineage_id") != terminal.get("lineage_id"):
            raise ValueError("prior campaign state belongs to another lineage")
    required = ("hard_admits", "verification_errors", "resource_limits", "raw_errors")
    if any(
        not isinstance(vector.get(key), int)
        or isinstance(vector[key], bool)
        or vector[key] < 0
        for key in required
    ):
        raise ValueError("leg lacks the full canonical non-negative progress vector")
    legs = list(prior.get("legs") or [])
    previous = (legs[-1].get("vector") if legs else None)
    improved = previous is None or (
        all(vector[key] <= previous[key] for key in required)
        and any(vector[key] < previous[key] for key in required)
    )
    no_improvement = 0 if improved else int(
        prior.get("consecutive_no_improvement") or 0
    ) + 1
    terminal_cost = terminal.get(
        "accounted_cost_usd", terminal.get("recorded_cost_usd", 0),
    )
    cumulative_cost = _finite_number(
        _finite_number(
            prior.get("recorded_cost_usd", 0), "prior recorded cost",
        ) + _finite_number(terminal_cost, "terminal accounted cost"),
        "cumulative recorded cost",
    )
    cumulative_wall = _finite_number(
        _finite_number(
            prior.get("elapsed_seconds", 0), "prior elapsed time",
        ) + _finite_number(
            terminal.get("elapsed_seconds", 0), "terminal elapsed time",
        ),
        "cumulative elapsed time",
    )
    cost_unresolved = terminal.get("cost_status") not in {
        "complete", "reconciled", "equivalent_conservative",
    }
    stop_reasons = []
    if cumulative_cost >= max_cost_usd:
        stop_reasons.append("COST_CEILING")
    if cumulative_wall >= max_wall_seconds:
        stop_reasons.append("WALL_CEILING")
    if no_improvement >= plateau_k:
        stop_reasons.append("PLATEAU")
    if cost_unresolved:
        stop_reasons.append("COST_UNRESOLVED")
    state = {
        "schema_version": 1,
        "kind": "trusted_core_campaign_state",
        "lineage_id": terminal["lineage_id"],
        "legs": [
            *legs,
            {
                "terminal_receipt_id": terminal["receipt_id"],
                "decision": terminal["decision"],
                "vector": dict(vector),
                "improved": improved,
            },
        ],
        "consecutive_no_improvement": no_improvement,
        "recorded_cost_usd": cumulative_cost,
        "elapsed_seconds": cumulative_wall,
        "stop": bool(stop_reasons) or terminal["decision"] == "ACCEPTED",
        "stop_reasons": stop_reasons,
    }
    state["receipt_id"] = provenance.receipt_id(state)
    return state


def _terminal_vector(result: dict[str, Any]) -> dict[str, int]:
    promotion = result.get("promotion_receipt") or {}
    decision = promotion.get("decision")
    gate_key = (
        "acceptance_gate_receipt"
        if decision == "ACCEPTED"
        else "banking_gate_receipt"
    )
    vector = (promotion.get(gate_key) or {}).get("vector")
    if not isinstance(vector, dict):
        raise ValueError("terminal promotion lacks a canonical gate vector")
    return vector


def _write_exclusive(path: Path, value: dict[str, Any]) -> None:
    provenance.write_immutable_json(path, value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("prepare-start")
    for name in ("campaign", "manifest", "peel-json", "project", "repo-root",
                 "out"):
        start.add_argument(f"--{name}", type=Path, required=True)
    context = sub.add_parser("prepare-context")
    for name in ("campaign", "manifest", "start", "project", "repo-root",
                 "registration", "sterility", "out"):
        context.add_argument(f"--{name}", type=Path, required=True)
    context.add_argument("--predecessor", type=Path)
    context.add_argument("--predecessor-terminal", type=Path)
    context.add_argument("--campaign-state", type=Path)
    terminal = sub.add_parser("validate-terminal")
    for name in ("context", "result", "usage-audit", "project", "out"):
        terminal.add_argument(f"--{name}", type=Path, required=True)
    terminal.add_argument("--max-cost-usd", type=float, required=True)
    terminal.add_argument("--max-wall-seconds", type=float, required=True)
    advance = sub.add_parser("advance-state")
    for name in ("terminal", "result", "out"):
        advance.add_argument(f"--{name}", type=Path, required=True)
    advance.add_argument("--prior", type=Path)
    advance.add_argument("--plateau-k", type=int, required=True)
    advance.add_argument("--max-cost-usd", type=float, required=True)
    advance.add_argument("--max-wall-seconds", type=float, required=True)
    harness = sub.add_parser("harness-receipt")
    harness.add_argument("--repo-root", type=Path, required=True)
    replay = sub.add_parser("validate-root-replay")
    for name in ("authority", "replay", "out"):
        replay.add_argument(f"--{name}", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare-start":
            start = docker_start_envelope(
                peel_json_path=args.peel_json,
                manifest_path=args.manifest,
                campaign_path=args.campaign,
                project=args.project,
                repo_root=args.repo_root,
            )
            _write_exclusive(args.out, start)
            print(json.dumps(start, sort_keys=True))
        elif args.command == "prepare-context":
            frontier_sidecar = (
                args.out.with_name(f"{args.out.name}.frontier.json")
                if args.predecessor is not None else None
            )
            context = build_lineage_context(
                campaign_path=args.campaign,
                start_envelope_path=args.start,
                registration_path=args.registration,
                sterility_path=args.sterility,
                manifest_path=args.manifest,
                project=args.project,
                repo_root=args.repo_root,
                predecessor_path=args.predecessor,
                predecessor_terminal_path=args.predecessor_terminal,
                campaign_state_path=args.campaign_state,
                frontier_sidecar_path=frontier_sidecar,
            )
            _write_exclusive(args.out, context)
            print(json.dumps(context, sort_keys=True))
        elif args.command == "validate-terminal":
            context = _json(args.context)
            result = _json(args.result)
            audit = _json(args.usage_audit)
            validation = validate_terminal(
                context=context,
                result=result,
                usage_audit=audit,
                project=args.project,
                max_cost_usd=args.max_cost_usd,
                max_wall_seconds=args.max_wall_seconds,
            )
            _write_exclusive(args.out, validation)
            print(json.dumps(validation, sort_keys=True))
        elif args.command == "advance-state":
            terminal = _json(args.terminal)
            result = _json(args.result)
            prior = _json(args.prior) if args.prior else {}
            state = advance_campaign_state(
                prior=prior,
                terminal=terminal,
                vector=_terminal_vector(result),
                plateau_k=args.plateau_k,
                max_cost_usd=args.max_cost_usd,
                max_wall_seconds=args.max_wall_seconds,
            )
            _write_exclusive(args.out, state)
            print(json.dumps(state, sort_keys=True))
        elif args.command == "harness-receipt":
            print(json.dumps(harness_source_receipt(args.repo_root), sort_keys=True))
        else:
            validation = validate_root_replay(
                _json(args.authority), _json(args.replay),
            )
            _write_exclusive(args.out, validation)
            print(json.dumps(validation, sort_keys=True))
    except ValueError as exc:
        print(f"trusted-core profile error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
