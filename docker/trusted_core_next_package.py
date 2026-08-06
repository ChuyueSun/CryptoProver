#!/usr/bin/env python3
"""Generate a fail-closed Trust Core successor package from a reusable bank."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from lib import frontier, provenance, taxonomy  # noqa: E402


class NextPackageError(RuntimeError):
    pass


_LEG_LOCAL_AMENDMENTS = {
    "accounting_amendment", "leg9_accounting_amendment",
    "offline_recovery_amendment", "launch_seed_amendment",
    "oauth_cost_recovery", "operator_review_accounting",
    "relaunch_accounting_amendment",
}


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NextPackageError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise NextPackageError(f"JSON is not an object: {path}")
    return value


def _sha(path: Path) -> str:
    return provenance.sha256_file(path)


def _option(argv: list[str], name: str) -> str:
    try:
        index = argv.index(name)
        return argv[index + 1]
    except (ValueError, IndexError) as exc:
        raise NextPackageError(f"launch argv lacks {name}") from exc


def _replace_option(argv: list[str], name: str, value: str) -> list[str]:
    updated = list(argv)
    try:
        index = updated.index(name)
    except ValueError as exc:
        raise NextPackageError(f"launch argv lacks {name}") from exc
    if index + 1 >= len(updated):
        raise NextPackageError(f"launch argv has no value for {name}")
    updated[index + 1] = value
    return updated


def _optional_option(argv: list[str], name: str) -> str | None:
    try:
        return _option(argv, name)
    except NextPackageError:
        return None


def _upsert_option(
    argv: list[str], name: str, value: str, *,
    anchor: str = "--launch-registration",
) -> list[str]:
    """Replace ``name``'s value, inserting after ``anchor`` when absent.

    Root launches carry no seed/predecessor options by design (run_agents.sh
    forbids a fresh trusted-core root from importing campaign state), so the
    first bank of a fresh campaign must be able to ADD the four seed options
    to the successor argv instead of failing closed on replace-only (F9).
    Only the registered seed options go through this path; every other role
    keeps strict replace semantics.
    """
    updated = list(argv)
    if name in updated:
        return _replace_option(updated, name, value)
    try:
        anchor_index = updated.index(anchor)
    except ValueError as exc:
        raise NextPackageError(f"launch argv lacks {anchor}") from exc
    if anchor_index + 1 >= len(updated):
        raise NextPackageError(f"launch argv has no value for {anchor}")
    updated[anchor_index + 2:anchor_index + 2] = [name, value]
    return updated


def _gate_frontier_relation(previous: dict[str, Any], current: dict[str, Any]) -> str:
    """Mirror the runner's registered partial-frontier ordering.

    The successor generator is a separate fail-closed boundary: it must not
    trust a reusable promotion merely because ``run.py`` emitted one. The
    ordering itself is the SINGLE shared production body in lib/frontier.py
    (also consumed by run.py); this wrapper keeps only the generator's
    stricter receipt-shape requirements: undecided or shapeless receipts
    raise instead of classifying, except the lawful absent-previous INITIAL.
    """
    previous_vector = previous.get("vector") or {}
    current_vector = current.get("vector") or {}
    previous_tree = (previous.get("tree_receipt") or {}).get("tree_hash")
    current_tree = (current.get("tree_receipt") or {}).get("tree_hash")
    if not current_tree:
        raise NextPackageError("bank dominance receipts lack tree identity")
    if current_vector.get("verified_count") is None:
        raise NextPackageError("bank dominance receipts lack decided vectors")
    if not previous:
        return "INITIAL"
    if not previous_tree:
        raise NextPackageError("bank dominance receipts lack tree identity")
    if previous_vector.get("verified_count") is None:
        raise NextPackageError("bank dominance receipts lack decided vectors")
    return frontier.vector_relation(
        dict(previous_vector), dict(current_vector),
        previous_tree_hash=str(previous_tree), current_tree_hash=str(current_tree),
        missing_previous="zero",
    )


def _validate_bank_frontier(promotion: dict[str, Any]) -> str:
    """Require a terminal bank to retain or advance the runner's best gate."""
    banking_gate = promotion.get("banking_gate_receipt") or {}
    best_decided_gate = promotion.get("best_decided_gate_receipt") or {}
    relation = _gate_frontier_relation(best_decided_gate, banking_gate)
    if relation in {"DISPLACED", "REGRESSED"}:
        raise NextPackageError(
            "terminal bank is dominated by the best decided frontier: "
            f"{relation}"
        )
    final_tree = (promotion.get("final_tree_receipt") or {}).get("tree_hash")
    banking_tree = (banking_gate.get("tree_receipt") or {}).get("tree_hash")
    if not final_tree or final_tree != banking_tree:
        raise NextPackageError("terminal bank tree differs from banking gate")
    return relation


def _content_receipt(value: dict[str, Any], kind: str) -> str:
    receipt_id = value.get("receipt_id")
    if value.get("kind") != kind or receipt_id != provenance.receipt_id(value):
        raise NextPackageError(f"invalid {kind} receipt")
    return str(receipt_id)


def _run(command: list[str], *, stdout: Path | None = None) -> None:
    if stdout is None:
        completed = subprocess.run(command, check=False)
    else:
        with stdout.open("wb") as target:
            completed = subprocess.run(command, stdout=target, check=False)
    if completed.returncode != 0:
        raise NextPackageError(
            f"command exited {completed.returncode}: {' '.join(command)}"
        )


def _copy_authorized(source: Path, target: Path, paths: list[str]) -> None:
    for relative in paths:
        source_path = source / relative
        target_path = target / relative
        if source_path.is_file() or source_path.is_symlink():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.exists() or target_path.is_symlink():
                target_path.unlink()
            if source_path.is_symlink():
                target_path.symlink_to(os.readlink(source_path))
            else:
                shutil.copy2(source_path, target_path)
        elif target_path.exists() or target_path.is_symlink():
            target_path.unlink()


def _manifest_authorized_paths(manifest: dict[str, Any]) -> list[str]:
    """Return canonical workspace-relative edit paths from a peel manifest.

    Production peel manifests use ``files: [{"path": ...}]``.  Do not accept
    fixture-only aliases: an unknown schema must fail closed rather than
    silently collapse authority to the generated ``Cargo.lock`` exception.
    """
    entries = manifest.get("files")
    if entries is not None:
        if not isinstance(entries, list) or not entries:
            raise NextPackageError("peel manifest files authority is malformed")
        paths = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise NextPackageError("peel manifest file entry is malformed")
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                raise NextPackageError("peel manifest file entry lacks path")
            paths.append(path)
    else:
        raise NextPackageError("peel manifest lacks files path authority")
    for path in paths:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts or path == "Cargo.lock":
            raise NextPackageError(f"unsafe peel manifest edit path: {path}")
    return sorted(set(paths) | {"Cargo.lock"})


def build_cumulative_patch(
    *, canonical: Path, bank: Path, project_rel: Path, authorized: list[str],
    expected_tree: str, patch_path: Path,
) -> dict[str, Any]:
    canonical_receipt = provenance.source_tree_receipt(canonical / project_rel)
    bank_receipt = provenance.source_tree_receipt(bank / project_rel)
    canonical_files = {entry["path"]: entry for entry in canonical_receipt["files"]}
    bank_files = {entry["path"]: entry for entry in bank_receipt["files"]}
    changed = sorted(
        path for path in canonical_files.keys() | bank_files.keys()
        if canonical_files.get(path) != bank_files.get(path)
    )
    unauthorized = sorted(set(changed) - set(authorized))
    if unauthorized:
        raise NextPackageError(f"bank changes unauthorized paths: {unauthorized}")
    with tempfile.TemporaryDirectory(prefix="tc-next-patch-") as temporary:
        temp = Path(temporary)
        builder = temp / "builder"
        replay = temp / "replay"
        shutil.copytree(canonical, builder, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        _run(["git", "-C", str(builder), "init", "-q"])
        _run(["git", "-C", str(builder), "config", "user.name", "Trust Core Supervisor"])
        _run(["git", "-C", str(builder), "config", "user.email", "supervisor@invalid"])
        _run(["git", "-C", str(builder), "add", "-A"])
        _run(["git", "-C", str(builder), "commit", "-qm", "canonical peel"])
        _copy_authorized(bank, builder, authorized)
        # Cargo.lock is the sole generated-path exception and is intentionally
        # ignored by the canonical workspace. Force-staging remains bounded by
        # the already validated authorized path set.
        _run(["git", "-C", str(builder), "add", "-f", "-A", "--", *authorized])
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        _run(
            ["git", "-C", str(builder), "diff", "--cached", "--binary",
             "--full-index", "HEAD", "--", *authorized],
            stdout=patch_path,
        )
        if not patch_path.stat().st_size:
            raise NextPackageError("bank produced an empty cumulative patch")
        shutil.copytree(canonical, replay, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        _run(["git", "-C", str(replay), "apply", "--check", str(patch_path)])
        _run(["git", "-C", str(replay), "apply", str(patch_path)])
        replay_tree = provenance.source_tree_receipt(replay / project_rel)["tree_hash"]
        if replay_tree != expected_tree:
            raise NextPackageError(
                f"cumulative replay tree mismatch: {replay_tree} != {expected_tree}"
            )
    receipt = {
        "schema_version": 1,
        "kind": "trusted_core_supervisor_cumulative_replay",
        "okay": True,
        "patch_sha256": _sha(patch_path),
        "patch_bytes": patch_path.stat().st_size,
        "tree_hash": expected_tree,
        "authorized_paths": authorized,
        "changed_paths": changed,
    }
    receipt["receipt_id"] = provenance.receipt_id(receipt)
    return receipt


def updated_registration(
    current: dict[str, Any], *, current_sha: str, promotion: dict[str, Any],
    terminal: dict[str, Any], campaign: dict[str, Any], patch_sha: str,
    replay_sha: str,
) -> dict[str, Any]:
    budget = current.get("budget") or {}
    cost = campaign.get("recorded_cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool):
        raise NextPackageError("campaign state lacks numeric recorded cost")
    ceiling = budget.get("max_cost_usd")
    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool):
        raise NextPackageError("registration lacks numeric cost ceiling")
    tree = (promotion.get("final_tree_receipt") or {}).get("tree_hash")
    # Leg-local amendments are historical facts about the superseded
    # registration. Carrying them forward would leave stale "next tree" and
    # cost statements beside the new authoritative predecessor block. The
    # supersedes hash retains them without duplicating contradictory prose.
    registered = {
        key: value for key, value in current.items()
        if key not in _LEG_LOCAL_AMENDMENTS
    }
    registered["supersedes_registration_sha256"] = current_sha
    registered["predecessor_accounting"] = {
        "promotion_receipt_id": promotion["receipt_id"],
        "terminal_validation_receipt_id": terminal["receipt_id"],
        "campaign_state_receipt_id": campaign["receipt_id"],
        "bank_tree_hash": tree,
        "accounted_cost_usd": cost,
        "available_headroom_usd": float(ceiling) - float(cost),
        "bank_seed_patch_sha256": patch_sha,
        "bank_replay_validation_sha256": replay_sha,
        "generated_replay_paths": ["Cargo.lock"],
        **({"oauth_evidence_sha256": (current.get("predecessor_accounting") or {}).get(
            "oauth_evidence_sha256"
        )} if (current.get("predecessor_accounting") or {}).get(
            "oauth_evidence_sha256"
        ) else {}),
    }
    registered["supervisor_amendment"] = {
        "schema_version": 1,
        "policy": "durable_reset_aware_resume_and_mechanical_bank_chaining",
        # Registered retry policy must state what the supervisor actually
        # does — a hand-copied subset had already drifted (T315). Single
        # source: lib.taxonomy, same body the supervisor imports.
        "same_package_retry": sorted(taxonomy.AUTORETRY_END_REASONS),
        "bank_continuation": "canonical peel to exact reusable bank cumulative patch; independent replay required",
        "fail_closed": True,
        "review_thread": "AGENT_DEBATE.md:T311",
    }
    registered["purpose"] = (
        "Autonomous Trust Core continuation from a reusable BANKED_PARTIAL "
        f"tree {tree}; provider-free successor ceremony under T311."
    )
    return registered


def updated_package(
    current: dict[str, Any], *, registration: Path, patch: Path,
    promotion: Path, terminal: Path, campaign: Path, replay: Path,
) -> dict[str, Any]:
    argv = list(current["launch_argv"])
    argv = _replace_option(argv, "--launch-registration", str(registration))
    seed_roles = {
        "--seed-wip": patch,
        "--seed-receipt": promotion,
        "--predecessor-terminal": terminal,
        "--campaign-state": campaign,
    }
    for option, value in seed_roles.items():
        argv = _upsert_option(argv, option, str(value))
    replacements = {"--launch-registration": registration, **seed_roles}
    role_paths = {
        Path(previous_value)
        for option in replacements
        if (previous_value := _optional_option(
            current["launch_argv"], option,
        )) is not None
    }
    new_role_paths = set(replacements.values()) | {replay}
    preserved = [
        Path(entry["path"])
        for entry in current["immutable_inputs"]
        if Path(entry["path"]) not in role_paths
    ]
    # Preserved inputs were hash-verified at launch time, possibly hours ago.
    # Re-pinning them from current disk without comparing would silently bless
    # any in-between mutation with a plausible successor package (T315 M5) —
    # carry the registered digest forward only if the bytes still match it.
    registered_sha = {
        Path(entry["path"]): entry["sha256"]
        for entry in current["immutable_inputs"]
    }
    # Hash each path EXACTLY ONCE and pin the digest that was verified.
    # Verifying one read and pinning a second read leaves a window in which a
    # concurrent writer's mutation passes the check and is then blessed into
    # the successor package as immutable — the very substitution this guard
    # exists to stop.
    digests: dict[Path, str] = {}
    for path in preserved:
        actual = _sha(path)
        if actual != registered_sha[path]:
            raise NextPackageError(
                f"preserved immutable input changed since registration: "
                f"{path} registered {registered_sha[path][:16]} != "
                f"current {actual[:16]}"
            )
        digests[path] = actual
    for path in new_role_paths:
        # NOT setdefault: its default argument is evaluated eagerly, so an
        # overlapping path (a preserved input reused as a new role) would be
        # read a second time — reintroducing the verify-once/pin-twice window
        # this loop exists to close.
        if path not in digests:
            digests[path] = _sha(path)
    inputs = [
        {"path": str(path), "sha256": digests[path]}
        for path in sorted(set(preserved) | new_role_paths, key=str)
    ]
    updated = {**current, "launch_argv": argv, "immutable_inputs": inputs}
    return {
        **updated,
        "package_id": provenance.supervisor_package_id(updated),
    }


def generate(args: argparse.Namespace) -> None:
    current_path = args.current_package.resolve()
    current_package = _json(current_path)
    continuation = current_package.get("continuation") or {}
    required = (
        "repo_root", "gitroot", "ref", "manifest", "depth", "project_rel",
        "root_start_envelope", "staging_root", "terminal_validation_path",
    )
    if any(not continuation.get(key) for key in required):
        raise NextPackageError("package lacks complete continuation configuration")
    values = {"run_id": args.run_id}
    result_path = Path(current_package["result_path"].format_map(values))
    result = _json(result_path)
    promotion_path = result_path.with_name("promotion_receipt.json")
    promotion = _json(promotion_path)
    embedded_promotion = dict(result.get("promotion_receipt") or {})
    embedded_promotion.pop("receipt_path", None)
    if promotion != embedded_promotion:
        raise NextPackageError("promotion file differs from terminal result")
    if promotion.get("receipt_id") != provenance.receipt_id(promotion):
        raise NextPackageError("promotion receipt content ID mismatch")
    if promotion.get("decision") != "BANKED_PARTIAL" or (
        promotion.get("terminal_disposition") or {}
    ).get("reusable") is not True:
        raise NextPackageError("terminal promotion is not reusable BANKED_PARTIAL")
    _validate_bank_frontier(promotion)
    terminal_path = Path(continuation["terminal_validation_path"].format_map(values))
    campaign_path = Path(current_package["campaign_state_path"].format_map(values))
    terminal = _json(terminal_path)
    campaign = _json(campaign_path)
    _content_receipt(terminal, "trusted_core_terminal_validation")
    _content_receipt(campaign, "trusted_core_campaign_state")
    if terminal.get("promotion_receipt_id") != promotion["receipt_id"]:
        raise NextPackageError("terminal does not bind promotion")
    legs = campaign.get("legs") or []
    if not legs or legs[-1].get("terminal_receipt_id") != terminal["receipt_id"]:
        raise NextPackageError("campaign state does not bind terminal")
    if campaign.get("stop") is True:
        raise NextPackageError("campaign state requires stop")

    repo_root = Path(continuation["repo_root"])
    manifest = Path(continuation["manifest"])
    campaign_spec = Path(_option(current_package["launch_argv"], "--campaign-spec"))
    registration_path = Path(_option(current_package["launch_argv"], "--launch-registration"))
    bank = Path(continuation["bank_workspace_path"].format_map(values))
    project_rel = Path(continuation["project_rel"])
    actual_tree = provenance.source_tree_receipt(bank / project_rel)["tree_hash"]
    expected_tree = (promotion.get("final_tree_receipt") or {}).get("tree_hash")
    if actual_tree != expected_tree:
        raise NextPackageError("bank workspace differs from promotion authority")
    manifest_json = _json(manifest)
    authorized = _manifest_authorized_paths(manifest_json)

    staging = Path(continuation["staging_root"]) / f"auto-{args.run_id}"
    if staging.exists():
        raise NextPackageError(f"successor staging collision: {staging}")
    staging.mkdir(parents=True)
    try:
        with tempfile.TemporaryDirectory(prefix="tc-next-peel-") as temporary:
            canonical = Path(temporary) / "canonical"
            peel_json = staging / "canonical_peel.json"
            try:
                _run([
                    "python3", str(repo_root / "peel.py"), "--worktree", str(canonical),
                    "--gitroot", continuation["gitroot"], "--ref", continuation["ref"],
                    "--manifest", str(manifest), "--depth", str(continuation["depth"]),
                    "--require-exact-transform",
                ], stdout=peel_json)
                replay_start = staging / "replayed_start_envelope.json"
                _run([
                    "python3", str(repo_root / "trusted_core_profile.py"), "prepare-start",
                    "--campaign", str(campaign_spec), "--manifest", str(manifest),
                    "--peel-json", str(peel_json), "--project", str(canonical / project_rel),
                    "--repo-root", str(repo_root), "--out", str(replay_start),
                ])
                root_validation = staging / "root_replay_validation.json"
                _run([
                    "python3", str(repo_root / "trusted_core_profile.py"), "validate-root-replay",
                    "--authority", continuation["root_start_envelope"],
                    "--replay", str(replay_start), "--out", str(root_validation),
                ])
                patch_path = staging / "canonical_peel_to_bank.patch"
                replay = build_cumulative_patch(
                    canonical=canonical, bank=bank, project_rel=project_rel,
                    authorized=authorized, expected_tree=expected_tree, patch_path=patch_path,
                )
            finally:
                subprocess.run([
                    "python3", str(repo_root / "peel.py"), "--worktree", str(canonical),
                    "--gitroot", continuation["gitroot"], "--remove",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        replay_path = staging / "cumulative_replay_validation.json"
        provenance.write_immutable_json(replay_path, replay)
        copied_promotion = staging / "seed_promotion_receipt.json"
        copied_terminal = staging / "terminal_validation.json"
        copied_campaign = staging / "campaign_state.json"
        for source, target in (
            (promotion_path, copied_promotion), (terminal_path, copied_terminal),
            (campaign_path, copied_campaign),
        ):
            shutil.copy2(source, target)
        current_registration = _json(registration_path)
        registration = updated_registration(
            current_registration, current_sha=_sha(registration_path),
            promotion=promotion, terminal=terminal, campaign=campaign,
            patch_sha=_sha(patch_path), replay_sha=_sha(replay_path),
        )
        next_registration = staging / "launch_registration.json"
        provenance.write_immutable_json(next_registration, registration)
        package = updated_package(
            current_package, registration=next_registration, patch=patch_path,
            promotion=copied_promotion, terminal=copied_terminal,
            campaign=copied_campaign, replay=replay_path,
        )
        provenance.write_immutable_json(args.next_package, package)
        for path in staging.iterdir():
            path.chmod(0o444)
        staging.chmod(0o555)
        args.next_package.chmod(0o444)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            args.next_package.unlink()
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-package", type=Path, required=True)
    parser.add_argument("--next-package", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        generate(_parser().parse_args(argv))
    except (NextPackageError, ValueError, RuntimeError) as exc:
        print(f"trusted-core next-package error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
