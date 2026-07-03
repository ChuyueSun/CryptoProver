#!/usr/bin/env python3
"""Redundant racing — first-valid-wins across K cold attempts on ONE target.

This is the successor to the carryover thread (v1/P1/P2). That thread proved the
problem is *variance*, not context (see docs/extension_spec.md E2): the same arm
on the same target with the same flags would succeed in one rep and NEEDS_DECOMP
in another. Carrying state into a fresh session never fixed it. The fix (autoform
Lesson 4) is redundancy: run K independent cold attempts on the same target,
first to pass the EXISTING final gate wins, cancel the rest. Cancellation is the
whole economy — it turns a flat K× cost into "K× only until the first win."

Design (spec: docs/redundant-racing.md), non-negotiable decisions:
  * Subprocess-per-attempt, not in-process threads. Each attempt is its own
    `run.py` process in its own git worktree with its own --results subdir and
    its own process group (start_new_session=True). Cancelling a loser =
    os.killpg(SIGKILL) on its group — claude + cargo verus + z3 + Monitor loops.
  * Win = the existing gate. An attempt wins iff its result.json has
    success==True (already means verus_okay AND 0 hard admits). We never invent
    a new acceptance check. If all K fail → best loser + aggregate end_reason,
    no false green.
  * Serialize the ONE shared writable resource: the canonical
    proven_registry.json (and failure_memory.json). Each attempt writes a
    PRIVATE copy under its own --results; race.py merges only the winner's entry
    into the canonical file under a non-blocking fcntl.flock.
  * Worktrees created SERIALLY (parallel `git worktree add` corrupts git state),
    run in parallel, always torn down with --force in a finally (incl. Ctrl-C).
  * Cold attempts only — each run.py attempt starts fresh (no cross-attempt
    carryover); racing REPLACES the carryover idea.

This file wraps run_task; it does not edit it. Stdlib only.

Usage:
    python race.py <target.rs> --project <crate_root> [-K 3] \
        [--base-commit HEAD] [--rounds 5] [--max-task-minutes 45] \
        [--global-cap-minutes 60] [--model sonnet] [--vstd-root /path/to/vstd]

The §4 validation A/B (S vs R3, ≥3 reps, variance-heavy target, cache removed)
is a SEPARATE, gated, costly step — do not run it from here automatically.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from lib import results as _results  # noqa: E402


# ── data ─────────────────────────────────────────────────────────────────────

@dataclass
class AttemptOutcome:
    index: int
    worktree: str
    results_subdir: str
    pid: Optional[int]          # == process-group id (start_new_session)
    returncode: Optional[int]
    success: bool
    end_reason: Optional[str]
    admits_filled: int          # for ranking losers (more filled = "better")
    result: dict = field(default_factory=dict)


@dataclass
class RaceResult:
    target: str
    run_id: str
    K: int
    success: bool
    winner_index: Optional[int]
    winner_worktree: Optional[str]
    end_reason: str             # winner's COMPLETE, or aggregate on all-fail
    wall_clock_seconds: float
    attempts: list[AttemptOutcome] = field(default_factory=list)


# ── git worktree lifecycle (§2.4 / §3) ───────────────────────────────────────

def _git(*args: str, cwd: Optional[Path] = None) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def _create_worktrees(gitroot: Path, base_commit: str, run_id: str,
                      K: int) -> list[Path]:
    """Serial creation (parallel `git worktree add` corrupts git state)."""
    base = Path("/private/tmp") if Path("/private/tmp").exists() else Path("/tmp")
    paths: list[Path] = []
    for i in range(1, K + 1):
        wt = base / f"race-{run_id}-{i}"
        # Defensive: a stale worktree from a crashed prior run would block add.
        if wt.exists():
            try:
                _git("worktree", "remove", "--force", str(wt), cwd=gitroot)
            except RuntimeError:
                shutil.rmtree(wt, ignore_errors=True)
        _git("worktree", "add", "--detach", str(wt), base_commit, cwd=gitroot)
        paths.append(wt)
        print(f"[race] worktree {i}/{K}: {wt} @ {base_commit}", flush=True)
    return paths


def _teardown_worktrees(gitroot: Path, paths: list[Path]) -> None:
    """Always run in a finally — incl. on exception / Ctrl-C."""
    for wt in paths:
        try:
            _git("worktree", "remove", "--force", str(wt), cwd=gitroot)
            print(f"[race] removed worktree {wt}", flush=True)
        except RuntimeError as e:
            print(f"[race] WARN: worktree remove failed for {wt}: {e}", flush=True)
            shutil.rmtree(wt, ignore_errors=True)


# ── canonical registry / failure-memory merge (§2.3) ─────────────────────────

def _flock_nonblocking(fh, timeout_sec: float = 10.0) -> bool:
    """Mirror autoform's _flock_nonblocking: poll LOCK_NB + sleep, never block
    uninterruptibly. Returns True on acquire, False on timeout."""
    deadline = time.time() + timeout_sec
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(0.1)


def _merge_winner_registry(canonical_results: Path, winner: AttemptOutcome,
                           target: Path, project: Path, run_id: str) -> None:
    """Append ONLY the winner's entry to the canonical proven_registry.json,
    under a non-blocking flock. The per-attempt private registries are never
    promoted — this is the single point of contention (§2.3)."""
    canonical_results.mkdir(parents=True, exist_ok=True)
    reg_path = canonical_results / "proven_registry.json"
    lock_path = canonical_results / "proven_registry.json.lock"
    target_id = _results.target_id_from_path(target)
    module = winner.result.get("module_path", "")
    entry = {
        "name": target_id,
        "module": module,
        "file": (str(target.relative_to(project))
                 if _is_relative_to(target, project) else str(target)),
        "run_id": run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "via": "race",
    }
    with open(lock_path, "w") as lock_fh:
        if not _flock_nonblocking(lock_fh):
            print("[race] WARN: could not lock proven_registry.json — "
                  "skipping canonical merge (winner already recorded "
                  "in its private registry)", flush=True)
            return
        try:
            existing = (json.loads(reg_path.read_text())
                        if reg_path.exists() else {"proven": []})
            existing.setdefault("proven", []).append(entry)
            reg_path.write_text(json.dumps(existing, indent=2))
            print(f"[race] merged winner into {reg_path}", flush=True)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _is_relative_to(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


# ── per-attempt subprocess (§1.2) ────────────────────────────────────────────

def _read_result_json(results_subdir: Path, run_id: str,
                      target_id: str) -> dict:
    """run_task writes to <results>/<run_id>/<target_id>/result.json."""
    rj = results_subdir / run_id / target_id / "result.json"
    if not rj.exists():
        return {}
    try:
        return json.loads(rj.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _admits_filled(result: dict) -> int:
    """Rank losers by how much they accomplished. We don't have start admits
    here, so use (negative) remaining hard admits as the proxy: fewer hard
    admits left = better loser."""
    cls = result.get("admit_classification") or {}
    hard = cls.get("hard")
    if hard is None:
        return 0
    return -int(hard)


async def _run_attempt(index: int, attempt_target: Path, attempt_project: Path,
                       results_subdir: Path, run_id: str, target_id: str,
                       opts: dict, env: dict,
                       live_pids: dict[int, int]) -> AttemptOutcome:
    """Spawn one cold run.py attempt in its own process group.

    `live_pids` maps index -> pgid for currently-running attempts. We register
    the pid the INSTANT the subprocess spawns (not after it exits) so the race
    loop can SIGKILL a still-running loser the moment a winner lands — capturing
    it only in the return value would make the kill a no-op (the loser hasn't
    returned yet). We pop on completion so the kill loop never signals a pid that
    already exited (and whose pgid the OS may have recycled)."""
    cmd = [
        sys.executable, str(HERE / "run.py"), str(attempt_target),
        "--project", str(attempt_project),
        "--results", str(results_subdir),
        "--run-id", run_id,
        "--rounds", str(opts["rounds"]),
        "--max-task-minutes", str(opts["max_task_minutes"]),
    ]
    if opts.get("model"):
        cmd += ["--model", opts["model"]]
    if opts.get("vstd_root"):
        cmd += ["--vstd-root", str(opts["vstd_root"])]
    if opts.get("verus_rlimit") is not None:
        cmd += ["--verus-rlimit", str(opts["verus_rlimit"])]

    print(f"[race] launching attempt {index}: {' '.join(cmd)}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        start_new_session=True,          # own process group → killpg target
    )
    live_pids[index] = proc.pid          # pgid == pid (start_new_session)
    try:
        rc = await proc.wait()
    finally:
        live_pids.pop(index, None)
    result = _read_result_json(results_subdir, run_id, target_id)
    return AttemptOutcome(
        index=index,
        worktree=str(attempt_project),
        results_subdir=str(results_subdir),
        pid=proc.pid,
        returncode=rc,
        success=bool(result.get("success", False)),
        end_reason=result.get("end_reason"),
        admits_filled=_admits_filled(result),
        result=result,
    )


def _killpg(pid: Optional[int]) -> None:
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ── orchestration ────────────────────────────────────────────────────────────

async def _race(attempt_targets: list[tuple[int, Path, Path, Path]],
                run_id: str, target_id: str, opts: dict, env: dict,
                global_cap_seconds: float) -> tuple[Optional[AttemptOutcome],
                                                     list[AttemptOutcome]]:
    """Launch all attempts; return (winner_or_None, all_finished_outcomes).

    First attempt to finish with success==True wins; the instant we commit to a
    winner, every other live process group is SIGKILLed (§2.2). The winner is
    whoever we commit to FIRST — late result.json writes by a loser racing the
    kill are ignored."""
    tasks: dict[asyncio.Task, int] = {}
    # index -> pgid of a CURRENTLY-RUNNING attempt. Registered at spawn,
    # removed at completion, by _run_attempt itself. The kill loop reads it to
    # SIGKILL exactly the still-live losers (§2.2).
    live_pids: dict[int, int] = {}

    for (i, atgt, aproj, rsub) in attempt_targets:
        t = asyncio.create_task(_run_attempt(
            i, atgt, aproj, rsub, run_id, target_id, opts, env, live_pids))
        tasks[t] = i

    winner: Optional[AttemptOutcome] = None
    finished: list[AttemptOutcome] = []
    deadline = time.time() + global_cap_seconds

    try:
        while tasks:
            remaining = deadline - time.time()
            if remaining <= 0:
                print(f"[race] global cap ({global_cap_seconds:.0f}s) reached "
                      "— stopping race", flush=True)
                break
            done, _pending = await asyncio.wait(
                tasks.keys(), timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:        # timed out on this slice
                continue
            for t in done:
                outcome = t.result()
                del tasks[t]
                finished.append(outcome)
                status = "WIN" if outcome.success else (outcome.end_reason or "?")
                print(f"[race] attempt {outcome.index} finished: {status}",
                      flush=True)
                if outcome.success and winner is None:
                    winner = outcome
            if winner is not None:
                break
    finally:
        # Prompt + total cancellation (§2.2): SIGKILL every still-live group,
        # then reap the asyncio tasks so we don't leak them. live_pids holds
        # only attempts still running, keyed by index.
        killed = [idx for idx in list(live_pids.keys())]
        for idx in killed:
            _killpg(live_pids.get(idx))
        if killed:
            print(f"[race] SIGKILLed losing attempt group(s): "
                  f"{sorted(killed)}", flush=True)
        for t in list(tasks.keys()):
            try:
                outcome = await t           # reaped quickly after killpg
                finished.append(outcome)
            except asyncio.CancelledError:
                pass

    return winner, finished


def _aggregate_end_reason(outcomes: list[AttemptOutcome]) -> str:
    """All-fail aggregate. NEEDS_DECOMP if any attempt named it (the target may
    genuinely need decomposition); else the best loser's reason; else LIMIT."""
    reasons = [o.end_reason for o in outcomes if o.end_reason]
    if any(r == "NEEDS_DECOMP" for r in reasons):
        return "NEEDS_DECOMP"
    if reasons:
        # pick the reason of the best (most-filled) loser
        best = max(outcomes, key=lambda o: o.admits_filled)
        return best.end_reason or "LIMIT"
    return "LIMIT"


def race_task(target: Path, project: Path, K: int = 3,
              base_commit: str = "HEAD", results_root: Optional[Path] = None,
              run_id: Optional[str] = None, rounds: int = 5,
              max_task_minutes: float = 45.0,
              global_cap_minutes: Optional[float] = None,
              model: Optional[str] = None, vstd_root: Optional[Path] = None,
              verus_rlimit: Optional[float] = None) -> RaceResult:
    target = target.resolve()
    project = project.resolve()
    results_root = (results_root or Path("results")).resolve()
    run_id = run_id or _results.run_id_new("race")
    target_id = _results.target_id_from_path(target)
    if global_cap_minutes is None:
        # default: a little headroom over a single attempt's budget
        global_cap_minutes = max_task_minutes + 15.0

    gitroot = Path(_git("rev-parse", "--show-toplevel", cwd=project)).resolve()
    project_rel = project.relative_to(gitroot) if _is_relative_to(project, gitroot) else Path(".")
    target_rel = target.relative_to(project)
    base_sha = _git("rev-parse", base_commit, cwd=gitroot)

    # Race results live under a dedicated subtree; each attempt gets its own
    # private --results so its registry/failure_memory never collide (§2.3).
    race_root = results_root / run_id
    race_root.mkdir(parents=True, exist_ok=True)

    # Env: mirror run.py's CLAUDECODE key-strip so spawned claude uses session auth.
    env = os.environ.copy()
    if env.get("CLAUDECODE") == "1":
        env.pop("ANTHROPIC_API_KEY", None)

    opts = {
        "rounds": rounds, "max_task_minutes": max_task_minutes,
        "model": model, "vstd_root": vstd_root, "verus_rlimit": verus_rlimit,
    }

    print(f"[race] target={target_rel} project_rel={project_rel} "
          f"K={K} base={base_sha[:10]} run_id={run_id}", flush=True)
    print(f"[race] per-attempt budget={max_task_minutes:.0f}m  "
          f"global cap={global_cap_minutes:.0f}m", flush=True)

    start = time.time()
    worktrees: list[Path] = []
    try:
        worktrees = _create_worktrees(gitroot, base_sha, run_id, K)
        attempt_targets: list[tuple[int, Path, Path, Path]] = []
        for i, wt in enumerate(worktrees, 1):
            aproj = wt / project_rel
            atgt = aproj / target_rel
            rsub = race_root / f"attempt_{i}"
            attempt_targets.append((i, atgt, aproj, rsub))

        winner, finished = asyncio.run(_race(
            attempt_targets, run_id, target_id, opts, env,
            global_cap_minutes * 60.0,
        ))

        wall = time.time() - start

        if winner is not None:
            print(f"[race] WINNER: attempt {winner.index} "
                  f"({winner.end_reason}) in {wall/60:.1f}m wall-clock",
                  flush=True)
            # Winner artifacts: copy out target + diff.md before teardown.
            _capture_winner(gitroot, Path(winner.worktree), target_rel,
                            race_root)
            _merge_winner_registry(results_root, winner, target, project, run_id)
            end_reason = winner.end_reason or "COMPLETE"
            success = True
            winner_index = winner.index
            winner_wt = winner.worktree
        else:
            end_reason = _aggregate_end_reason(finished)
            print(f"[race] ALL {K} ATTEMPTS FAILED — aggregate "
                  f"end_reason={end_reason} (no false green)", flush=True)
            success = False
            winner_index = None
            winner_wt = None

        race_result = RaceResult(
            target=str(target), run_id=run_id, K=K, success=success,
            winner_index=winner_index, winner_worktree=winner_wt,
            end_reason=end_reason, wall_clock_seconds=wall,
            attempts=sorted(finished, key=lambda o: o.index),
        )
        _write_race_result(race_root, race_result)
        return race_result
    finally:
        _teardown_worktrees(gitroot, worktrees)


def _capture_winner(gitroot: Path, winner_worktree: Path, target_rel: Path,
                    race_root: Path) -> None:
    """Copy the winner's proved target file out and write its diff vs base so
    the proof survives worktree teardown (we do NOT auto-merge to canonical —
    a human/caller applies it; safer than mutating the canonical project)."""
    try:
        src = winner_worktree / target_rel
        dst = race_root / "winner_target.rs"
        shutil.copy2(src, dst)
        print(f"[race] winner target → {dst}", flush=True)
    except OSError as e:
        print(f"[race] WARN: could not copy winner target: {e}", flush=True)
    try:
        diff = _git("diff", cwd=winner_worktree)
        (race_root / "diff.md").write_text(
            "# Winner diff (vs base_commit)\n\n```diff\n" + diff + "\n```\n")
        print(f"[race] winner diff → {race_root / 'diff.md'}", flush=True)
    except RuntimeError as e:
        print(f"[race] WARN: could not capture winner diff: {e}", flush=True)


def _write_race_result(race_root: Path, rr: RaceResult) -> None:
    _results.write_json(race_root / "race_result.json", rr)
    print(f"[race] race_result → {race_root / 'race_result.json'}", flush=True)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Redundant racing: K cold run.py attempts on one target, "
                    "first-valid-wins, cancel losers.")
    ap.add_argument("target", type=Path, help="Target .rs file (inside project)")
    ap.add_argument("--project", type=Path, required=True,
                    help="Cargo crate root (the run.py --project).")
    ap.add_argument("-K", "--attempts", type=int, default=3,
                    help="Number of racing attempts (default: 3; keep small).")
    ap.add_argument("--base-commit", default="HEAD",
                    help="Commit each worktree checks out (the all-admits "
                         "state). Default: HEAD.")
    ap.add_argument("--results", type=Path, default=Path("results"),
                    help="Canonical results root (winner registry merge target).")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--max-task-minutes", type=float, default=45.0,
                    help="Per-attempt wall-clock budget (forwarded to run.py).")
    ap.add_argument("--global-cap-minutes", type=float, default=None,
                    help="Hard cap on the whole race; after it, all attempts "
                         "are killed and the best loser returned. "
                         "Default: max-task-minutes + 15.")
    ap.add_argument("--model", default=None)
    ap.add_argument("--vstd-root", type=Path, default=None)
    ap.add_argument("--verus-rlimit", type=float, default=None)
    args = ap.parse_args()

    if not args.target.exists():
        print(f"[error] target not found: {args.target}", file=sys.stderr)
        return 1
    if args.attempts < 1:
        print("[error] -K must be >= 1", file=sys.stderr)
        return 2

    rr = race_task(
        target=args.target, project=args.project, K=args.attempts,
        base_commit=args.base_commit, results_root=args.results,
        run_id=args.run_id, rounds=args.rounds,
        max_task_minutes=args.max_task_minutes,
        global_cap_minutes=args.global_cap_minutes,
        model=args.model, vstd_root=args.vstd_root,
        verus_rlimit=args.verus_rlimit,
    )

    print("\n" + "=" * 60)
    print(f"RACE {rr.run_id} — {'SUCCESS' if rr.success else 'FAILED'}")
    print(f"target={rr.target}")
    print(f"K={rr.K}  winner={rr.winner_index}  end_reason={rr.end_reason}")
    print(f"wall-clock={rr.wall_clock_seconds/60:.1f}m")
    for o in rr.attempts:
        tag = "✓ WIN" if o.success else f"✗ {o.end_reason}"
        print(f"  attempt {o.index}: {tag}  (rc={o.returncode})")
    print("=" * 60)
    return 0 if rr.success else 1


if __name__ == "__main__":
    sys.exit(main())
