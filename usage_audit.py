#!/usr/bin/env python3
"""Build an honest cost receipt for one launcher run.

Provider terminal events are the only authoritative local dollar receipts.
Interrupted streams can still contain useful token telemetry, but their dollar
cost remains unresolved until a provider-side reconciliation is supplied.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


RAW_RE = re.compile(r"^round_(\d+)\.jsonl$")
ROUND_RE = re.compile(r"^round_(\d+)\.json$")
LIFECYCLE_RE = re.compile(r"^round_(\d+)\.lifecycle\.json$")
TERMINAL_TYPES = {"result", "turn.completed", "turn.failed"}


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "top-level JSON value is not an object"
    return data, None


def _round_cost(path: Path | None) -> tuple[Decimal | None, list[str]]:
    if path is None:
        return None, []
    data, error = _load_json(path)
    if error or data is None:
        return None, [f"round_record_parse_error: {error}"]
    usage = data.get("claude_usage")
    if not isinstance(usage, dict):
        return None, []
    value = _decimal(usage.get("total_cost_usd"))
    if value is None:
        return None, []
    if usage.get("cost_reported") is False:
        return None, ["round_record_numeric_cost_marked_unreported"]
    return value, []


def _raw_receipt(path: Path | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": str(path) if path else None,
        "jsonl_lines": 0,
        "parse_errors": 0,
        "terminal_events": 0,
        "provider_cost_events": 0,
        "provider_costs": [],
    }
    if path is None:
        return out
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        out["parse_errors"] = 1
        return out
    out["jsonl_lines"] = len(lines)
    costs: list[Decimal] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            out["parse_errors"] += 1
            continue
        if not isinstance(event, dict) or event.get("type") not in TERMINAL_TYPES:
            continue
        out["terminal_events"] += 1
        if event.get("type") == "result":
            cost = _decimal(event.get("total_cost_usd"))
            if cost is not None:
                costs.append(cost)
    out["provider_cost_events"] = len(costs)
    out["provider_costs"] = [str(cost) for cost in costs]
    return out


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def archive_task_receipts(
    results_root: Path,
    run_id: str,
    task_id: str,
    launch_instance_id: str,
) -> Path | None:
    """Move an existing task directory into append-only usage history.

    Resume launches reuse the same run/task path while `run.py` restarts round
    numbering at one. Archiving before relaunch prevents already-billed raw
    streams from being overwritten by the next attempt.
    """
    run_dir = results_root / run_id
    task_dir = run_dir / task_id
    if not task_dir.is_dir():
        return None
    archive_root = run_dir / "_usage_history" / launch_instance_id
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / task_id
    suffix = 2
    while destination.exists():
        destination = archive_root / f"{task_id}.{suffix}"
        suffix += 1
    os.replace(task_dir, destination)
    _atomic_json(destination / "_usage_archive.json", {
        "schema_version": 1,
        "logical_task_id": task_id,
        "launch_instance_id": launch_instance_id,
        "archived_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "original_path": str(task_dir),
    })
    return destination


def _stream_record(
    task_id: str,
    round_number: int,
    raw_path: Path | None,
    round_path: Path | None,
    lifecycle_path: Path | None,
    require_lifecycle: bool,
    source: str,
) -> dict[str, Any]:
    raw = _raw_receipt(raw_path)
    round_cost, reasons = _round_cost(round_path)
    lifecycle = None
    if lifecycle_path is not None:
        lifecycle, lifecycle_error = _load_json(lifecycle_path)
        if lifecycle_error:
            reasons.append(f"lifecycle_parse_error: {lifecycle_error}")

    terminal_costs = [_decimal(value) for value in raw["provider_costs"]]
    numeric_terminal_costs = [value for value in terminal_costs if value is not None]
    recorded_cost = max(numeric_terminal_costs) if numeric_terminal_costs else None
    if recorded_cost is None:
        recorded_cost = round_cost
    elif round_cost is not None and round_cost != recorded_cost:
        reasons.append(
            "raw_round_cost_mismatch: "
            f"raw_max={recorded_cost} round_record={round_cost}"
        )
        recorded_cost = max(recorded_cost, round_cost)

    if raw_path is None:
        reasons.append("round_or_lifecycle_without_raw_stream")
    else:
        if raw["parse_errors"]:
            reasons.append(f"raw_parse_errors={raw['parse_errors']}")
        if raw["terminal_events"] == 0:
            reasons.append("raw_stream_without_terminal_event")
        if raw["terminal_events"] > 1:
            reasons.append(
                f"multiple_terminal_events={raw['terminal_events']}"
            )
        if raw["provider_cost_events"] != raw["terminal_events"]:
            reasons.append(
                "terminal_events_without_provider_cost="
                f"{raw['terminal_events'] - raw['provider_cost_events']}"
            )
    if round_path is None:
        # A raw terminal receipt still accounts for provider cost exactly; the
        # missing harness record remains visible but is not itself a cost gap.
        harness_gap = "raw_stream_without_round_record"
    else:
        harness_gap = None
    if require_lifecycle and lifecycle_path is None:
        reasons.append("missing_required_lifecycle_receipt")
    if lifecycle is not None and lifecycle.get("status") != "agent_exited":
        reasons.append(
            f"lifecycle_not_exited={lifecycle.get('status', 'unknown')}"
        )

    return {
        "task_id": task_id,
        "round_number": round_number,
        "source": source,
        "raw_path": str(raw_path) if raw_path else None,
        "round_record_path": str(round_path) if round_path else None,
        "lifecycle_path": str(lifecycle_path) if lifecycle_path else None,
        "lifecycle_status": lifecycle.get("status") if lifecycle else None,
        "terminal_events": raw["terminal_events"],
        "provider_cost_events": raw["provider_cost_events"],
        "raw_parse_errors": raw["parse_errors"],
        "recorded_cost_usd": float(recorded_cost) if recorded_cost is not None else 0.0,
        "recorded_cost_usd_decimal": (
            str(recorded_cost) if recorded_cost is not None else "0"
        ),
        "cost_resolved": not reasons,
        "unresolved_reasons": sorted(set(reasons)),
        "harness_gap": harness_gap,
    }


def audit_run(
    results_root: Path,
    run_id: str,
    *,
    started_at: str,
    ended_at: str | None = None,
    launcher_rc: int | None = None,
    project: str | None = None,
    launch_instance_id: str | None = None,
    reconciled_cost_usd: Decimal | None = None,
    reconciliation_source: str | None = None,
) -> dict[str, Any]:
    run_dir = results_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    prior_audit_path = run_dir / "usage_audit.json"
    prior_audit, prior_audit_error = _load_json(prior_audit_path)
    if prior_audit_error and prior_audit_path.exists():
        raise ValueError(f"cannot read existing usage audit: {prior_audit_error}")
    envelope_path = run_dir / "launch_envelope.json"
    prior_envelope, envelope_error = _load_json(envelope_path)
    if envelope_error and envelope_path.exists():
        raise ValueError(f"cannot read existing launch envelope: {envelope_error}")
    launch_instance_id = launch_instance_id or f"legacy.{started_at}"
    require_lifecycle = True
    segments = list((prior_envelope or {}).get("segments") or [])
    if prior_envelope and not segments and prior_envelope.get("started_at"):
        segments.append({
            "launch_instance_id": (
                prior_envelope.get("launch_instance_id")
                or f"legacy.{prior_envelope['started_at']}"
            ),
            "launcher_pid": prior_envelope.get("launcher_pid"),
            "started_at": prior_envelope.get("started_at"),
            "ended_at": prior_envelope.get("ended_at"),
            "launcher_rc": prior_envelope.get("launcher_rc"),
            "status": prior_envelope.get("status"),
        })
    segment = next(
        (item for item in segments
         if item.get("launch_instance_id") == launch_instance_id),
        None,
    )
    if segment is None:
        segment = {
            "launch_instance_id": launch_instance_id,
            "launcher_pid": os.getppid(),
            "started_at": started_at,
            "ended_at": None,
            "launcher_rc": None,
            "status": "running",
        }
        segments.append(segment)
    elif segment.get("started_at") != started_at:
        raise ValueError(
            "launch start mismatch for instance "
            f"{launch_instance_id}: {segment.get('started_at')!r} != "
            f"{started_at!r}"
        )
    segment.update({
        "ended_at": ended_at,
        "launcher_rc": launcher_rc,
        "status": "sealed" if ended_at is not None else "running",
    })
    envelope = {
        "schema_version": 2,
        "run_id": run_id,
        "results_root": str(results_root),
        "project": project or (prior_envelope or {}).get("project"),
        "status": (
            "running" if any(item.get("status") == "running" for item in segments)
            else "sealed"
        ),
        "require_lifecycle_receipts": require_lifecycle,
        "segments": segments,
    }
    _atomic_json(envelope_path, envelope)

    streams: list[dict[str, Any]] = []
    task_sources: list[tuple[str, Path, str]] = [
        (path.name, path, "live")
        for path in sorted(run_dir.iterdir())
        if path.is_dir() and path.name != "_usage_history"
    ]
    history_root = run_dir / "_usage_history"
    if history_root.is_dir():
        for archived_task in sorted(history_root.glob("*/*")):
            if not archived_task.is_dir():
                continue
            archive_meta, _ = _load_json(
                archived_task / "_usage_archive.json")
            logical_task_id = (
                archive_meta.get("logical_task_id")
                if archive_meta else archived_task.name.split(".", 1)[0]
            )
            task_sources.append((
                str(logical_task_id),
                archived_task,
                f"archive:{archived_task.parent.name}/{archived_task.name}",
            ))

    for logical_task_id, task_dir, source in task_sources:
        raw_by_round: dict[int, Path] = {}
        lifecycle_by_round: dict[int, Path] = {}
        raw_dir = task_dir / "claude_raw"
        if raw_dir.is_dir():
            for path in raw_dir.iterdir():
                raw_match = RAW_RE.match(path.name)
                if raw_match:
                    raw_by_round[int(raw_match.group(1))] = path
                    continue
                lifecycle_match = LIFECYCLE_RE.match(path.name)
                if lifecycle_match:
                    lifecycle_by_round[int(lifecycle_match.group(1))] = path
        round_by_number = {
            int(match.group(1)): path
            for path in task_dir.iterdir()
            if path.is_file() and (match := ROUND_RE.match(path.name))
        }
        numbers = sorted(
            set(raw_by_round) | set(round_by_number) | set(lifecycle_by_round)
        )
        task_streams = [
            _stream_record(
                logical_task_id,
                number,
                raw_by_round.get(number),
                round_by_number.get(number),
                lifecycle_by_round.get(number),
                require_lifecycle,
                source,
            )
            for number in numbers
        ]
        streams.extend(task_streams)

    task_summaries: list[dict[str, Any]] = []
    for task_id in sorted({item["task_id"] for item in streams}):
        task_streams = [
            item for item in streams if item["task_id"] == task_id
        ]
        task_total = sum(
            (Decimal(item["recorded_cost_usd_decimal"]) for item in task_streams),
            Decimal("0"),
        )
        task_unresolved = sum(not item["cost_resolved"] for item in task_streams)
        task_summaries.append({
            "task_id": task_id,
            "stream_attempts": len(task_streams),
            "recorded_cost_usd": float(task_total),
            "recorded_cost_usd_decimal": str(task_total),
            "unresolved_streams": task_unresolved,
            "cost_status": (
                "complete" if task_unresolved == 0
                else "lower_bound" if task_total > 0
                else "unknown"
            ),
        })

    recorded_total = sum(
        (Decimal(item["recorded_cost_usd_decimal"]) for item in streams),
        Decimal("0"),
    )
    unresolved = sum(not item["cost_resolved"] for item in streams)
    launch_running = envelope["status"] == "running"
    if not streams:
        cost_status = "unknown" if launch_running else "not_run"
    elif unresolved or launch_running:
        cost_status = "lower_bound" if recorded_total > 0 else "unknown"
    else:
        cost_status = "complete"

    reconciliation_basis = {
        "recorded_cost_usd_decimal": str(recorded_total),
        "stream_attempts": len(streams),
        "launch_segments": len(segments),
    }
    reconciliation: dict[str, Any] = {
        "status": "not_provided",
        "source": None,
        "reconciled_cost_usd": None,
        "gap_usd": None,
        "basis": None,
    }
    if reconciled_cost_usd is not None:
        if not reconciliation_source:
            raise ValueError("reconciliation_source is required with reconciled cost")
        gap = reconciled_cost_usd - recorded_total
        reconciliation = {
            "status": "conflict" if gap < 0 else "accepted",
            "source": reconciliation_source,
            "reconciled_cost_usd": float(reconciled_cost_usd),
            "reconciled_cost_usd_decimal": str(reconciled_cost_usd),
            "gap_usd": float(gap),
            "gap_usd_decimal": str(gap),
            "basis": reconciliation_basis,
        }
        cost_status = "conflict" if gap < 0 else "reconciled"
    else:
        prior_reconciliation = (
            prior_audit.get("reconciliation")
            if isinstance(prior_audit, dict) else None
        )
        if (
            isinstance(prior_reconciliation, dict)
            and prior_reconciliation.get("status") in {"accepted", "conflict"}
        ):
            if (
                prior_reconciliation.get("basis") == reconciliation_basis
                and not launch_running
            ):
                reconciliation = prior_reconciliation
                cost_status = (
                    "conflict"
                    if reconciliation.get("status") == "conflict"
                    else "reconciled"
                )
            else:
                reconciliation = {
                    "status": "stale_after_new_evidence",
                    "source": None,
                    "reconciled_cost_usd": None,
                    "gap_usd": None,
                    "basis": reconciliation_basis,
                    "previous": prior_reconciliation,
                }

    counts = {
        "tasks": len(task_summaries),
        "stream_attempts": len(streams),
        "raw_streams": sum(item["raw_path"] is not None for item in streams),
        "lifecycle_receipts": sum(
            item["lifecycle_path"] is not None for item in streams
        ),
        "harness_round_records": sum(
            item["round_record_path"] is not None for item in streams
        ),
        "terminal_events": sum(item["terminal_events"] for item in streams),
        "provider_cost_events": sum(
            item["provider_cost_events"] for item in streams
        ),
        "unresolved_streams": unresolved,
        "raw_without_round_record": sum(
            item["harness_gap"] == "raw_stream_without_round_record"
            for item in streams
        ),
        "round_or_lifecycle_without_raw": sum(
            item["raw_path"] is None for item in streams
        ),
    }
    return {
        "schema_version": 2,
        "run_id": run_id,
        "results_root": str(results_root),
        "audit_path": str(run_dir / "usage_audit.json"),
        "launch": envelope,
        "cost_status": cost_status,
        "recorded_cost_usd": float(recorded_total),
        "recorded_cost_usd_decimal": str(recorded_total),
        "cost_semantics": (
            "Numeric provider-reported cost receipts only, read directly from "
            "raw terminal events or preserved in harness round records. This "
            "is a lower bound whenever cost_status is lower_bound or unknown; "
            "no local token repricing is included."
        ),
        "counts": counts,
        "tasks": task_summaries,
        "streams": streams,
        "reconciliation": reconciliation,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--ended-at")
    parser.add_argument("--launcher-rc", type=int)
    parser.add_argument("--project")
    parser.add_argument("--launch-instance-id")
    parser.add_argument("--archive-task-id")
    parser.add_argument("--reconciled-cost-usd")
    parser.add_argument("--reconciliation-source")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    reconciled = (
        _decimal(args.reconciled_cost_usd)
        if args.reconciled_cost_usd is not None else None
    )
    if args.reconciled_cost_usd is not None and reconciled is None:
        raise SystemExit("--reconciled-cost-usd must be a non-negative number")
    archived_task = None
    try:
        if args.archive_task_id:
            if not args.launch_instance_id:
                raise ValueError(
                    "--launch-instance-id is required with --archive-task-id")
            archived_task = archive_task_receipts(
                args.results_root,
                args.run_id,
                args.archive_task_id,
                args.launch_instance_id,
            )
        audit = audit_run(
            args.results_root,
            args.run_id,
            started_at=args.started_at,
            ended_at=args.ended_at,
            launcher_rc=args.launcher_rc,
            project=args.project,
            launch_instance_id=args.launch_instance_id,
            reconciled_cost_usd=reconciled,
            reconciliation_source=args.reconciliation_source,
        )
    except ValueError as exc:
        print(f"usage audit error: {exc}", file=sys.stderr)
        return 2
    out = Path(audit["audit_path"])
    _atomic_json(out, audit)
    print(json.dumps({
        "audit_path": str(out),
        "cost_status": audit["cost_status"],
        "recorded_cost_usd": audit["recorded_cost_usd_decimal"],
        "stream_attempts": audit["counts"]["stream_attempts"],
        "unresolved_streams": audit["counts"]["unresolved_streams"],
        "tasks": audit["tasks"],
        "archived_task_path": str(archived_task) if archived_task else None,
    }, separators=(",", ":")))
    return 2 if audit["cost_status"] == "conflict" else 0


if __name__ == "__main__":
    raise SystemExit(main())
