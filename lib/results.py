"""Result directory helpers.

Layout (per Numina):
    results/<run_id>/<target_id>/
        result.json
        round_N.json
        claude_raw/round_N.jsonl
        cli.log                     # per-task skill log (CLI_LOG_PATH)
        spec_snapshot.json
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Optional


def task_dir(results_root: Path, run_id: str, target_id: str) -> Path:
    d = results_root / run_id / target_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "claude_raw").mkdir(exist_ok=True)
    return d


def target_id_from_path(target_path: Path) -> str:
    """Stable identifier for a target file."""
    return target_path.stem


def run_id_new(prefix: str = "run") -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        _jsonable(data), indent=2, ensure_ascii=False,
    ))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses, Paths, etc. to JSON-friendly types."""
    if is_dataclass(obj):
        return _jsonable(asdict(obj))
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


@dataclass
class RoundResult:
    round_number: int
    end_reason: Optional[str]     # COMPLETE | LIMIT | None
    returncode: int
    duration_seconds: float
    verus_okay: bool
    # Authoritative checker counts captured before any feedback sampling.
    # `verus_errors` below is intentionally a diversified prompt sample, not a
    # progress metric.
    verus_error_count_raw: int = 0
    verification_error_count: int = 0
    # Raw Verus final summary counts from "verification results:: N verified,
    # M errors", when the checker emitted that line. These are distinct from
    # the structured diagnostic counts above.
    verified_count: int | None = None
    raw_verus_error_count: int | None = None
    diagnostic_kind_counts: dict = field(default_factory=dict)
    has_build_wrapper: bool = False
    compile_blocked_or_indeterminate: bool = False
    # True when the gate verus_check exited without Verus' final
    # "verification results" summary (internal panic / build abort / kill):
    # every count above is then a lower bound, not the frontier.
    truncated: bool = False
    verus_errors: list[dict] = field(default_factory=list)
    spec_drift: list[dict] = field(default_factory=list)
    claude_usage: dict = field(default_factory=dict)
    # Diagnostic-only usage parsed from claude_raw/round_N.jsonl. Unlike
    # claude_usage, this does not claim to be billable-authoritative; it exists
    # so no-result/killed rounds and subagent-heavy rounds are still observable.
    raw_usage_summary: dict = field(default_factory=dict)
    # Number of `Agent` (subagent) tool-uses the agent spawned this round.
    # Read-only-offload metric: lets us measure whether the prompt's
    # read-delegation framing actually moves the agent off 0 spawns.
    agent_delegations: int = 0
    # Content-addressed identity of the candidate and runner-owned verifier
    # gate. These are deliberately separate from the diversified prompt sample:
    # a later seed/reset must be able to bind a decision to exact source bytes.
    tree_receipt: dict = field(default_factory=dict)
    gate_receipt: dict = field(default_factory=dict)
    # Full normalized inventory is persisted for audit; `verus_errors` stays a
    # compact, diversified agent-feedback sample.
    diagnostic_inventory: list[dict] = field(default_factory=list)
    # A round-boundary transaction classification. The runner cannot observe
    # arbitrary interior editor hunks, so this describes only the pre/post
    # candidate it actually gated.
    candidate_transaction: dict = field(default_factory=dict)
    # Stream parser evidence: terminal metadata can follow a valid `result`
    # event, so callers can distinguish that from a genuinely missing result.
    parser_provenance: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    task_id: str
    run_id: str
    target_path: str
    module_path: str
    success: bool
    end_reason: str
    rounds_used: int
    duration_seconds: float
    # Execution CLI used for agent turns. `claude` remains the default for
    # backward-compatible readers of older result.json files.
    agent_backend: str = "claude"
    round_results: list[RoundResult] = field(default_factory=list)
    error_message: Optional[str] = None
    # Auto-reset bookkeeping: round numbers where a fresh claude session
    # was started mid-task. Empty if no resets fired.
    reset_round_starts: list[int] = field(default_factory=list)
    # M4 classification of remaining admits at end-of-task. Keys:
    # total, intentional, hard, detail. See run.classify_remaining_admits.
    admit_classification: dict = field(default_factory=dict)
    # Final-state gate telemetry, duplicated at top level so progress monitors
    # do not have to infer it from nested round_results.
    final_verus_okay: bool = False
    final_admits_remaining: int = -1
    final_hard_admits_remaining: int = -1
    final_intentional_axiom_admits: int = 0
    final_error_count: int = 0
    final_spec_drift_count: int = 0
    # Optional one-line label/provenance for runs whose starting state changes
    # what the result measures (for example operator-scaffolded lane isolation).
    experiment_provenance: Optional[str] = None
    # Causal fresh-session records. `reset_round_starts` remains for backward
    # compatibility; this preserves the why and the source/gate receipt passed
    # across the reset boundary.
    reset_events: list[dict] = field(default_factory=list)
    # Disclosed interventions (for example a deliberate signal or trusted
    # header restore) supplied by the launcher/operator.
    operator_events: list[dict] = field(default_factory=list)
    # A terminal integrity rejection must say whether the live worktree was
    # restored to a clean snapshot before it became reusable.
    terminal_disposition: dict = field(default_factory=dict)
    # Immutable acceptance/rejection receipt written alongside result.json.
    promotion_receipt: dict = field(default_factory=dict)
    final_tree_receipt: dict = field(default_factory=dict)
    final_gate_receipt: dict = field(default_factory=dict)
    # Split the task wall budget into productive agent time and reserved final
    # gate time so a verifier cannot be starved by the Claude deadline.
    agent_budget_seconds: float = 0.0
    agent_elapsed_seconds: float = 0.0
    final_gate_allowance_seconds: float = 0.0
