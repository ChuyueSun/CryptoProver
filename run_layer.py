#!/usr/bin/env python3
"""Convenience wrapper: run the MVP across a known layer set.

Layer-set definitions mirror inference_dalek/eval/domain_layers.py. We hard-
code them here to keep this script standalone — no cross-project import.

Usage:
    python run_layer.py L0 --project /path/to/dalek-lite/curve25519-dalek
    python run_layer.py A  --project ...        # HAB alias for L0+L1
    python run_layer.py ALL --project ...       # full sweep L0..L9 in order

Resume behavior (S1):
    Re-running with the SAME --run-id automatically skips modules whose
    result.json shows success=true. Useful for crash recovery (e.g.
    claude binary auto-update mid-campaign).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import results as _results  # noqa: E402
from run import run_task  # noqa: E402


# Domain layers L0-L9 mirror inference-dalek/inference_dalek/eval/domain_layers.py.
# Each layer's modules are listed in the order the source defines them; cross-layer
# dependency order is L0 → L1 → ... → L9. For per-layer runs, the agent gets the
# same context regardless of order within a layer (no in-layer deps).
_L0 = [
    "specs::field_specs",
    "specs::field_specs_u64",
    "lemmas::field_lemmas::u64_5_as_nat_lemmas",
    "lemmas::field_lemmas::pow2_51_lemmas",
]
_L1 = [
    "lemmas::field_lemmas::add_lemmas",
    "lemmas::field_lemmas::negate_lemmas",
    "lemmas::field_lemmas::reduce_lemmas",
    "lemmas::field_lemmas::mul_lemmas",
    "lemmas::field_lemmas::compute_q_lemmas",
]
_L2 = [
    "lemmas::field_lemmas::pow_chain_lemmas",
    "lemmas::field_lemmas::pow2k_lemmas",
    "lemmas::field_lemmas::pow22501_t3_lemma",
    "lemmas::field_lemmas::pow22501_t19_lemma",
    "lemmas::field_lemmas::pow_p58_lemma",
    "lemmas::field_lemmas::invert_lemmas",
    "lemmas::field_lemmas::constants_lemmas",
    "lemmas::field_lemmas::field_algebra_lemmas",
    "lemmas::field_lemmas::sqrt_m1_lemmas",
    "lemmas::field_lemmas::sqrt_ratio_lemmas",
    "lemmas::field_lemmas::batch_invert_lemmas",
    "backend::serial::u64::field",
    "field",
]
_L3 = [
    "specs::core_specs",
    "lemmas::field_lemmas::load8_lemmas",
    "lemmas::field_lemmas::as_bytes_lemmas",
    "lemmas::field_lemmas::from_bytes_lemmas",
    "lemmas::field_lemmas::limbs_to_bytes_lemmas",
    "lemmas::field_lemmas::to_bytes_reduction_lemmas",
]
_L4 = [
    "specs::scalar_specs",
    "specs::scalar52_specs",
    "specs::montgomery_reduce_specs",
    "lemmas::scalar_lemmas",
    "lemmas::scalar_lemmas_extra",
    "lemmas::scalar_byte_lemmas::bytes_to_scalar_lemmas",
    "lemmas::scalar_byte_lemmas::scalar_to_bytes_lemmas",
    "lemmas::scalar_montgomery_lemmas",
    "lemmas::scalar_lemmas_::montgomery_reduce_lemmas",
    "lemmas::scalar_lemmas_::montgomery_reduce_part1_chain_lemmas",
    "lemmas::scalar_lemmas_::montgomery_reduce_part2_chain_lemmas",
    "lemmas::scalar_lemmas_::radix16_lemmas",
    "lemmas::scalar_lemmas_::radix_2w_lemmas",
    "lemmas::scalar_lemmas_::naf_lemmas",
    "lemmas::scalar_batch_invert_lemmas",
    "backend::serial::u64::scalar",
    "scalar",
    "scalar_helpers",
]
_L5 = [
    "specs::edwards_specs",
    "specs::arithm_trait_specs",
    "lemmas::edwards_lemmas::constants_lemmas",
    "lemmas::edwards_lemmas::curve_equation_lemmas",
    "lemmas::edwards_lemmas::step1_lemmas",
]
_L6 = [
    "lemmas::edwards_lemmas::niels_addition_correctness",
    "lemmas::edwards_lemmas::double_correctness",
    "lemmas::edwards_lemmas::decompress_lemmas",
    "lemmas::edwards_lemmas::straus_lemmas",
    "lemmas::edwards_lemmas::mul_base_lemmas",
    "lemmas::edwards_lemmas::vartime_double_base_lemmas",
    "lemmas::edwards_lemmas::pippenger_lemmas",
    "specs::window_specs",
    "window",
    "edwards",
]
_L7 = [
    "specs::montgomery_specs",
    "lemmas::montgomery_lemmas",
    "lemmas::montgomery_curve_lemmas",
    "lemmas::montgomery_pow_chain_lemmas",
    "montgomery",
]
_L8 = [
    "specs::ristretto_specs",
    "lemmas::ristretto_lemmas::elligator_lemmas",
    "lemmas::ristretto_lemmas::axioms",
    "lemmas::ristretto_lemmas::batch_compress_lemmas",
    "ristretto",
]
_L9 = [
    "core_assumes",
    "specs::primality_specs",
    "specs::proba_specs",
    "specs::iterator_specs",
    "specs::lizard_specs",
    "backend::serial::u64::constants",
    "backend::serial::u64::subtle_assumes",
    "backend::serial::curve_models",
    "backend::serial::scalar_mul::variable_base",
    "backend::serial::scalar_mul::vartime_double_base",
    "backend::serial::scalar_mul::straus",
    "backend::serial::scalar_mul::precomputed_straus",
    "backend::serial::scalar_mul::pippenger",
    "constants",
    "traits",
]


LAYER_SETS: dict[str, list[str]] = {
    # Per-domain-layer (mirrors inference-dalek/inference_dalek/eval/domain_layers.py)
    "L0": _L0,
    "L1": _L1,
    "L2": _L2,
    "L3": _L3,
    "L4": _L4,
    "L5": _L5,
    "L6": _L6,
    "L7": _L7,
    "L8": _L8,
    "L9": _L9,
    # HAB-set aliases (mirrors inference_dalek/eval/domain_layers.py _LAYER_SET_INDICES)
    "A": _L0 + _L1,
    "B": _L3,
    "C": _L5 + _L6,
    "D": _L8,
    # Full sweep, dependency-ordered
    "ALL": _L0 + _L1 + _L2 + _L3 + _L4 + _L5 + _L6 + _L7 + _L8 + _L9,
}


# Axiom modules — admits in these files are mathematical axioms by design.
# The agent's classifier (M4) already recognizes them by file/fn-name signals,
# so we don't auto-skip; we just note them here for documentation.
AXIOM_MODULES: set[str] = {
    "specs::primality_specs",
    "core_assumes",
    "backend::serial::u64::subtle_assumes",
    "specs::proba_specs",
    "lemmas::edwards_lemmas::curve_equation_lemmas",
    "lemmas::ristretto_lemmas::axioms",
}


def module_to_file(module: str, project: Path) -> Path:
    """Resolve a module path like `specs::field_specs` to a .rs file."""
    rel = Path("src") / (module.replace("::", "/") + ".rs")
    candidate = project / rel
    if candidate.exists():
        return candidate
    # Some modules live in mod.rs files
    mod_rs = project / "src" / module.replace("::", "/") / "mod.rs"
    if mod_rs.exists():
        return mod_rs
    raise FileNotFoundError(f"Could not resolve module {module!r} under {project}/src/")


def _previously_successful(results_root: Path, run_id: str, module: str,
                           project: Path) -> bool:
    """S1 — return True if a prior run of this module under the same run_id
    already finished with success=True. Used to make re-runs idempotent."""
    try:
        target = module_to_file(module, project)
    except FileNotFoundError:
        return False
    target_id = _results.target_id_from_path(target)
    rj = results_root / run_id / target_id / "result.json"
    if not rj.exists():
        return False
    try:
        d = json.loads(rj.read_text())
        return bool(d.get("success", False))
    except (OSError, json.JSONDecodeError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("layer_set", choices=sorted(LAYER_SETS.keys()))
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--results", type=Path, default=Path("results"))
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip modules already in proven_registry.json")
    ap.add_argument("--no-resume", action="store_true",
                    help="Disable S1 resume-from-checkpoint (default: enabled). "
                         "When enabled, re-running with the same --run-id skips "
                         "modules whose result.json shows success=true.")
    ap.add_argument("--budget-min-floor", type=float, default=30.0,
                    help="Auto-budget floor (minutes). Default: 30. "
                         "(Empirical finding: 20 min was too tight for 6-admit "
                         "modules where round 1 consumes the full budget; "
                         "subsequent rounds got squeezed to 60s and the "
                         "agent died before fixing partial work.)")
    ap.add_argument("--budget-min-per-admit", type=float, default=1.5,
                    help="Auto-budget slope (minutes per admit). Default: 1.5.")
    ap.add_argument("--only", nargs="+", default=None,
                    help="Only run these module paths (subset of the layer set).")
    ap.add_argument("--verus-rlimit", type=float, default=80.0,
                    help="Pass --rlimit FLOAT to harness verus_check. Default: 80.")
    ap.add_argument("--no-failure-memory", action="store_true",
                    help="Skip rendering failure_memory into prompts.")
    ap.add_argument("--vstd-root", type=Path, default=None,
                    help="Path to Verus's vstd source to index alongside the "
                         "project. Forwarded to run.run_task for every module.")
    args = ap.parse_args()

    modules = list(LAYER_SETS[args.layer_set])
    project = args.project.resolve()
    results_root = args.results.resolve()
    run_id = args.run_id or _results.run_id_new(f"layer{args.layer_set}")
    results_root.mkdir(parents=True, exist_ok=True)

    # Optional --only filter (subset of the layer set, exact module-path match)
    if args.only:
        wanted = set(args.only)
        modules = [m for m in modules if m in wanted]
        unmatched = wanted - set(modules)
        if unmatched:
            print(f"[run_layer] WARN: --only entries not in layer set: {sorted(unmatched)}")

    # Optional skip-existing (proven_registry — global success across all runs)
    if args.skip_existing:
        reg = results_root / "proven_registry.json"
        if reg.exists():
            existing = {p["name"] for p in json.loads(reg.read_text()).get("proven", [])}
            new_modules = [m for m in modules
                           if module_to_file(m, project).stem not in existing]
            if len(new_modules) < len(modules):
                print(f"[run_layer] skipping {len(modules) - len(new_modules)} "
                      f"already-proven modules (registry hit)")
            modules = new_modules

    # S1 resume-from-checkpoint (per-run-id): skip modules whose prior result.json
    # in THIS run_id shows success=true. Enabled by default. Disabled by --no-resume.
    if not args.no_resume:
        skipped_resume = [m for m in modules
                          if _previously_successful(results_root, run_id, m, project)]
        if skipped_resume:
            print(f"[run_layer] resume: skipping {len(skipped_resume)} module(s) "
                  f"already successful under run_id={run_id}")
            for m in skipped_resume:
                print(f"[run_layer]   ✓ {m}")
            modules = [m for m in modules if m not in skipped_resume]

    print(f"[run_layer] layer set {args.layer_set}: {len(modules)} module(s)")
    print(f"[run_layer] run_id     = {run_id}")
    print(f"[run_layer] results    = {results_root}")
    print()

    summary: list[dict] = []
    # S3 — cumulative tracking. No abort cap; we're on a fixed plan so the
    # API enforces its own rate limits.
    cum_cost = 0.0
    cum_input = 0
    cum_cache_creation = 0
    cum_output = 0
    start_all = time.time()

    for i, module in enumerate(modules, 1):
        print("=" * 70)
        print(f"[run_layer] [{i}/{len(modules)}] {module}")
        print("=" * 70)
        try:
            target = module_to_file(module, project)
        except FileNotFoundError as e:
            print(f"[run_layer] SKIP: {e}")
            summary.append({"module": module, "success": False,
                            "reason": "file_not_found"})
            continue

        # Auto-budget per module: max(floor, slope * admits)
        try:
            num_admits = target.read_text().count("admit()")
        except OSError:
            num_admits = 0
        budget_min = max(args.budget_min_floor,
                         args.budget_min_per_admit * num_admits)
        is_axiom = module in AXIOM_MODULES
        axiom_note = " [AXIOM module — admits expected to remain]" if is_axiom else ""
        print(f"[run_layer] {num_admits} admit(s) → budget {budget_min:.0f} min{axiom_note}")

        try:
            result = run_task(
                target=target, project=project,
                run_id=run_id, results_root=results_root,
                max_rounds=args.rounds,
                max_task_minutes=budget_min,
                verus_rlimit=args.verus_rlimit,
                skip_failure_memory=args.no_failure_memory,
                vstd_root=args.vstd_root.resolve() if args.vstd_root else None,
            )
            # Accumulate S3 stats from each round's claude_usage.
            this_cost = 0.0
            this_cc = 0
            this_in = 0
            this_out = 0
            for rr in result.round_results:
                u = rr.claude_usage or {}
                this_cost += u.get("total_cost_usd", 0) or 0
                this_cc += u.get("cache_creation_input_tokens", 0) or 0
                this_in += u.get("input_tokens", 0) or 0
                this_out += u.get("output_tokens", 0) or 0
            cum_cost += this_cost
            cum_cache_creation += this_cc
            cum_input += this_in
            cum_output += this_out
            print(f"[run_layer] module cost ${this_cost:.2f} "
                  f"(cc={this_cc/1000:.0f}k out={this_out/1000:.1f}k) | "
                  f"cumulative ${cum_cost:.2f}")

            summary.append({
                "module": module,
                "target": str(target),
                "success": result.success,
                "end_reason": result.end_reason,
                "rounds_used": result.rounds_used,
                "duration_seconds": result.duration_seconds,
                "admit_classification": result.admit_classification,
                "cost_usd": this_cost,
                "cache_creation_tokens": this_cc,
                "output_tokens": this_out,
            })
        except Exception as e:
            print(f"[run_layer] ERROR: {e}", file=sys.stderr)
            summary.append({"module": module, "success": False,
                            "reason": f"exception: {e!r}"})

    duration_all = time.time() - start_all

    print("\n" + "=" * 70)
    print(f"LAYER SET {args.layer_set} — SUMMARY")
    print("=" * 70)
    ok = sum(1 for s in summary if s.get("success"))
    print(f"Verified: {ok}/{len(summary)}")
    print(f"Total duration: {duration_all/60:.1f} min")
    print(f"Total cost: ${cum_cost:.2f}")
    print(f"Total cache_creation: {cum_cache_creation/1000:.0f}k tokens")
    print(f"Total output: {cum_output/1000:.1f}k tokens")
    print()
    for s in summary:
        status = "✓" if s.get("success") else "✗"
        rounds = s.get("rounds_used", "-")
        dur = s.get("duration_seconds", 0)
        cls = s.get("admit_classification") or {}
        cls_note = ""
        if cls.get("total", 0) > 0:
            cls_note = f" admits[hard={cls.get('hard',0)},int={cls.get('intentional',0)}]"
        cost = s.get("cost_usd", 0)
        print(f"  {status}  {s['module']:55s}  rounds={rounds}  "
              f"dur={dur:.0f}s  ${cost:>5.2f}{cls_note}  "
              f"({s.get('end_reason', s.get('reason', ''))})")

    # Persist the layer-run summary
    summary_path = results_root / run_id / "layer_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({
        "layer_set": args.layer_set,
        "run_id": run_id,
        "total": len(summary),
        "verified": ok,
        "duration_seconds_total": duration_all,
        "cumulative_cost_usd": cum_cost,
        "cumulative_cache_creation_tokens": cum_cache_creation,
        "cumulative_input_tokens": cum_input,
        "cumulative_output_tokens": cum_output,
        "targets": summary,
    }, indent=2))
    print(f"\n[run_layer] summary saved to {summary_path}")

    return 0 if ok == len(summary) else 1


if __name__ == "__main__":
    sys.exit(main())
