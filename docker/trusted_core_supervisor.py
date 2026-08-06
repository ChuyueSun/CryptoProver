#!/usr/bin/env python3
"""Durable, fail-closed supervisor for Trust Core campaign legs.

The supervisor does not manufacture proof authority.  It launches an exact
registered package, classifies only sealed runner/usage receipts, and either
waits, installs a mechanically generated successor package, or stops.
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from lib import provenance, taxonomy  # noqa: E402

# Routing sets come from the single shared taxonomy (lib/taxonomy.py) — the
# hand-copied versions of these two sets drifted from the runner's emitted
# labels (F3, 2026-08-03) and are not allowed back.
RETRYABLE_TRANSPORT = set(taxonomy.AUTORETRY_END_REASONS)
STOP_REASONS = set(taxonomy.STOP_END_REASONS)


class SupervisorError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SupervisorError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SupervisorError(f"JSON is not an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ledger(path: Path, **event: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _utc_now(), **event}
    with path.open("a", encoding="utf-8") as target:
        target.write(json.dumps(record, sort_keys=True) + "\n")
        target.flush()
        os.fsync(target.fileno())


def _format_parts(parts: list[str], values: dict[str, str]) -> list[str]:
    try:
        return [part.format_map(values) for part in parts]
    except KeyError as exc:
        raise SupervisorError(f"unknown command placeholder: {exc.args[0]}") from exc


def _validate_package(path: Path) -> dict[str, Any]:
    package = _json(path)
    if package.get("schema_version") != 1:
        raise SupervisorError("unsupported supervisor package schema")
    for key in ("package_id", "run_id_prefix", "prelaunch_argv", "launch_argv",
                "result_path", "usage_audit_path", "immutable_inputs"):
        if not package.get(key):
            raise SupervisorError(f"supervisor package lacks {key}")
    for command_name in ("prelaunch_argv", "launch_argv"):
        if not isinstance(package[command_name], list) or not all(
            isinstance(item, str) and item for item in package[command_name]
        ):
            raise SupervisorError(f"{command_name} must be a nonempty string list")
    inputs = package["immutable_inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise SupervisorError("immutable_inputs must be a nonempty list")
    for entry in inputs:
        if not isinstance(entry, dict):
            raise SupervisorError("invalid immutable input entry")
        source = Path(str(entry.get("path") or ""))
        expected = entry.get("sha256")
        if not source.is_file() or _sha256(source) != expected:
            raise SupervisorError(f"immutable input mismatch: {source}")
    expected_package_id = provenance.supervisor_package_id(package)
    if package.get("package_id") != expected_package_id:
        raise SupervisorError(
            "supervisor package_id does not bind the package contents"
        )
    return package


def _find_reset_epoch(paths: list[Path]) -> int | None:
    candidates: list[int] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"resetsAt", "reset_at", "resetEpoch", "reset_epoch"}:
                    try:
                        candidates.append(int(child))
                    except (TypeError, ValueError):
                        pass
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for path in paths:
        try:
            with path.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Assistant/user envelopes contain model-owned tool input
                    # and tool results. They are not provider reset authority:
                    # recursively accepting a `reset_epoch` key there lets the
                    # proof agent choose the supervisor's next wake time.
                    if not isinstance(event, dict) or event.get("type") not in {
                        "assistant", "user",
                    }:
                        visit(event)
        except OSError:
            continue
    return max(candidates) if candidates else None


def _finite_nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise SupervisorError(f"{label} is not a finite non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SupervisorError(
            f"{label} is not a finite non-negative number"
        ) from exc
    if not math.isfinite(number) or number < 0:
        raise SupervisorError(f"{label} is not a finite non-negative number")
    return number


def _registered_budget(package: dict[str, Any]) -> tuple[float, float]:
    """Read the immutable launch registration's cumulative campaign limits."""
    argv = package["launch_argv"]
    try:
        registration_path = Path(argv[argv.index("--launch-registration") + 1])
    except (ValueError, IndexError) as exc:
        raise SupervisorError(
            "supervisor launch lacks --launch-registration"
        ) from exc
    immutable = {
        Path(str(entry.get("path") or "")).resolve()
        for entry in package["immutable_inputs"]
        if isinstance(entry, dict)
    }
    if registration_path.resolve() not in immutable:
        raise SupervisorError("launch registration is not an immutable package input")
    registration = _json(registration_path)
    budget = registration.get("budget") or {}
    max_cost = _finite_nonnegative(
        budget.get("max_cost_usd"), "registered maximum cost",
    )
    max_wall = _finite_nonnegative(
        budget.get("max_wall_seconds"), "registered maximum wall time",
    )
    if max_cost <= 0 or max_wall <= 0:
        raise SupervisorError("registered campaign limits must be positive")
    return max_cost, max_wall


def _usage_debit(audit: dict[str, Any]) -> tuple[float, float]:
    """Return the conservative cost and active launch time for one attempt."""
    status = audit.get("cost_status")
    cost = _finite_nonnegative(
        audit.get("recorded_cost_usd") or 0, "usage recorded cost",
    )
    if status == "reconciled":
        reconciliation = audit.get("reconciliation") or {}
        if reconciliation.get("status") != "accepted":
            raise SupervisorError("usage reconciliation is not accepted")
        cost = _finite_nonnegative(
            reconciliation.get("reconciled_cost_usd"), "reconciled cost",
        )
    elif status == "equivalent_conservative":
        conservative = audit.get("equivalent_conservative") or {}
        if conservative.get("status") != "accepted":
            raise SupervisorError("conservative usage debit is not accepted")
        cost = _finite_nonnegative(
            conservative.get("accounted_cost_usd"), "conservative cost",
        )
    segments = (audit.get("launch") or {}).get("segments") or []
    if not segments:
        raise SupervisorError("usage audit lacks launch timing segments")
    elapsed = 0.0
    for segment in segments:
        try:
            started = datetime.fromisoformat(
                str(segment["started_at"]).replace("Z", "+00:00")
            )
            ended = datetime.fromisoformat(
                str(segment["ended_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SupervisorError("usage audit timing segment is invalid") from exc
        duration = (ended - started).total_seconds()
        if not math.isfinite(duration) or duration < 0:
            raise SupervisorError("usage audit timing segment is negative")
        elapsed += duration
    return cost, elapsed


def _classify(
    package: dict[str, Any], values: dict[str, str], returncode: int,
) -> tuple[str, dict[str, Any]]:
    result_path = Path(package["result_path"].format_map(values))
    audit_path = Path(package["usage_audit_path"].format_map(values))
    if not result_path.is_file():
        raise SupervisorError(f"launch produced no result: {result_path}")
    result = _json(result_path)
    if not audit_path.is_file():
        raise SupervisorError(f"launch result lacks usage audit: {audit_path}")
    audit = _json(audit_path)
    attempt_cost, attempt_wall = _usage_debit(audit)
    end_reason = result.get("end_reason")
    if not isinstance(end_reason, str) or not end_reason:
        raise SupervisorError("result lacks end_reason")
    promotion = result.get("promotion_receipt") or {}
    decision = promotion.get("decision")
    disposition = promotion.get("terminal_disposition") or {}

    if end_reason == "PREMODEL_GATE_INDETERMINATE":
        if not audit_path.is_file():
            raise SupervisorError(
                "PREMODEL_GATE_INDETERMINATE result lacks usage audit"
            )
        counts = audit.get("counts") or {}
        if (
            audit.get("cost_status") != "not_run"
            or float(audit.get("recorded_cost_usd") or 0) != 0
            or counts.get("stream_attempts") != 0
            or counts.get("provider_cost_events") != 0
            or counts.get("unresolved_streams") != 0
        ):
            raise SupervisorError(
                "PREMODEL_GATE_INDETERMINATE is not exact zero-provider accounting"
            )
    elif end_reason in RETRYABLE_TRANSPORT:
        if not audit_path.is_file():
            raise SupervisorError(f"{end_reason} result lacks usage audit")
        if audit.get("cost_status") != "complete" or (
            audit.get("counts") or {}
        ).get("unresolved_streams") != 0:
            raise SupervisorError(f"{end_reason} accounting is not complete")
    if end_reason == "RATE_LIMITED":
        if returncode not in {0, 42}:
            raise SupervisorError(
                f"RATE_LIMITED launcher return code is not authoritative: {returncode}"
            )
        return "retry", {
            "end_reason": end_reason, "decision": decision,
            "attempt_cost_usd": attempt_cost,
            "attempt_wall_seconds": attempt_wall,
        }

    if returncode != 0:
        raise SupervisorError(
            f"launcher exited {returncode} before authoritative terminal classification"
        )

    if end_reason in RETRYABLE_TRANSPORT:
        return "retry", {
            "end_reason": end_reason, "decision": decision,
            "attempt_cost_usd": attempt_cost,
            "attempt_wall_seconds": attempt_wall,
        }
    if decision == "ACCEPTED" or end_reason == "COMPLETE":
        if decision != "ACCEPTED" or disposition.get("reusable") is not True:
            raise SupervisorError("COMPLETE lacks reusable ACCEPTED promotion")
        return "complete", {
            "end_reason": end_reason, "decision": decision,
            "attempt_cost_usd": attempt_cost,
            "attempt_wall_seconds": attempt_wall,
        }
    if decision == "BANKED_PARTIAL":
        if disposition.get("reusable") is not True:
            raise SupervisorError("BANKED_PARTIAL is not reusable")
        campaign_path = package.get("campaign_state_path")
        if not campaign_path:
            raise SupervisorError("banked package lacks campaign_state_path")
        campaign = _json(Path(str(campaign_path).format_map(values)))
        if campaign.get("stop") is True:
            return "stop", {
                "end_reason": end_reason, "decision": decision,
                "stop_reasons": campaign.get("stop_reasons") or [],
                "attempt_cost_usd": attempt_cost,
                "attempt_wall_seconds": attempt_wall,
            }
        return "advance", {
            "end_reason": end_reason, "decision": decision,
            "attempt_cost_usd": attempt_cost,
            "attempt_wall_seconds": attempt_wall,
        }
    if end_reason in STOP_REASONS:
        return "stop", {
            "end_reason": end_reason, "decision": decision,
            "attempt_cost_usd": attempt_cost,
            "attempt_wall_seconds": attempt_wall,
        }
    return "stop", {
        "end_reason": end_reason, "decision": decision,
        "stop_reasons": ["NONREUSABLE_OR_UNKNOWN_TERMINAL"],
        "attempt_cost_usd": attempt_cost,
        "attempt_wall_seconds": attempt_wall,
    }


def _next_package(
    package: dict[str, Any], values: dict[str, str], current_path: Path,
) -> Path:
    command = package.get("next_package_argv")
    if not isinstance(command, list) or not command:
        raise SupervisorError("BANKED_PARTIAL lacks next_package_argv")
    next_path = current_path.parent / f"package-after-{values['run_id']}.json"
    hook_values = {**values, "current_package": str(current_path),
                   "next_package": str(next_path)}
    completed = subprocess.run(_format_parts(command, hook_values), check=False)
    if completed.returncode != 0:
        raise SupervisorError(f"next-package generator exited {completed.returncode}")
    _validate_package(next_path)
    return next_path


def run(args: argparse.Namespace) -> int:
    state_path = args.state.resolve()
    ledger_path = args.ledger.resolve()
    lock_path = args.lock.resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SupervisorError("another supervisor holds the lock") from exc

        state = _json(state_path) if state_path.exists() else {
            "schema_version": 1, "attempt": 0,
            "package_path": str(args.package.resolve()), "status": "starting",
        }
        while True:
            if state.get("status") in {"stop", "complete", "error"}:
                raise SupervisorError(
                    f"persisted terminal supervisor state requires human reset: {state['status']}"
                )
            if state.get("status") == "waiting":
                try:
                    prior_wake = float(state["next_attempt_epoch"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise SupervisorError("waiting state lacks next_attempt_epoch") from exc
                if time.time() < prior_wake:
                    if args.once:
                        return 0
                    while time.time() < prior_wake:
                        time.sleep(min(60.0, prior_wake - time.time()))
            package_path = Path(state["package_path"])
            try:
                package = _validate_package(package_path)
            except SupervisorError as exc:
                state.update({"status": "error", "terminal": {"error": str(exc)}})
                _atomic_json(state_path, state)
                raise
            persisted_package_id = state.get("package_id")
            if (persisted_package_id is not None
                    and persisted_package_id != package["package_id"]):
                exc = SupervisorError(
                    "supervisor package does not match the persisted package_id"
                )
                state.update({"status": "error", "terminal": {"error": str(exc)}})
                _atomic_json(state_path, state)
                raise exc
            attempt = int(state.get("attempt") or 0) + 1
            run_id = f"{package['run_id_prefix']}-{attempt:06d}"
            if len(run_id) > 40:
                raise SupervisorError("generated run ID exceeds launcher limit")
            values = {"run_id": run_id, "attempt": str(attempt),
                      "package": str(package_path)}
            result_path = Path(package["result_path"].format_map(values))
            if result_path.exists():
                raise SupervisorError(f"fresh run ID collision: {result_path}")
            prelaunch = subprocess.run(
                _format_parts(package["prelaunch_argv"], values), check=False,
            )
            if prelaunch.returncode != 0:
                raise SupervisorError(
                    f"prelaunch active-state/sterility check exited {prelaunch.returncode}"
                )
            trigger = state.pop("next_trigger", "START_OR_RESUME")
            state.update({"attempt": attempt, "run_id": run_id,
                          "status": "launching", "package_id": package["package_id"]})
            _atomic_json(state_path, state)
            _ledger(ledger_path, event="LAUNCH", attempt=attempt, run_id=run_id,
                    trigger=trigger,
                    package_id=package["package_id"], next_action="RUN")
            completed = subprocess.run(_format_parts(package["launch_argv"], values), check=False)
            try:
                action, evidence = _classify(package, values, completed.returncode)
                max_cost, max_wall = _registered_budget(package)
            except SupervisorError as exc:
                state.update({"status": "error", "terminal": {"error": str(exc)}})
                _atomic_json(state_path, state)
                raise
            cumulative_cost = _finite_nonnegative(
                state.get("supervised_cost_usd") or 0,
                "supervisor cumulative cost",
            ) + float(evidence.get("attempt_cost_usd") or 0)
            cumulative_wall = _finite_nonnegative(
                state.get("supervised_wall_seconds") or 0,
                "supervisor cumulative wall time",
            ) + float(evidence.get("attempt_wall_seconds") or 0)
            state.update({
                "supervised_cost_usd": cumulative_cost,
                "supervised_wall_seconds": cumulative_wall,
            })
            ceiling_reasons = []
            if cumulative_cost > max_cost or (
                action in {"retry", "advance"} and cumulative_cost >= max_cost
            ):
                ceiling_reasons.append("COST_CEILING")
            if cumulative_wall > max_wall or (
                action in {"retry", "advance"} and cumulative_wall >= max_wall
            ):
                ceiling_reasons.append("WALL_CEILING")
            if ceiling_reasons:
                action = "stop"
                evidence = {
                    **evidence,
                    "stop_reasons": ceiling_reasons,
                    "supervised_cost_usd": cumulative_cost,
                    "supervised_wall_seconds": cumulative_wall,
                }
            _ledger(ledger_path, event="DECISION", attempt=attempt, run_id=run_id,
                    trigger=evidence, returncode=completed.returncode,
                    next_action=action.upper())

            if action in {"complete", "stop"}:
                state.update({"status": action, "terminal": evidence})
                _atomic_json(state_path, state)
                return 0 if action == "complete" else 3
            if action == "advance":
                try:
                    next_path = _next_package(package, values, package_path)
                except SupervisorError as exc:
                    state.update({"status": "error", "terminal": {"error": str(exc)}})
                    _atomic_json(state_path, state)
                    raise
                state.update({"package_path": str(next_path), "status": "ready",
                              "package_id": _validate_package(next_path)["package_id"],
                              "next_trigger": "BANKED_PARTIAL", "retry_streak": 0})
                _atomic_json(state_path, state)
                if args.once:
                    return 0
                continue

            raw_pattern = package.get("raw_glob")
            raw_paths = (
                [Path(path) for path in glob.glob(raw_pattern.format_map(values))]
                if isinstance(raw_pattern, str)
                else list(result_path.parent.glob("claude_raw/*.jsonl"))
            )
            reset_epoch = _find_reset_epoch(raw_paths)
            now = time.time()
            if reset_epoch is not None and not now <= reset_epoch <= now + 14 * 86400:
                reset_epoch = None
            retry_streak = int(state.get("retry_streak") or 0) + 1
            fallback = min(
                args.max_backoff,
                args.initial_backoff * (2 ** min(retry_streak - 1, 10)),
            )
            wake = max(now + fallback, float(reset_epoch or 0))
            wake += random.SystemRandom().uniform(0, args.jitter)
            state.update({"status": "waiting", "next_attempt_epoch": wake,
                          "provider_reset_epoch": reset_epoch,
                          "retry_streak": retry_streak,
                          "next_trigger": evidence["end_reason"]})
            _atomic_json(state_path, state)
            _ledger(ledger_path, event="WAIT", attempt=attempt, run_id=run_id,
                    trigger=evidence["end_reason"], next_action="RETRY",
                    next_attempt_epoch=wake, provider_reset_epoch=reset_epoch)
            if args.once:
                return 0
            while time.time() < wake:
                time.sleep(min(60.0, wake - time.time()))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--initial-backoff", type=float, default=900)
    parser.add_argument("--max-backoff", type=float, default=21600)
    parser.add_argument("--jitter", type=float, default=120)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args)
    except SupervisorError as exc:
        try:
            _ledger(
                args.ledger.resolve(), event="ERROR", trigger=str(exc),
                next_action="STOP_FAIL_CLOSED",
            )
        except OSError:
            pass
        print(f"trusted-core supervisor error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
