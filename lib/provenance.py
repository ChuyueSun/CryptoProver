"""Content-addressed source and verifier receipts.

The harness has to distinguish an identical candidate from a fresh source
state.  File names and result-directory timing are not enough: a later seed or
reset must be able to prove that the package gate it cites ran against the same
workspace bytes.  This module is deliberately stdlib-only so both ``run.py``
and the host-side Docker launcher can use one canonical algorithm.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable


_ALWAYS_IGNORED_DIRS = frozenset({
    ".git", "__pycache__", ".mypy_cache", ".pytest_cache",
})
_CARGO_OUTPUT_DIRS = frozenset({
    "target", "results", "claude_raw", "claude_memory",
})
_IGNORED_FILES = frozenset({".DS_Store", ".git"})
_LITERAL_RUST_INCLUDE = re.compile(
    r"\b(?:include|include_str|include_bytes)!\s*\(\s*\"([^\"]+)\"\s*\)"
)
_LITERAL_RUST_INCLUDE_RAW = re.compile(
    r"\b(?:include|include_str|include_bytes)!\s*\(\s*r(?P<hashes>#{0,255})"
    r"\"(?P<path>[^\"]+)\"(?P=hashes)\s*\)"
)
_LITERAL_RUST_PATH = re.compile(
    r"#\s*\[\s*path\s*=\s*\"([^\"]+)\"\s*\]"
)
_LITERAL_RUST_PATH_RAW = re.compile(
    r"#\s*\[\s*path\s*=\s*r(?P<hashes>#{0,255})"
    r"\"(?P<path>[^\"]+)\"(?P=hashes)\s*\]"
)
PEEL_TRANSFORM_TOOL_PATHS = (
    "peel.py",
    "admit.py",
    "strip_specs.py",
    "lib/admits.py",
    "lib/provenance.py",
)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical encoding used for content-addressed receipts."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def receipt_id(value: dict[str, Any]) -> str:
    """Content ID for a receipt before location-specific metadata is attached."""
    payload = dict(value)
    payload.pop("receipt_path", None)
    payload.pop("receipt_id", None)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def owner_queue_sort_key(item: dict[str, Any]) -> tuple:
    """The single canonical ordering for owner-queue entries."""
    return (-item["count"], item["file"], item["kind"])


def is_canonical_owner_queue(owner_queue: list[dict[str, Any]]) -> bool:
    """Whether a queue already carries the canonical owner-queue ordering.

    Validators must call this instead of re-encoding the sort key inline —
    a divergent inline copy would reject queues this module itself produced,
    bricking continuation handoffs on a tie-break tweak.
    """
    return owner_queue == sorted(owner_queue, key=owner_queue_sort_key)


def diagnostic_owner_queue(
    diagnostic_inventory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mechanically aggregate diagnostics by owner with canonical ordering."""
    counts: dict[tuple[str, str], int] = {}
    for item in diagnostic_inventory:
        if not isinstance(item, dict):
            raise ValueError("diagnostic inventory is malformed")
        owner = str(item.get("file") or "<build-wrapper>")
        kind = str(item.get("kind") or "unknown")
        counts[(owner, kind)] = counts.get((owner, kind), 0) + 1
    return sorted(
        (
            {"file": owner, "kind": kind, "count": count}
            for (owner, kind), count in counts.items()
        ),
        key=owner_queue_sort_key,
    )


def peel_transform_tool_hashes(repo_root: Path) -> dict[str, str]:
    """Hash every implementation file that can affect a peel transform."""
    return {
        rel: sha256_file(repo_root / rel)
        for rel in PEEL_TRANSFORM_TOOL_PATHS
    }


def validate_campaign_spec(
    spec_path: Path,
    *,
    repo_root: Path,
    require_launch_budget: bool = False,
) -> dict[str, Any]:
    """Validate the fixed Trust Core identity against the current repository.

    Runtime-only bindings (image, final harness commit, proxy policy, and user
    budget authorization) intentionally live in the launch registration.  This
    function rejects drift in the fixed scientific task before any peel or
    model invocation can occur.
    """
    try:
        spec = json.loads(spec_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid campaign spec: {spec_path}") from exc
    if spec.get("schema_version") != 1 or spec.get("status") != "pre_registered":
        raise ValueError("campaign spec must be pre_registered schema_version 1")
    task = spec.get("task")
    if not isinstance(task, dict):
        raise ValueError("campaign spec lacks task object")
    manifest_rel = task.get("manifest_path")
    if not isinstance(manifest_rel, str) or not manifest_rel:
        raise ValueError("campaign spec lacks task.manifest_path")
    manifest_path = repo_root / manifest_rel
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid campaign manifest: {manifest_path}") from exc
    actual_sha = sha256_file(manifest_path)
    if actual_sha != task.get("manifest_sha256"):
        raise ValueError(
            f"campaign manifest hash mismatch: expected "
            f"{task.get('manifest_sha256')} actual {actual_sha}"
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("campaign manifest lacks files array")
    lemma_count = sum(
        len(entry.get("lemmas", ()))
        for entry in files if isinstance(entry, dict)
    )
    expected_file_count = task.get("manifest_file_count")
    expected_lemma_count = task.get("deleted_named_lemma_count")
    if len(files) != expected_file_count:
        raise ValueError(
            f"campaign manifest file census mismatch: expected "
            f"{expected_file_count} actual {len(files)}"
        )
    if lemma_count != expected_lemma_count:
        raise ValueError(
            f"campaign lemma census mismatch: expected "
            f"{expected_lemma_count} actual {lemma_count}"
        )
    identity_pairs = (
        ("peel_depth", "depth"),
        ("pin", "pin"),
        ("experiment_mode", "experiment_mode"),
    )
    for task_key, manifest_key in identity_pairs:
        if task.get(task_key) != manifest.get(manifest_key):
            raise ValueError(
                f"campaign {task_key} mismatch: expected "
                f"{task.get(task_key)!r} manifest "
                f"{manifest.get(manifest_key)!r}"
            )
    stopping = spec.get("stopping_policy") or {}
    unresolved_budget = [
        name for name in ("k", "max_cost_usd", "max_wall_seconds")
        if stopping.get(name) is None
    ]
    if require_launch_budget and unresolved_budget:
        raise ValueError(
            "campaign budget is not authorized: missing "
            + ", ".join(unresolved_budget)
        )
    for field in ("expected_pre_tree_hash", "expected_post_peel_tree_hash"):
        value = task.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(
            char not in "0123456789abcdef" for char in value
        ):
            raise ValueError(f"campaign task.{field} must be a SHA-256")
    return {
        "campaign_id": spec.get("campaign_id"),
        "campaign_spec_sha256": sha256_file(spec_path),
        "manifest_path": str(manifest_path),
        "manifest_sha256": actual_sha,
        "manifest_file_count": len(files),
        "deleted_named_lemma_count": lemma_count,
        "source_ref": task.get("source_ref"),
        "source_commit": task.get("source_commit"),
        "source_tree": task.get("source_tree"),
        "expected_pre_tree_hash": task.get("expected_pre_tree_hash"),
        "expected_post_peel_tree_hash": task.get(
            "expected_post_peel_tree_hash"
        ),
        "unresolved_budget_fields": unresolved_budget,
    }


def validate_peel_start_receipt(
    receipt: dict[str, Any],
    *,
    manifest_path: Path,
    project: Path,
    tool_root: Path,
    expected_source_commit: str | None = None,
    expected_source_tree: str | None = None,
    expected_pre_tree_hash: str | None = None,
    expected_post_tree_hash: str | None = None,
) -> dict[str, Any]:
    """Validate a canonical fresh peel against current bytes and fixed inputs.

    This validator intentionally reconstructs the expected transform census
    from the manifest instead of trusting the receipt's summary.  It then
    re-hashes the current workspace, so a valid old receipt beside modified
    source is rejected before model invocation.
    """
    if receipt.get("schema_version") != 3:
        raise ValueError("peel start receipt must use schema_version 3")
    if receipt.get("kind") != "canonical_peel_start":
        raise ValueError("receipt is not a canonical_peel_start")
    if receipt.get("receipt_id") != receipt_id(receipt):
        raise ValueError("peel start receipt_id mismatch")

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid peel manifest: {manifest_path}") from exc
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("peel manifest must contain a non-empty files array")
    manifest_paths = [
        entry.get("path") for entry in files if isinstance(entry, dict)
    ]
    if len(manifest_paths) != len(files) or any(
        not isinstance(path, str) or not path for path in manifest_paths
    ):
        raise ValueError("peel manifest contains an invalid file entry")
    if len(set(manifest_paths)) != len(manifest_paths):
        raise ValueError("peel manifest contains duplicate file paths")

    transform = receipt.get("transform") or {}
    actual_manifest_sha = sha256_file(manifest_path)
    if transform.get("manifest_sha256") != actual_manifest_sha:
        raise ValueError("peel receipt manifest hash mismatch")
    expected_tools = peel_transform_tool_hashes(tool_root)
    if transform.get("tool_sha256") != expected_tools:
        raise ValueError("peel transform tool hash mismatch")
    if transform.get("exact_required") is not True:
        raise ValueError("peel receipt was not built in exact mode")
    if transform.get("errors") != []:
        raise ValueError("peel receipt contains transform errors")
    if receipt.get("frozen_surface_unchanged") is not True:
        raise ValueError("peel receipt reports a changed frozen surface")
    if receipt.get("post_seal_tree_unchanged") is not True:
        raise ValueError("peel receipt lacks post-seal byte equality")
    if receipt.get("sealed_worktree_clean") is not True:
        raise ValueError("peeled worktree was not clean after sealing")
    if (
        manifest.get("depth") is not None
        and transform.get("depth") != manifest.get("depth")
    ):
        raise ValueError("peel receipt depth does not match manifest")
    if (
        manifest.get("pin") is not None
        and transform.get("pin") != manifest.get("pin")
    ):
        raise ValueError("peel receipt pin does not match manifest")
    if transform.get("experiment_mode") != manifest.get("experiment_mode"):
        raise ValueError("peel receipt experiment mode does not match manifest")

    editable = transform.get("editable_files")
    changed = transform.get("changed_files")
    if editable != manifest_paths:
        raise ValueError("peel receipt editable-file order/census mismatch")
    if sorted(changed or []) != sorted(manifest_paths):
        raise ValueError("peel receipt changed-file census mismatch")

    peeled = transform.get("peeled")
    if not isinstance(peeled, list):
        raise ValueError("peel receipt lacks observed per-file transforms")
    observed_by_path: dict[str, dict[str, Any]] = {}
    for item in peeled:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise ValueError("peel receipt contains an invalid file transform")
        if item["file"] in observed_by_path:
            raise ValueError("peel receipt contains duplicate file transforms")
        observed_by_path[item["file"]] = item
    if set(observed_by_path) != set(manifest_paths):
        raise ValueError("peel receipt per-file census mismatch")

    depth = transform.get("depth")
    for spec in files:
        path = spec["path"]
        observed = observed_by_path[path]
        if observed.get("changed") is not True:
            raise ValueError(f"{path}: receipt says editable file did not change")
        expected_deleted: list[str] = []
        if isinstance(depth, int) and depth >= 2:
            expected_deleted.extend(spec.get("lemmas", ()))
        if isinstance(depth, int) and depth >= 3:
            expected_deleted.extend(spec.get("spec_fns", ()))
        if sorted(observed.get("deleted") or []) != sorted(expected_deleted):
            raise ValueError(f"{path}: observed deletion census mismatch")
        expected_contracts = (
            list(spec.get("contract_fns", ()))
            if isinstance(depth, int) and depth >= 4 else []
        )
        if sorted(observed.get("stripped") or []) != sorted(expected_contracts):
            raise ValueError(f"{path}: observed contract-strip census mismatch")
        proof_op = observed.get("proof_op")
        if isinstance(depth, int) and depth >= 1 and proof_op == "strip":
            expected_proofs = list(spec.get("strip_proof_fns", ()))
            if sorted(observed.get("proof_stripped") or []) != sorted(
                expected_proofs
            ):
                raise ValueError(f"{path}: observed proof-strip census mismatch")
        if (
            isinstance(depth, int)
            and depth >= 1
            and proof_op == "strip-all"
            and not observed.get("proof_stripped")
        ):
            raise ValueError(f"{path}: strip-all removed no proof content")

    source = receipt.get("source") or {}
    source_checks = (
        ("resolved_commit", expected_source_commit),
        ("resolved_tree", expected_source_tree),
    )
    for field, expected in source_checks:
        if expected is not None and source.get(field) != expected:
            raise ValueError(
                f"peel source {field} mismatch: expected {expected} "
                f"actual {source.get(field)}"
            )
    pre_tree = source.get("pre_tree_receipt") or {}
    if (
        expected_pre_tree_hash is not None
        and pre_tree.get("tree_hash") != expected_pre_tree_hash
    ):
        raise ValueError("peel pre-transform source-tree hash mismatch")

    recorded_post = receipt.get("post_tree_receipt") or {}
    current_post = source_tree_receipt(project)
    current_post.pop("workspace_root", None)
    if recorded_post != current_post:
        raise ValueError("current source bytes do not match peel start receipt")
    if (
        expected_post_tree_hash is not None
        and current_post.get("tree_hash") != expected_post_tree_hash
    ):
        raise ValueError("peeled source-tree hash does not match registration")
    root = workspace_root(project)
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^{commit}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        head_tree = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        parents = subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%P", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError("cannot validate sealed peeled Git state") from exc
    if head != receipt.get("sealed_head"):
        raise ValueError("current HEAD does not match sealed peel receipt")
    if head_tree != receipt.get("sealed_head_tree"):
        raise ValueError("current Git tree does not match sealed peel receipt")
    if parents:
        raise ValueError("sealed peel HEAD is not a parentless commit")
    if status:
        raise ValueError("peeled worktree is no longer clean")

    return {
        "receipt_id": receipt["receipt_id"],
        "manifest_sha256": actual_manifest_sha,
        "source_commit": source.get("resolved_commit"),
        "source_tree": source.get("resolved_tree"),
        "pre_tree_hash": pre_tree.get("tree_hash"),
        "post_tree_hash": current_post.get("tree_hash"),
        "editable_file_count": len(editable),
        "changed_file_count": len(changed),
        "observed_deleted_name_count": sum(
            len(item.get("deleted") or []) for item in peeled
        ),
        "observed_proof_strip_count": sum(
            len(item.get("proof_stripped") or []) for item in peeled
        ),
    }


def validated_peel_start_envelope(
    receipt: dict[str, Any],
    *,
    manifest_path: Path,
    project: Path,
    tool_root: Path,
    campaign_spec_path: Path | None = None,
) -> dict[str, Any]:
    """Create the immutable pre-model boundary for a fresh peeled start."""
    campaign: dict[str, Any] | None = None
    expected: dict[str, Any] = {}
    if campaign_spec_path is not None:
        campaign = validate_campaign_spec(
            campaign_spec_path, repo_root=tool_root,
        )
        expected = {
            "expected_source_commit": campaign["source_commit"],
            "expected_source_tree": campaign["source_tree"],
            "expected_pre_tree_hash": campaign["expected_pre_tree_hash"],
            "expected_post_tree_hash": campaign[
                "expected_post_peel_tree_hash"
            ],
        }
        if Path(campaign["manifest_path"]).resolve() != manifest_path.resolve():
            raise ValueError("campaign and launcher use different manifests")
    validation = validate_peel_start_receipt(
        receipt,
        manifest_path=manifest_path,
        project=project,
        tool_root=tool_root,
        **expected,
    )
    if campaign is not None:
        if (
            validation["editable_file_count"]
            != campaign["manifest_file_count"]
        ):
            raise ValueError("campaign editable-file census mismatch")
        if (
            validation["observed_deleted_name_count"]
            != campaign["deleted_named_lemma_count"]
        ):
            raise ValueError("campaign observed deletion census mismatch")
        try:
            campaign_json = json.loads(campaign_spec_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("campaign spec became unreadable") from exc
        expected_proofs = (campaign_json.get("task") or {}).get(
            "stripped_proof_function_count"
        )
        if validation["observed_proof_strip_count"] != expected_proofs:
            raise ValueError("campaign observed proof-strip census mismatch")
    envelope = {
        "schema_version": 1,
        "kind": "validated_peel_start",
        "campaign": campaign,
        "transform_receipt": receipt,
        "validation": validation,
    }
    envelope["receipt_id"] = receipt_id(envelope)
    return envelope


def validate_launch_registration(
    registration_path: Path,
    *,
    campaign: dict[str, Any],
    actual_image_digest: str | None = None,
    actual_harness_commit: str | None = None,
    actual_harness_tree_sha256: str | None = None,
    actual_proxy_policy_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate the immutable run-specific bindings for a scored campaign."""
    try:
        value = json.loads(registration_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid launch registration: {registration_path}") from exc
    if value.get("schema_version") != 1 or value.get("scoreable") is not True:
        raise ValueError("launch registration must be scoreable schema_version 1")
    expected = {
        "campaign_id": campaign.get("campaign_id"),
        "campaign_spec_sha256": campaign.get("campaign_spec_sha256"),
        "manifest_sha256": campaign.get("manifest_sha256"),
    }
    for key, wanted in expected.items():
        if value.get(key) != wanted:
            raise ValueError(
                f"launch registration {key} mismatch: "
                f"expected {wanted!r} actual {value.get(key)!r}"
            )
    runtime_expected = {
        "container_image_digest": actual_image_digest,
        "harness_git_commit": actual_harness_commit,
        "harness_source_tree_sha256": actual_harness_tree_sha256,
        "provider_proxy_policy_sha256": actual_proxy_policy_sha256,
    }
    for key, actual in runtime_expected.items():
        registered = value.get(key)
        if not isinstance(registered, str) or not registered:
            raise ValueError(f"launch registration lacks {key}")
        if actual is not None and registered != actual:
            raise ValueError(
                f"launch registration {key} mismatch: "
                f"registered {registered!r} actual {actual!r}"
            )
    validate_credential_identity(value)
    budget = value.get("budget")
    if not isinstance(budget, dict):
        raise ValueError("launch registration lacks budget")
    authorization_id = budget.get("authorization_id")
    if not isinstance(authorization_id, str) or not authorization_id:
        raise ValueError("launch registration lacks budget.authorization_id")
    for key in ("max_cost_usd", "max_wall_seconds"):
        amount = budget.get(key)
        if (
            not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or not math.isfinite(amount)
            or amount <= 0
        ):
            raise ValueError(f"launch registration budget.{key} must be positive")
    plateau_k = budget.get("plateau_k")
    if (
        not isinstance(plateau_k, int)
        or isinstance(plateau_k, bool)
        or plateau_k <= 0
    ):
        raise ValueError(
            "launch registration budget.plateau_k must be a positive integer"
        )
    hint_level = value.get("hint_level")
    if hint_level not in {"H0", "H1", "H2", "H3", "H4"}:
        raise ValueError("launch registration hint_level must be H0-H4")
    if hint_level in {"H2", "H3", "H4"} and not value.get("hint_authorization_id"):
        raise ValueError(f"launch registration {hint_level} requires hint_authorization_id")
    execution = value.get("execution")
    if not isinstance(execution, dict):
        raise ValueError("launch registration lacks execution policy")
    if execution.get("agent_backend") != "claude":
        raise ValueError("launch registration execution.agent_backend must be claude")
    if not isinstance(execution.get("model"), str) or not execution["model"]:
        raise ValueError("launch registration execution.model must be explicit")
    positive_ints = (
        "rounds",
        "cargo_jobs",
        "cpu_shares",
        "max_parallel",
        "agent_max_turns",
    )
    for key in positive_ints:
        amount = execution.get(key)
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError(
                f"launch registration execution.{key} must be a positive integer"
            )
    for key in ("max_task_minutes", "verus_rlimit"):
        amount = execution.get(key)
        if (
            not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or not math.isfinite(amount)
            or amount <= 0
        ):
            raise ValueError(
                f"launch registration execution.{key} must be positive"
            )
    if not isinstance(execution.get("memory"), str) or not execution["memory"]:
        raise ValueError("launch registration execution.memory must be explicit")
    if not isinstance(execution.get("experiment_mode"), str) or not execution[
        "experiment_mode"
    ]:
        raise ValueError(
            "launch registration execution.experiment_mode must be explicit"
        )
    return {
        **value,
        "registration_sha256": sha256_file(registration_path),
        "registration_id": receipt_id(value),
    }


def validate_credential_identity(
    registration: dict[str, Any],
    credential: str | None = None,
) -> dict[str, str]:
    """Validate, and optionally match, the scored OAuth credential binding."""
    identity = registration.get("credential_identity")
    if not isinstance(identity, dict):
        raise ValueError("launch registration lacks credential_identity")
    expected = {
        "kind": "claude_code_oauth",
        "algorithm": "sha256-salt-token-v1",
    }
    for key, wanted in expected.items():
        if identity.get(key) != wanted:
            raise ValueError(
                f"launch registration credential_identity.{key} must be "
                f"{wanted!r}"
            )
    salt = identity.get("salt_hex")
    digest = identity.get("digest")
    if (
        not isinstance(salt, str)
        or not re.fullmatch(r"[0-9a-f]{64}", salt)
    ):
        raise ValueError(
            "launch registration credential_identity.salt_hex must be "
            "64 lowercase hex characters"
        )
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ValueError(
            "launch registration credential_identity.digest must be "
            "64 lowercase hex characters"
        )
    if credential is not None:
        observed = hashlib.sha256(
            bytes.fromhex(salt) + credential.encode()
        ).hexdigest()
        if not hmac.compare_digest(observed, digest):
            raise ValueError(
                "launch credential does not match registered identity"
            )
    return {
        "kind": identity["kind"],
        "algorithm": identity["algorithm"],
        "salt_hex": salt,
        "digest": digest,
    }


def derive_lineage_id(campaign_spec_sha256: str, start_receipt_id: str) -> str:
    return hashlib.sha256(
        f"trusted-core-lineage-v1\0{campaign_spec_sha256}\0{start_receipt_id}".encode()
    ).hexdigest()


def reusable_seed_authority(receipt_path: Path) -> dict[str, Any]:
    """Validate a COMPLETE or BANKED_PARTIAL scored predecessor receipt."""
    try:
        value = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid promotion receipt: {receipt_path}") from exc
    stored_id = value.get("receipt_id")
    if not isinstance(stored_id, str) or stored_id != receipt_id(value):
        raise ValueError("promotion receipt content ID mismatch")
    if value.get("schema_version") != 2 or value.get("scoreable") is not True:
        raise ValueError("promotion receipt is not a scoreable schema_version 2 receipt")
    decision = value.get("decision")
    disposition = value.get("terminal_disposition") or {}
    if decision == "ACCEPTED":
        allowed_state = "ACCEPTED"
        kind = "COMPLETE"
        gate = value.get("acceptance_gate_receipt") or {}
    elif decision == "BANKED_PARTIAL":
        allowed_state = "BANKED_PARTIAL"
        kind = "BANKED_PARTIAL"
        gate = value.get("banking_gate_receipt") or {}
    else:
        raise ValueError("promotion receipt is neither ACCEPTED nor BANKED_PARTIAL")
    if disposition.get("state") != allowed_state or not disposition.get("reusable"):
        raise ValueError("promotion receipt lacks a reusable terminal disposition")
    tree = value.get("final_tree_receipt") or {}
    tree_hash = tree.get("tree_hash")
    if not isinstance(tree_hash, str) or not tree_hash:
        raise ValueError("promotion receipt lacks final_tree_receipt.tree_hash")
    if (
        gate.get("fresh") is not True
        or gate.get("exact_tree_match") is not True
        or (gate.get("tree_receipt") or {}).get("tree_hash") != tree_hash
    ):
        raise ValueError(f"{decision} lacks a fresh exact-tree terminal gate")
    vector = gate.get("vector")
    required_vector = {
        "hard_admits", "verification_errors", "resource_limits", "raw_errors",
    }
    if (
        not isinstance(vector, dict)
        or not required_vector <= set(vector)
        or any(
            not isinstance(vector[key], int)
            or isinstance(vector[key], bool)
            or vector[key] < 0
            for key in required_vector
        )
    ):
        raise ValueError(f"{decision} lacks a canonical non-negative progress vector")
    if decision == "ACCEPTED" and (
        (gate.get("verus_result") or {}).get("okay") is not True
        or vector.get("hard_admits") != 0
    ):
        raise ValueError("ACCEPTED lacks a fresh green zero-admit terminal gate")
    lineage_id = value.get("lineage_id")
    if not isinstance(lineage_id, str) or not lineage_id:
        raise ValueError("promotion receipt lacks lineage_id")
    campaign_spec_sha256 = value.get("campaign_spec_sha256")
    if not isinstance(campaign_spec_sha256, str) or not campaign_spec_sha256:
        raise ValueError("promotion receipt lacks campaign_spec_sha256")
    return {
        "kind": kind,
        "tree_hash": tree_hash,
        "lineage_id": lineage_id,
        "receipt_id": stored_id,
        "campaign_spec_sha256": campaign_spec_sha256,
    }


def workspace_root(project: Path) -> Path:
    """Return the outermost contiguous Cargo workspace containing ``project``."""
    project = project.resolve()
    found: Path | None = None
    current = project
    while True:
        if (current / "Cargo.toml").is_file():
            found = current
        parent = current.parent
        if parent == current or not (parent / "Cargo.toml").is_file():
            break
        current = parent
    return found or project


def _iter_relevant_files(root: Path) -> Iterable[Path]:
    for current, dirs, names in os.walk(root, followlinks=False):
        base = Path(current)
        dirs[:] = sorted(
            directory for directory in dirs
            if directory not in _ALWAYS_IGNORED_DIRS
            and not (
                directory in _CARGO_OUTPUT_DIRS
                and (base == root or (base / "Cargo.toml").is_file())
            )
        )
        for name in sorted(names):
            if name in _IGNORED_FILES:
                continue
            path = base / name
            if path.is_file() or path.is_symlink():
                yield path


def _literal_rust_include_files(root: Path, initial: Iterable[Path]) -> list[Path]:
    """Return literal Rust include inputs, including those below ignored dirs.

    Build outputs remain excluded from the ordinary walk, but a source file can
    make any byte sequence compiler-visible with include!/include_str!/
    include_bytes!. Those explicit dependencies must therefore enter the exact
    source identity even when their directory is normally generated/noisy.
    Dynamic include expressions are derived from already-receipted source/build
    inputs and are checked by the fresh terminal build; literal paths are the
    direct mutable-input hole this closure prevents.
    """
    pending = list(initial)
    seen = {path.resolve() for path in pending}
    included: list[Path] = []
    while pending:
        source = pending.pop()
        if source.suffix != ".rs" or source.is_symlink():
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        dependencies = [
            *_LITERAL_RUST_INCLUDE.findall(text),
            *_LITERAL_RUST_PATH.findall(text),
            *(match.group("path") for match in _LITERAL_RUST_INCLUDE_RAW.finditer(text)),
            *(match.group("path") for match in _LITERAL_RUST_PATH_RAW.finditer(text)),
        ]
        for raw in dependencies:
            candidate = (source.parent / raw).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"literal Rust include escapes workspace: {source}: {raw}"
                ) from exc
            if not candidate.is_file() or candidate in seen:
                continue
            seen.add(candidate)
            included.append(candidate)
            pending.append(candidate)
    return included


def _file_entry(path: Path, root: Path) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    if path.is_symlink():
        target = os.readlink(path)
        data = target.encode("utf-8", "surrogateescape")
        return {
            "path": rel,
            "kind": "symlink",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return {"path": rel, "kind": "file", "sha256": digest.hexdigest(), "size": size}


def source_tree_receipt(project: Path) -> dict[str, Any]:
    """Return a deterministic receipt for all relevant workspace source bytes.

    Generated build/runtime directories are intentionally excluded; Cargo
    manifests, lockfiles, source, and local build configuration remain covered.
    The manifest lets a reviewer inspect a mismatch without relying on a bare
    opaque hash.
    """
    root = workspace_root(project)
    walked = list(_iter_relevant_files(root))
    explicit_inputs = _literal_rust_include_files(root, walked)
    paths = sorted({*walked, *explicit_inputs}, key=lambda path: path.relative_to(root).as_posix())
    files = [_file_entry(path, root) for path in paths]
    canonical = canonical_json_bytes(files)
    return {
        "schema_version": 1,
        "workspace_root": str(root),
        "file_count": len(files),
        "tree_hash": hashlib.sha256(canonical).hexdigest(),
        "files": files,
        "literal_compiler_inputs": sorted(
            path.relative_to(root).as_posix() for path in explicit_inputs
        ),
    }


def supervisor_package_id(package: dict[str, Any]) -> str:
    """Content identity for a supervisor package, excluding its ID field."""
    content = {key: value for key, value in package.items() if key != "package_id"}
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def gate_signature(command: Iterable[str], *, tool_paths: Iterable[Path] = ()) -> dict[str, Any]:
    """Bind a verifier receipt to exact argv plus the checked tool bytes."""
    tools: list[dict[str, str]] = []
    for path in sorted((Path(p) for p in tool_paths), key=lambda p: str(p)):
        if path.is_file():
            tools.append({"path": str(path.resolve()), "sha256": _file_entry(path, path.parent)["sha256"]})
        else:
            tools.append({"path": str(path), "sha256": "missing"})
    payload = {"argv": [str(part) for part in command], "tools": tools}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "signature": hashlib.sha256(encoded).hexdigest()}


def receipt_key(tree_hash: str, gate_signature_value: str) -> str:
    return hashlib.sha256(f"{tree_hash}\0{gate_signature_value}".encode()).hexdigest()


def write_immutable_json(path: Path, value: Any) -> None:
    """Write a receipt once, or verify an existing receipt is byte-equivalent."""
    payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text() != payload:
            raise RuntimeError(f"immutable receipt collision: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def accepted_promotion_tree_hash(receipt_path: Path) -> str:
    """Return the only tree hash a seed receipt is allowed to promote.

    A rejected/partial candidate has no reusable authority, even when its
    source files happen to be present beside the receipt.  Keep this check in
    the shared primitive so host launchers cannot accidentally reimplement a
    looser JSON interpretation.
    """
    try:
        value = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid promotion receipt: {receipt_path}") from exc
    if value.get("decision") != "ACCEPTED":
        raise ValueError("promotion receipt is not ACCEPTED")
    disposition = value.get("terminal_disposition") or {}
    if disposition.get("state") != "ACCEPTED" or not disposition.get("reusable"):
        raise ValueError("promotion receipt lacks an accepted reusable disposition")
    tree = value.get("final_tree_receipt") or {}
    tree_hash = tree.get("tree_hash")
    if not isinstance(tree_hash, str) or not tree_hash:
        raise ValueError("promotion receipt lacks final_tree_receipt.tree_hash")
    return tree_hash


def _main() -> None:
    parser = argparse.ArgumentParser(description="Compute a canonical workspace source receipt")
    parser.add_argument("project", type=Path, nargs="?")
    parser.add_argument("--tree-hash", action="store_true", help="print only the tree hash")
    parser.add_argument("--validate-campaign", type=Path,
                        help="validate a fixed campaign spec against --repo-root")
    parser.add_argument("--repo-root", type=Path,
                        help="repository root used by --validate-campaign")
    parser.add_argument("--require-launch-budget", action="store_true",
                        help="with --validate-campaign, reject unresolved budget fields")
    parser.add_argument("--accepted-tree-hash", type=Path,
                        help="validate an ACCEPTED promotion receipt and print its tree hash")
    parser.add_argument("--reusable-seed", type=Path,
                        help="validate COMPLETE/BANKED_PARTIAL receipt and print authority JSON")
    parser.add_argument("--validate-peel-start", type=Path,
                        help="validate a peel transform receipt against current bytes")
    parser.add_argument("--manifest", type=Path,
                        help="manifest used with --validate-peel-start")
    parser.add_argument("--tool-root", type=Path,
                        help="harness root used with --validate-peel-start")
    parser.add_argument("--campaign-spec", type=Path,
                        help="optional fixed campaign identity for the peel start")
    parser.add_argument("--receipt-out", type=Path,
                        help="immutably write the validated peel-start envelope")
    args = parser.parse_args()
    if args.validate_campaign is not None:
        if args.project is not None or args.tree_hash or args.accepted_tree_hash or args.reusable_seed:
            parser.error("--validate-campaign cannot be combined with project/tree/seed options")
        if args.repo_root is None:
            parser.error("--validate-campaign requires --repo-root")
        try:
            print(json.dumps(validate_campaign_spec(
                args.validate_campaign,
                repo_root=args.repo_root,
                require_launch_budget=args.require_launch_budget,
            ), indent=2, sort_keys=True))
        except ValueError as exc:
            parser.error(str(exc))
        return
    if args.validate_peel_start is not None:
        if args.project is None:
            parser.error("--validate-peel-start requires project")
        if args.manifest is None or args.tool_root is None:
            parser.error(
                "--validate-peel-start requires --manifest and --tool-root"
            )
        try:
            transform_receipt = json.loads(
                args.validate_peel_start.read_text()
            )
            envelope = validated_peel_start_envelope(
                transform_receipt,
                manifest_path=args.manifest,
                project=args.project,
                tool_root=args.tool_root,
                campaign_spec_path=args.campaign_spec,
            )
            if args.receipt_out is not None:
                write_immutable_json(args.receipt_out, envelope)
            print(json.dumps(envelope, indent=2, sort_keys=True))
        except (
            OSError, json.JSONDecodeError, RuntimeError, ValueError,
        ) as exc:
            parser.error(str(exc))
        return
    if args.reusable_seed is not None:
        if args.project is not None or args.tree_hash or args.accepted_tree_hash or args.repo_root:
            parser.error("--reusable-seed cannot be combined with other modes")
        try:
            print(json.dumps(
                reusable_seed_authority(args.reusable_seed),
                indent=2, sort_keys=True,
            ))
        except ValueError as exc:
            parser.error(str(exc))
        return
    if args.accepted_tree_hash is not None:
        if args.project is not None or args.tree_hash or args.repo_root:
            parser.error("--accepted-tree-hash cannot be combined with a project or --tree-hash")
        try:
            print(accepted_promotion_tree_hash(args.accepted_tree_hash))
        except ValueError as exc:
            parser.error(str(exc))
        return
    if args.project is None:
        parser.error("project is required unless --accepted-tree-hash is used")
    receipt = source_tree_receipt(args.project)
    if args.tree_hash:
        print(receipt["tree_hash"])
    else:
        print(json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    _main()
