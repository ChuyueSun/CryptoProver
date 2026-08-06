#!/usr/bin/env python3
"""Generate a plain-language evidence report for the current state of a Git repo.

The tool never auto-runs commands found in a repository. Checks execute only when
the caller supplies one or more --check arguments.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOOL_VERSION = "0.1"
MAX_CAPTURE_CHARS = 40_000

LANGUAGES = {
    ".py": "Python",
    ".rs": "Rust",
    ".sh": "Shell",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".dfy": "Dafny",
    ".lean": "Lean",
}


def _run(
    argv: list[str] | str,
    *,
    cwd: Path,
    timeout: float = 30,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _git(repo: Path, *args: str, timeout: float = 30) -> str:
    result = _run(["git", *args], cwd=repo, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stdout.strip() or f"git {' '.join(args)} failed")
    return result.stdout.rstrip("\n")


def find_repo(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    if not candidate.exists():
        raise ValueError(f"repository path does not exist: {candidate}")
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=candidate)
    if result.returncode != 0:
        raise ValueError(f"not a Git repository: {candidate}")
    return Path(result.stdout.strip()).resolve()


def parse_porcelain_z(raw: bytes) -> list[dict[str, str]]:
    """Parse `git status --porcelain=v1 -z`, including rename source paths."""
    records = raw.split(b"\0")
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        text = record.decode("utf-8", "surrogateescape")
        if len(text) < 4:
            continue
        xy = text[:2]
        item = {"status": xy, "path": text[3:]}
        if (xy[0] in "RC" or xy[1] in "RC") and index < len(records):
            source = records[index]
            index += 1
            if source:
                item["original_path"] = source.decode("utf-8", "surrogateescape")
        changes.append(item)
    return changes


def git_changes(repo: Path) -> list[dict[str, str]]:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return parse_porcelain_z(result.stdout)


def _file_evidence(repo: Path, change: dict[str, str]) -> dict[str, Any]:
    relative = change["path"]
    path = repo / relative
    evidence: dict[str, Any] = dict(change)
    if not path.exists() and not path.is_symlink():
        evidence.update({"kind": "absent", "sha256": None, "size": 0})
        return evidence
    if path.is_symlink():
        target = os.readlink(path)
        data = target.encode("utf-8", "surrogateescape")
        evidence.update(
            {"kind": "symlink", "target": target, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        )
        return evidence
    if not path.is_file():
        evidence.update({"kind": "other", "sha256": None, "size": 0})
        return evidence
    data = path.read_bytes()
    evidence.update({"kind": "file", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return evidence


def _canonical_hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(data).hexdigest()


def capture_snapshot(repo: Path) -> dict[str, Any]:
    changes = [_file_evidence(repo, change) for change in git_changes(repo)]
    snapshot = {
        "head": _git(repo, "rev-parse", "HEAD"),
        "branch": _git(repo, "branch", "--show-current") or "(detached)",
        "changes": changes,
    }
    snapshot["sha256"] = _canonical_hash(snapshot)
    return snapshot


def _repo_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip())
    return sorted(
        item.decode("utf-8", "surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def _area_role(area: str) -> str:
    known = {
        "(root)": "Top-level commands and orchestration",
        "lib": "Shared implementation used by commands",
        "skills": "Small tools exposed to the proof agent",
        "tests": "Automated checks for repository behavior",
        "docs": "Design and operating documentation",
        "docker": "Execution environment and launch policy",
        ".github": "Repository automation",
        "agent-comm-channel": "Campaign operations and coordination tooling",
    }
    return known.get(area, "Feature or support area inferred from its path")


def _path_role(relative: str) -> str:
    path = Path(relative)
    area = path.parts[0] if len(path.parts) > 1 else "(root)"
    if path.name.startswith("test_") or "tests" in path.parts:
        return "Automated test"
    if path.suffix == ".md":
        return "Documentation"
    if path.suffix == ".sh":
        return "Shell entry point or automation"
    return _area_role(area)


def _entry_priority(item: dict[str, Any]) -> tuple[int, int, str]:
    """Put conventional root commands before incidental executable files."""
    relative = item["path"]
    name = Path(relative).name.lower()
    conventional = {
        "main.py": 0,
        "app.py": 1,
        "cli.py": 2,
        "run.py": 3,
        "index.js": 4,
        "index.ts": 4,
        "launch.sh": 5,
    }
    return (0 if "/" not in relative else 1, conventional.get(name, 20), relative)


def _python_module(relative: str) -> str:
    parts = list(Path(relative).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _python_structure(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return {"definitions": [], "imports": [], "entry_point": False}

    definitions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.append(
                {
                    "name": node.name,
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                }
            )

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)

    entry_point = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in tree.body
    )
    return {"definitions": definitions, "imports": sorted(imports), "entry_point": entry_point}


def _changed_line_ranges(repo: Path, relative: str, status: str) -> list[tuple[int, int]] | None:
    if status == "??":
        return None
    result = _run(["git", "diff", "--unified=0", "HEAD", "--", relative], cwd=repo)
    if result.returncode != 0:
        return []
    ranges: list[tuple[int, int]] = []
    for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", result.stdout, re.MULTILINE):
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count:
            ranges.append((start, start + count - 1))
    return ranges


def _overlaps(definition: dict[str, Any], ranges: list[tuple[int, int]] | None) -> bool:
    if ranges is None:
        return True
    return any(
        definition["line"] <= end and definition["end_line"] >= start
        for start, end in ranges
    )


def _code_scope_exclusion(relative: str) -> str | None:
    parts = Path(relative).parts
    if not parts:
        return None
    root = parts[0]
    common = {".git", ".venv", "venv", "node_modules", "target", "build", "dist", "__pycache__"}
    for part in parts:
        if part in common:
            return part
    if root == "results" or root.startswith("results-"):
        return "results*"
    if root.endswith("_evidence") or root in {"artifacts", "snapshots"}:
        return "evidence/artifact directories"
    return None


def build_code_map(repo: Path, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Infer a compact structural code map without executing repository code."""
    files = _repo_files(repo)
    code_candidates = [relative for relative in files if Path(relative).suffix.lower() in LANGUAGES]
    excluded: dict[str, int] = {}
    code_files = []
    for relative in code_candidates:
        reason = _code_scope_exclusion(relative)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        else:
            code_files.append(relative)
    areas: dict[str, dict[str, Any]] = {}
    languages: dict[str, dict[str, int]] = {}
    python_info: dict[str, dict[str, Any]] = {}

    for relative in code_files:
        path = repo / relative
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            line_count = len(path.read_bytes().splitlines())
        except OSError:
            continue
        suffix = path.suffix.lower()
        language = LANGUAGES[suffix]
        language_item = languages.setdefault(language, {"files": 0, "lines": 0})
        language_item["files"] += 1
        language_item["lines"] += line_count
        area_name = Path(relative).parts[0] if len(Path(relative).parts) > 1 else "(root)"
        area = areas.setdefault(
            area_name,
            {"area": area_name, "role": _area_role(area_name), "files": 0, "lines": 0},
        )
        area["files"] += 1
        area["lines"] += line_count
        if suffix == ".py":
            python_info[relative] = _python_structure(path)

    module_to_path = {
        _python_module(relative): relative
        for relative in python_info
        if _python_module(relative)
    }
    root_module_to_paths: dict[str, list[str]] = {}
    for module, relative in module_to_path.items():
        root_module_to_paths.setdefault(module.split(".")[0], []).append(relative)

    entry_points = []
    for relative, info in python_info.items():
        if not info["entry_point"]:
            continue
        local_dependencies: set[str] = set()
        for imported in info["imports"]:
            exact = module_to_path.get(imported)
            if exact and exact != relative:
                local_dependencies.add(exact)
                continue
            for candidate in root_module_to_paths.get(imported.split(".")[0], []):
                if candidate != relative:
                    local_dependencies.add(candidate)
        entry_points.append(
            {
                "path": relative,
                "role": _path_role(relative),
                "local_dependencies": sorted(local_dependencies)[:8],
            }
        )
    for relative in code_files:
        if Path(relative).suffix.lower() == ".sh":
            entry_points.append(
                {"path": relative, "role": _path_role(relative), "local_dependencies": []}
            )

    change_by_path = {change["path"]: change for change in changes}
    changed_code = []
    for relative in sorted(set(code_files) & set(change_by_path)):
        change = change_by_path[relative]
        symbols: list[str] = []
        info = python_info.get(relative)
        if info:
            ranges = _changed_line_ranges(repo, relative, change["status"])
            symbols = [
                definition["name"]
                for definition in info["definitions"]
                if _overlaps(definition, ranges)
            ]
        changed_code.append(
            {
                "path": relative,
                "change": _change_label(change),
                "role": _path_role(relative),
                "symbols": symbols,
            }
        )

    area_priority = {"(root)": 0, "lib": 1, "skills": 2, "tests": 3, "docker": 4}
    entry_points.sort(key=_entry_priority)
    primary_entry = entry_points[0] if entry_points else None
    support_areas = [
        item for item in sorted(
            areas.values(),
            key=lambda item: (area_priority.get(item["area"], 10), -item["lines"], item["area"]),
        )
        if item["area"] not in {"(root)", "tests"}
    ][:2]
    story_steps: list[dict[str, str]] = []
    if primary_entry:
        story_steps.append(
            {
                "label": "START",
                "title": primary_entry["path"],
                "explanation": "A person or automation can start the repository here.",
            }
        )
        dependencies = primary_entry["local_dependencies"]
        if dependencies:
            story_steps.append(
                {
                    "label": "DELEGATE",
                    "title": f"{len(dependencies)} local modules",
                    "explanation": "The entry point imports " + ", ".join(dependencies[:4]) + ".",
                }
            )
    if support_areas:
        story_steps.append(
            {
                "label": "SUPPORT",
                "title": " + ".join(item["area"] for item in support_areas),
                "explanation": "; ".join(
                    f"{item['area']} — {item['role'].lower()}" for item in support_areas
                ) + ".",
            }
        )
    test_area = areas.get("tests")
    if test_area:
        story_steps.append(
            {
                "label": "VERIFY",
                "title": "tests",
                "explanation": f"{test_area['files']} test files check repository behavior.",
            }
        )
    return {
        "method": "Static inference from file paths and Python AST; no repository code was executed.",
        "excluded_code_files": sum(excluded.values()),
        "exclusions": [
            {"reason": reason, "files": count}
            for reason, count in sorted(excluded.items())
        ],
        "code_file_count": sum(item["files"] for item in languages.values()),
        "code_line_count": sum(item["lines"] for item in languages.values()),
        "languages": [
            {"language": language, **counts}
            for language, counts in sorted(languages.items(), key=lambda item: (-item[1]["lines"], item[0]))
        ],
        "areas": sorted(
            areas.values(), key=lambda item: (area_priority.get(item["area"], 10), -item["lines"], item["area"])
        ),
        "entry_points": sorted(
            entry_points,
            key=_entry_priority,
        ),
        "story_steps": story_steps,
        "changed_code": changed_code,
    }


INTEGRITY_PATH = re.compile(
    r"(^|/)(tests?|specs?|verification|verifier|policies|\.github/workflows)(/|$)",
    re.IGNORECASE,
)
INTEGRITY_NAMES = {
    "agents.md",
    "claude.md",
    "cargo.toml",
    "prompt.md",
    "pyproject.toml",
    "requirements.txt",
}


def is_integrity_surface(path: str) -> bool:
    lowered = path.lower()
    name = Path(lowered).name
    return bool(
        INTEGRITY_PATH.search(lowered)
        or name in INTEGRITY_NAMES
        or name.startswith("test_")
        or name.endswith("_test.py")
        or "policy" in name
    )


def run_check(repo: Path, command: str, timeout: float, trust_domain: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = _run(command, cwd=repo, timeout=timeout, shell=True)
        output = result.stdout or ""
        return {
            "command": command,
            "trust_domain": trust_domain,
            "status": "passed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "output_sha256": hashlib.sha256(output.encode(errors="replace")).hexdigest(),
            "output": output[-MAX_CAPTURE_CHARS:],
            "output_truncated": len(output) > MAX_CAPTURE_CHARS,
        }
    except subprocess.TimeoutExpired as error:
        output_value = error.stdout or ""
        if isinstance(output_value, bytes):
            output_value = output_value.decode(errors="replace")
        return {
            "command": command,
            "trust_domain": trust_domain,
            "status": "indeterminate",
            "exit_code": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "output_sha256": hashlib.sha256(output_value.encode(errors="replace")).hexdigest(),
            "output": (output_value + f"\nTimed out after {timeout:g} seconds")[-MAX_CAPTURE_CHARS:],
            "output_truncated": len(output_value) > MAX_CAPTURE_CHARS,
        }


def build_receipt(
    repo: Path,
    claim: str,
    checks: list[str],
    timeout: float,
    trust_domain: str = "unspecified (do not assume independence)",
) -> dict[str, Any]:
    before = capture_snapshot(repo)
    check_results = [run_check(repo, command, timeout, trust_domain) for command in checks]
    after = capture_snapshot(repo)
    code_map = build_code_map(repo, after["changes"])
    integrity_paths = sorted(
        change["path"] for change in after["changes"] if is_integrity_surface(change["path"])
    )

    if before["sha256"] != after["sha256"]:
        snapshot_status = "failed"
        snapshot_explanation = "The repository changed while checks ran, so their result is not bound to the final files."
    else:
        snapshot_status = "established"
        snapshot_explanation = "The files and commit stayed unchanged while the checks ran."

    if not check_results:
        checks_status = "not-established"
        checks_explanation = "No verification command was supplied. No behavioral claim was checked."
    elif any(item["status"] == "failed" for item in check_results):
        checks_status = "failed"
        checks_explanation = "At least one explicitly supplied command failed."
    elif any(item["status"] == "indeterminate" for item in check_results):
        checks_status = "indeterminate"
        checks_explanation = "At least one command ran without producing a verdict, such as a timeout."
    else:
        checks_status = "established"
        checks_explanation = "Every explicitly supplied command passed."

    if integrity_paths:
        integrity_status = "not-established"
        integrity_explanation = "Tests, specifications, policies, or verification controls changed and need separate review."
    else:
        integrity_status = "established"
        integrity_explanation = "No changed path looks like a test, specification, policy, or verification control."

    if "failed" in {snapshot_status, checks_status}:
        conclusion = "blocked"
        conclusion_text = "Do not accept the AI claim yet. Evidence failed."
    elif checks_status in {"not-established", "indeterminate"} or integrity_status == "not-established":
        conclusion = "needs-review"
        conclusion_text = "The evidence is incomplete. A human decision or stronger check is still needed."
    else:
        conclusion = "evidence-ready"
        conclusion_text = "The supplied evidence supports review of this claim. It is not proof beyond the checks' coverage."

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "tool_version": TOOL_VERSION,
        "renderer": {
            "path": str(Path(__file__).resolve()),
            "sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": str(repo),
        "claim": claim,
        "trust_domain": trust_domain,
        "snapshot_before": before,
        "snapshot_after": after,
        "code_map": code_map,
        "checks": check_results,
        "integrity_paths": integrity_paths,
        "claims": {
            "snapshot_stable": {"status": snapshot_status, "explanation": snapshot_explanation},
            "checks_passed": {"status": checks_status, "explanation": checks_explanation},
            "checks_unchanged": {"status": integrity_status, "explanation": integrity_explanation},
        },
        "conclusion": conclusion,
        "conclusion_text": conclusion_text,
    }
    receipt["receipt_sha256"] = _canonical_hash(receipt)
    return receipt


# --- Run-directory adapter (dalek-lite harness results) -------------------
#
# Reads a per-task results directory produced by run.py
# (results/<run_id>/<target_id>/) and renders the same style of evidence
# report from the persisted artifacts instead of live Git state. Everything
# in a run directory is SELF-REPORTED by the authoring harness — the report
# says so explicitly rather than implying independence.

CHEAT_LABELS = {
    "SPEC_DRIFT",
    "AXIOM_DRIFT",
    "TOOLING_DRIFT",
    "FORBIDDEN_CONSTRUCT",
    "FROZEN_EDIT",
    "FALSE_CONTRACT",
    "ORACLE_LEAK",
}
INFRA_LABELS = {
    "RATE_LIMITED",
    "RETRY_EXHAUSTED",
    "TRANSPORT_ERROR",
    "USER_INTERRUPTED",
    "RATE_LIMIT_OR_HANG",
    "TIMEOUT",
    "GIT_RECOVERY",
}
END_REASON_MEANING = {
    "COMPLETE": "The external verifier accepted the whole scope, no unproven placeholders remain, and no integrity tripwire fired.",
    "LIMIT": "Honest partial result: the budget ran out with work remaining. Nothing is claimed beyond what verified.",
    "NEEDS_DECOMP": "The AI declared itself blocked on missing proof infrastructure and stopped instead of grinding. Nothing extra is claimed.",
    "SPEC_DRIFT": "Tripwire fired: the success criteria (specifications) changed during the run. The result was rejected automatically.",
    "AXIOM_DRIFT": "Tripwire fired: a new unproven axiom appeared during the run. The result was rejected automatically.",
    "TOOLING_DRIFT": "Tripwire fired: the harness's own checking tools were modified during the run. The result was rejected automatically.",
    "FORBIDDEN_CONSTRUCT": "Tripwire fired: a construct that bypasses the verifier (assume / external_body / rlimit change) appeared. The result was rejected automatically.",
    "FROZEN_EDIT": "Tripwire fired: a file outside the allowed edit scope was changed. The result was rejected automatically.",
    "FALSE_CONTRACT": "Tripwire fired: a reconstructed contract was provably false. The result was rejected automatically.",
    "RATE_LIMITED": "Infrastructure halt: the AI provider rejected requests (quota). The run never finished; nothing is claimed.",
    "RETRY_EXHAUSTED": "Infrastructure halt: the AI transport gave up after retries. The run never finished; nothing is claimed.",
    "TRANSPORT_ERROR": "Infrastructure halt: the AI transport failed. The run never finished; nothing is claimed.",
    "USER_INTERRUPTED": "A human interrupted the run. Nothing is claimed.",
    "GIT_RECOVERY": "The harness had to restore files from Git during the run; the result needs separate review.",
}


def _hash_artifact(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": path.name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def load_run_dir(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        raise ValueError(f"not a run directory (no result.json): {run_dir}")
    artifacts = [_hash_artifact(result_path)]
    result = json.loads(result_path.read_text(encoding="utf-8"))

    rounds: list[dict[str, Any]] = []
    round_re = re.compile(r"^round_(\d+)\.json$")
    for path in sorted(run_dir.iterdir()):
        match = round_re.match(path.name)
        if match:
            artifacts.append(_hash_artifact(path))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.setdefault("round_number", int(match.group(1)))
            rounds.append(payload)
    rounds.sort(key=lambda item: item.get("round_number", 0))

    for optional in ("spec_snapshot.json", "prompt_rendered.md"):
        candidate = run_dir / optional
        if candidate.is_file():
            artifacts.append(_hash_artifact(candidate))

    return {"run_dir": run_dir, "result": result, "rounds": rounds, "artifacts": artifacts}


def build_run_receipt(
    run_dir: Path,
    trust_domain: str = "the authoring harness on the same machine as the AI (self-reported; not independent)",
) -> dict[str, Any]:
    loaded = load_run_dir(run_dir)
    result = loaded["result"]
    rounds = loaded["rounds"]

    end_reason = str(result.get("end_reason") or "UNKNOWN").upper()
    meaning = END_REASON_MEANING.get(end_reason, "Unrecognized outcome label; review the raw result.json.")
    admits = result.get("admit_classification") or {}
    hard_admits = admits.get("hard")
    admit_detail = [
        item for item in (admits.get("detail") or []) if item.get("classification") != "intentional"
    ]

    guard_events: list[dict[str, Any]] = []
    for item in rounds:
        label = str(item.get("end_reason") or "").upper()
        if label in CHEAT_LABELS:
            guard_events.append({"round": item.get("round_number"), "event": label})
        if item.get("spec_drift"):
            guard_events.append(
                {"round": item.get("round_number"), "event": "SPEC_DRIFT_DETECTED", "detail": item["spec_drift"]}
            )
    if end_reason in CHEAT_LABELS and not any(e["event"] == end_reason for e in guard_events):
        guard_events.append({"round": result.get("rounds_used"), "event": end_reason})

    # Claim 1: the success criteria never changed.
    if end_reason in CHEAT_LABELS or guard_events:
        rules_status = "failed"
        rules_explanation = (
            "An integrity tripwire fired during this run and the result was rejected. "
            "The brake working is evidence the guards are real, but this work must not be accepted."
        )
    else:
        rules_status = "established"
        rules_explanation = (
            "No round reported a change to specifications, axioms, checking tools, or forbidden constructs. "
            "This is self-reported by the harness; the tripwires are detection, not prevention."
        )

    # Claim 2: an external verifier accepted the work.
    final_round = rounds[-1] if rounds else None
    final_verus_okay = bool(final_round.get("verus_okay")) if final_round else None
    if final_verus_okay:
        verifier_status = "established"
        verifier_explanation = "The final recorded round shows the Verus verifier accepting the checked scope."
    elif final_verus_okay is None:
        verifier_status = "not-established"
        verifier_explanation = "No per-round verification record exists, so no verifier verdict can be shown."
    elif end_reason == "COMPLETE":
        verifier_status = "failed"
        verifier_explanation = (
            "The run claims COMPLETE but the final round's verifier verdict is not passing. "
            "This inconsistency must be resolved before accepting anything."
        )
    else:
        verifier_status = "not-established"
        verifier_explanation = "The verifier does not (yet) accept the full scope; the outcome label reflects that."

    # Claim 3: no unproven placeholders remain.
    if hard_admits == 0:
        placeholders_status = "established"
        placeholders_explanation = "Zero hard admit() placeholders remain — the verifier's acceptance is not resting on any skipped proof."
    elif hard_admits is None:
        placeholders_status = "not-established"
        placeholders_explanation = "The run record does not include an admit inventory."
    elif end_reason == "COMPLETE":
        placeholders_status = "failed"
        placeholders_explanation = (
            f"The run claims COMPLETE but {hard_admits} hard admit() placeholders remain — an inconsistency."
        )
    else:
        placeholders_status = "not-established"
        placeholders_explanation = (
            f"{hard_admits} proof obligations are still stubbed with admit(). They are listed below, not hidden."
        )

    # Claim 4: the whole attempt history is on the record.
    rounds_used = result.get("rounds_used")
    total_cost = round(
        sum((item.get("claude_usage") or {}).get("total_cost_usd") or 0.0 for item in rounds), 6
    )
    if rounds_used is not None and rounds_used == len(rounds):
        record_status = "established"
        record_explanation = (
            f"All {len(rounds)} rounds the summary claims are present as individual receipts, including failures."
        )
    else:
        record_status = "failed"
        record_explanation = (
            f"The summary claims {rounds_used} rounds but {len(rounds)} per-round receipts exist. "
            "Missing receipts mean part of the story is off the record."
        )

    claims = {
        "rules_unchanged": {"status": rules_status, "explanation": rules_explanation},
        "verifier_accepted": {"status": verifier_status, "explanation": verifier_explanation},
        "no_placeholders": {"status": placeholders_status, "explanation": placeholders_explanation},
        "full_record": {"status": record_status, "explanation": record_explanation},
    }

    if end_reason in CHEAT_LABELS or any(item["status"] == "failed" for item in claims.values()):
        conclusion = "blocked"
        conclusion_text = "Do not accept this run. Either a tripwire fired or the record is internally inconsistent."
    elif end_reason == "COMPLETE" and all(item["status"] == "established" for item in claims.values()):
        conclusion = "evidence-ready"
        conclusion_text = (
            "Every recorded check supports the claim. All checks are self-reported by the harness — "
            "independent re-verification on a clean machine is the remaining step."
        )
    else:
        conclusion = "needs-review"
        conclusion_text = "This is an honest partial or halted run. The gaps are listed, not hidden."

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "mode": "run-dir",
        "tool_version": TOOL_VERSION,
        "renderer": {
            "path": str(Path(__file__).resolve()),
            "sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(loaded["run_dir"]),
        "run_id": result.get("run_id"),
        "task_id": result.get("task_id"),
        "target_path": result.get("target_path"),
        "end_reason": end_reason,
        "end_reason_meaning": meaning,
        "trust_domain": trust_domain,
        "rounds": [
            {
                "round": item.get("round_number"),
                "end_reason": item.get("end_reason"),
                "verus_okay": item.get("verus_okay"),
                "verus_error_count": len(item.get("verus_errors") or []),
                "spec_drift_count": len(item.get("spec_drift") or []),
                "duration_seconds": item.get("duration_seconds"),
                "cost_usd": (item.get("claude_usage") or {}).get("total_cost_usd"),
                "fresh_session": item.get("round_number") in set(result.get("reset_round_starts") or []),
            }
            for item in rounds
        ],
        "guard_events": guard_events,
        "admit_inventory": admit_detail,
        "hard_admits": hard_admits,
        "total_cost_usd": total_cost,
        "artifacts": loaded["artifacts"],
        "claims": claims,
        "conclusion": conclusion,
        "conclusion_text": conclusion_text,
    }
    receipt["receipt_sha256"] = _canonical_hash(receipt)
    return receipt


def render_run_html(receipt: dict[str, Any]) -> str:
    esc = html.escape
    claims = receipt["claims"]

    def fmt(value: Any, spec: str = "") -> str:
        if value is None:
            return "—"
        return format(value, spec) if spec else str(value)

    round_rows = "".join(
        "<tr>"
        f"<td>{fmt(item['round'])}{' · fresh session' if item['fresh_session'] else ''}</td>"
        f"<td>{esc(str(item['end_reason'] or ''))}</td>"
        f"<td>{'yes' if item['verus_okay'] else 'no'}</td>"
        f"<td>{fmt(item['verus_error_count'])}</td>"
        f"<td>{fmt(item['duration_seconds'], '.0f') if item['duration_seconds'] is not None else '—'}s</td>"
        f"<td>{('$' + format(item['cost_usd'], '.2f')) if item['cost_usd'] is not None else '—'}</td>"
        "</tr>"
        for item in receipt["rounds"]
    ) or '<tr><td colspan="6">No per-round receipts.</td></tr>'

    guard_rows = "".join(
        f"<li>Round {fmt(event.get('round'))}: <strong>{esc(event['event'])}</strong></li>"
        for event in receipt["guard_events"]
    )
    guard_block = (
        f"<p><strong>Tripwires that fired during this run:</strong></p><ul>{guard_rows}</ul>"
        if guard_rows
        else "<p>No tripwire fired during this run. (These same tripwires have rejected real runs before — they are not decorative.)</p>"
    )

    admit_rows = "".join(
        "<tr>"
        f"<td><code>{esc(str(item.get('function', '?')))}</code></td>"
        f"<td>{fmt(item.get('line'))}</td>"
        f"<td><code>{esc(Path(str(item.get('file', ''))).name)}</code></td>"
        "</tr>"
        for item in receipt["admit_inventory"]
    )
    admit_block = (
        '<details><summary>Show each unproven obligation</summary><div style="overflow-x:auto"><table>'
        "<thead><tr><th>Function</th><th>Line</th><th>File</th></tr></thead>"
        f"<tbody>{admit_rows}</tbody></table></div></details>"
        if admit_rows
        else ""
    )

    artifact_rows = "".join(
        f"<tr><td><code>{esc(item['path'])}</code></td><td><code>{item['sha256'][:16]}…</code></td><td>{item['size']}</td></tr>"
        for item in receipt["artifacts"]
    )

    receipt_json = json.dumps(receipt, indent=2, ensure_ascii=True, sort_keys=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI proof run evidence report</title>
<style>
:root {{ color-scheme: light dark; --bg:#f7f7f5; --fg:#1c1c1a; --panel:#fff; --muted:#66645f; --line:#d9d7d1; --good:#176b3a; --bad:#a12626; --warn:#8a5a00; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#181817; --fg:#efeee9; --panel:#232321; --muted:#b4b1a8; --line:#44423d; --good:#65c78c; --bad:#ff8d8d; --warn:#e3b75f; }} }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,sans-serif; }}
main {{ max-width:920px; margin:auto; padding:32px 20px 64px; }} h1 {{ font-size:clamp(1.8rem,5vw,3rem); line-height:1.1; margin:.25rem 0 1.5rem; }} h2 {{ margin-top:2.2rem; }}
.eyebrow {{ color:var(--muted); }} .claim {{ font-size:1.25rem; }} .bottom-line {{ background:var(--panel); border:2px solid var(--line); border-radius:14px; padding:20px; margin:24px 0; }}
.question {{ border-top:1px solid var(--line); padding:20px 0; }} .question-head {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
.pill {{ border:1px solid currentColor; border-radius:999px; padding:2px 9px; font-size:.78rem; font-weight:700; letter-spacing:.04em; white-space:nowrap; }}
.established,.passed,.evidence-ready {{ color:var(--good); }} .failed,.blocked {{ color:var(--bad); }} .not-established,.indeterminate,.needs-review {{ color:var(--warn); }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; vertical-align:top; }}
code,pre {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }} code {{ overflow-wrap:anywhere; }} pre {{ max-height:340px; overflow:auto; padding:14px; background:var(--panel); border:1px solid var(--line); border-radius:8px; white-space:pre-wrap; }}
details {{ padding:10px 0; border-bottom:1px solid var(--line); }} summary {{ cursor:pointer; }} .facts {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }} .fact {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; }} .fact span {{ display:block; color:var(--muted); }}
ul {{ padding-left:1.25rem; }} .muted {{ color:var(--muted); }}
</style>
</head>
<body><main>
<p class="eyebrow">Proof run evidence report · {esc(receipt['generated_at'])}</p>
<h1>Can I trust this AI proof run?</h1>
<p class="claim"><strong>Run:</strong> <code>{esc(str(receipt['run_id']))}</code> / <code>{esc(str(receipt['task_id']))}</code> · target <code>{esc(Path(str(receipt['target_path'] or '?')).name)}</code></p>
<section class="bottom-line">
<span class="pill {receipt['conclusion']}">{_status_label(receipt['conclusion'])}</span>
<h2>Bottom line — outcome label: {esc(receipt['end_reason'])}</h2><p>{esc(receipt['end_reason_meaning'])}</p><p>{esc(receipt['conclusion_text'])}</p>
</section>

<section class="question"><div class="question-head"><span class="pill {claims['rules_unchanged']['status']}">{_status_label(claims['rules_unchanged']['status'])}</span><h2>1. Did the definition of "correct" ever change?</h2></div>
<p>{esc(claims['rules_unchanged']['explanation'])}</p><p><strong>Cheat this defeats:</strong> weakening the specifications, adding an axiom, rewriting the checking tools, or using verifier bypasses to manufacture a green result.</p>{guard_block}</section>

<section class="question"><div class="question-head"><span class="pill {claims['verifier_accepted']['status']}">{_status_label(claims['verifier_accepted']['status'])}</span><h2>2. Did something other than the AI check the work?</h2></div>
<p>{esc(claims['verifier_accepted']['explanation'])}</p><p><strong>Who controlled this check:</strong> {esc(receipt['trust_domain'])}</p><p><strong>Cheat this defeats:</strong> the AI grading its own homework. The verdict comes from the Verus/Z3 verifier, but the record of that verdict is written by the harness — independent re-verification on a clean checkout is what would upgrade this from self-reported to independent.</p></section>

<section class="question"><div class="question-head"><span class="pill {claims['no_placeholders']['status']}">{_status_label(claims['no_placeholders']['status'])}</span><h2>3. What remains unproven?</h2></div>
<p>{esc(claims['no_placeholders']['explanation'])}</p><p><strong>Cheat this defeats:</strong> an <code>admit()</code> placeholder makes the verifier accept any statement without proof, so a green verifier verdict means nothing unless this count is zero.</p>{admit_block}</section>

<section class="question"><div class="question-head"><span class="pill {claims['full_record']['status']}">{_status_label(claims['full_record']['status'])}</span><h2>4. Is the whole story on the record?</h2></div>
<p>{esc(claims['full_record']['explanation'])}</p><p><strong>Cheat this defeats:</strong> showing only the successful attempt and hiding the failures (cherry-picking).</p>
<p><strong>Total recorded cost:</strong> ${receipt['total_cost_usd']:.2f} across {len(receipt['rounds'])} rounds.</p>
<details><summary>Show every round</summary><div style="overflow-x:auto"><table><thead><tr><th>Round</th><th>Outcome</th><th>Verifier OK</th><th>Errors</th><th>Duration</th><th>Cost</th></tr></thead><tbody>{round_rows}</tbody></table></div></details></section>

<section class="question"><h2>5. What does this report not prove?</h2><ul>
<li>Every number here was written by the same harness, on the same machine, where the AI ran. A compromised harness could fake all of it; only re-running the verifier on a clean machine closes that.</li>
<li>That the frozen specifications are <em>meaningful</em> — byte-freezing proves they weren't weakened, not that they say the right thing. That needs a one-time human or reviewer attestation.</li>
<li>That the baseline the tripwires compare against was itself clean. The baseline's anchor (a committed, reviewed ref) is part of the run setup, not this report.</li>
<li>That the verifier toolchain (verus, z3, rustc) was built honestly.</li></ul></section>

<section class="question"><h2>Evidence receipt</h2><div class="facts">
<div class="fact"><span>Run directory</span><code>{esc(receipt['run_dir'])}</code></div>
<div class="fact"><span>Receipt</span><code>{receipt['receipt_sha256']}</code></div>
<div class="fact"><span>Renderer SHA-256</span><code>{receipt['renderer']['sha256']}</code></div>
<div class="fact"><span>Tool</span>trust_report.py {TOOL_VERSION}</div></div>
<p><strong>Source artifacts this report was derived from</strong> (hash these files to check the report wasn't detached from its evidence):</p>
<div style="overflow-x:auto"><table><thead><tr><th>Artifact</th><th>SHA-256</th><th>Bytes</th></tr></thead><tbody>{artifact_rows}</tbody></table></div>
<details><summary>Show machine-readable receipt</summary><pre>{esc(receipt_json)}</pre></details></section>
<p class="muted">This report presents evidence; it does not ask you to trust the AI that authored the work.</p>
</main></body></html>"""


def _status_label(status: str) -> str:
    return {
        "established": "ESTABLISHED",
        "failed": "FAILED",
        "not-established": "NOT ESTABLISHED",
        "indeterminate": "INDETERMINATE",
        "blocked": "BLOCKED",
        "needs-review": "NEEDS REVIEW",
        "evidence-ready": "EVIDENCE READY",
    }[status]


def _change_label(change: dict[str, Any]) -> str:
    status = change["status"]
    if status == "??":
        return "new, untracked"
    if "D" in status:
        return "deleted"
    if "R" in status:
        return "renamed"
    if "A" in status:
        return "added"
    return "modified"


def render_html(receipt: dict[str, Any]) -> str:
    esc = html.escape
    after = receipt["snapshot_after"]
    changes = after["changes"]
    claims = receipt["claims"]
    code_map = receipt["code_map"]
    integrity_paths = set(receipt["integrity_paths"])

    language_text = ", ".join(
        f"{item['language']} ({item['files']} files)"
        for item in code_map["languages"][:5]
    ) or "No supported code files found"
    exclusion_text = ", ".join(
        f"{item['reason']} ({item['files']} files)"
        for item in code_map["exclusions"]
    ) or "None"
    area_rows = "".join(
        "<tr>"
        f"<td><code>{esc(item['area'])}</code></td>"
        f"<td>{esc(item['role'])}</td>"
        f"<td>{item['files']}</td><td>{item['lines']:,}</td>"
        "</tr>"
        for item in code_map["areas"][:20]
    ) or '<tr><td colspan="4">No code areas inferred.</td></tr>'
    entry_rows = "".join(
        "<tr>"
        f"<td><code>{esc(item['path'])}</code></td>"
        f"<td>{esc(item['role'])}</td>"
        f"<td>{esc(', '.join(item['local_dependencies']) or 'No local dependency inferred')}</td>"
        "</tr>"
        for item in code_map["entry_points"][:30]
    ) or '<tr><td colspan="3">No executable entry point inferred.</td></tr>'
    changed_code_rows = "".join(
        "<tr>"
        f"<td><code>{esc(item['path'])}</code></td>"
        f"<td>{esc(item['role'])}</td>"
        f"<td>{esc(', '.join(item['symbols'][:12]) or 'File-level or non-Python change')}</td>"
        "</tr>"
        for item in code_map["changed_code"][:80]
    ) or '<tr><td colspan="3">No supported code file changed.</td></tr>'
    story_cards = "".join(
        '<div class="flow-step">'
        f'<span>{esc(item["label"])}</span>'
        f'<strong><code>{esc(item["title"])}</code></strong>'
        f'<p>{esc(item["explanation"])}</p>'
        '</div>'
        for item in code_map["story_steps"]
    ) or '<p>No conventional execution path was inferred. Start with the area table below.</p>'

    change_rows = "".join(
        "<tr>"
        f"<td><code>{esc(item['path'])}</code></td>"
        f"<td>{esc(_change_label(item))}</td>"
        f"<td>{'Needs review' if item['path'] in integrity_paths else 'Ordinary change'}</td>"
        "</tr>"
        for item in changes
    ) or '<tr><td colspan="3">No Git-visible changes.</td></tr>'

    check_blocks = "".join(
        "<details>"
        f"<summary><span class=\"pill {item['status']}\">{item['status'].upper()}</span> "
        f"<code>{esc(item['command'])}</code> ({item['duration_seconds']}s)</summary>"
        f"<p><strong>Who controlled this check:</strong> {esc(item['trust_domain'])}</p>"
        f"<p>Exit code: {esc(str(item['exit_code']))} · Output SHA-256: <code>{item['output_sha256']}</code></p>"
        f"<pre>{esc(item['output'] or '(no output)')}</pre>"
        "</details>"
        for item in receipt["checks"]
    ) or "<p>No commands were run. This report does not establish behavior.</p>"

    receipt_json = json.dumps(receipt, indent=2, ensure_ascii=True, sort_keys=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI work evidence report</title>
<style>
:root {{ color-scheme: light dark; --bg:#f7f7f5; --fg:#1c1c1a; --panel:#fff; --muted:#66645f; --line:#d9d7d1; --good:#176b3a; --bad:#a12626; --warn:#8a5a00; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#181817; --fg:#efeee9; --panel:#232321; --muted:#b4b1a8; --line:#44423d; --good:#65c78c; --bad:#ff8d8d; --warn:#e3b75f; }} }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,sans-serif; }}
main {{ max-width:920px; margin:auto; padding:32px 20px 64px; }} h1 {{ font-size:clamp(1.8rem,5vw,3rem); line-height:1.1; margin:.25rem 0 1.5rem; }} h2 {{ margin-top:2.2rem; }}
.eyebrow {{ color:var(--muted); }} .claim {{ font-size:1.25rem; }} .bottom-line {{ background:var(--panel); border:2px solid var(--line); border-radius:14px; padding:20px; margin:24px 0; }}
.question {{ border-top:1px solid var(--line); padding:20px 0; }} .question-head {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
.inference-note {{ border-left:3px solid var(--warn); padding-left:12px; color:var(--muted); }}
.flow {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin:18px 0; }}
.flow-step {{ position:relative; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }}
.flow-step span {{ display:block; color:var(--muted); font-size:.72rem; font-weight:800; letter-spacing:.08em; margin-bottom:6px; }}
.flow-step strong {{ display:block; }} .flow-step p {{ margin:.45rem 0 0; color:var(--muted); font-size:.9rem; }}
.pill {{ border:1px solid currentColor; border-radius:999px; padding:2px 9px; font-size:.78rem; font-weight:700; letter-spacing:.04em; white-space:nowrap; }}
.established,.passed,.evidence-ready {{ color:var(--good); }} .failed,.blocked {{ color:var(--bad); }} .not-established,.indeterminate,.needs-review {{ color:var(--warn); }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; vertical-align:top; }}
code,pre {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }} code {{ overflow-wrap:anywhere; }} pre {{ max-height:340px; overflow:auto; padding:14px; background:var(--panel); border:1px solid var(--line); border-radius:8px; white-space:pre-wrap; }}
details {{ padding:10px 0; border-bottom:1px solid var(--line); }} summary {{ cursor:pointer; }} .facts {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }} .fact {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; }} .fact span {{ display:block; color:var(--muted); }}
ul {{ padding-left:1.25rem; }} .muted {{ color:var(--muted); }}
</style>
</head>
<body><main>
<p class="eyebrow">Repository evidence report · {esc(receipt['generated_at'])}</p>
<h1>Can I trust this AI work?</h1>
<p class="claim"><strong>AI claim:</strong> {esc(receipt['claim'])}</p>
<section class="bottom-line">
<span class="pill {receipt['conclusion']}">{_status_label(receipt['conclusion'])}</span>
<h2>Bottom line</h2><p>{esc(receipt['conclusion_text'])}</p>
</section>

<section class="question"><div class="question-head"><span class="pill not-established">INFERRED</span><h2>How does this repository's code work?</h2></div>
<p class="inference-note">{esc(code_map['method'])} These are navigation clues, not verified statements of developer intent.</p>
<h3>Read this first: the likely execution story</h3>
<div class="flow">{story_cards}</div>
<div class="facts">
<div class="fact"><span>Supported code files</span>{code_map['code_file_count']}</div>
<div class="fact"><span>Lines inspected</span>{code_map['code_line_count']:,}</div>
<div class="fact"><span>Languages</span>{esc(language_text)}</div>
</div>
<p class="muted"><strong>Code-like artifacts excluded:</strong> {code_map['excluded_code_files']} files — {esc(exclusion_text)}.</p>
<details><summary>Code areas and responsibilities ({len(code_map['areas'])} areas)</summary><div style="overflow-x:auto"><table><thead><tr><th>Area</th><th>Likely responsibility</th><th>Files</th><th>Lines</th></tr></thead><tbody>{area_rows}</tbody></table></div></details>
<details><summary>Where execution can start ({len(code_map['entry_points'])} inferred entry points)</summary><div style="overflow-x:auto"><table><thead><tr><th>Entry point</th><th>Likely role</th><th>Local code it imports</th></tr></thead><tbody>{entry_rows}</tbody></table></div></details>
<details><summary>Changed code explained ({len(code_map['changed_code'])} supported code files)</summary><div style="overflow-x:auto"><table><thead><tr><th>Changed file</th><th>Likely role</th><th>Functions/classes touched</th></tr></thead><tbody>{changed_code_rows}</tbody></table></div></details>
</section>

<section class="question"><div class="question-head"><span class="pill {claims['snapshot_stable']['status']}">{_status_label(claims['snapshot_stable']['status'])}</span><h2>1. Do we know exactly what was checked?</h2></div>
<p>{esc(claims['snapshot_stable']['explanation'])}</p><p><strong>Cheat this defeats:</strong> showing you passing results from different files than the ones under review.</p><p><strong>{len(changes)} Git-visible changed files.</strong></p>
<details><summary>Show changed files</summary><div style="overflow-x:auto"><table><thead><tr><th>File</th><th>Change</th><th>Risk</th></tr></thead><tbody>{change_rows}</tbody></table></div></details></section>

<section class="question"><div class="question-head"><span class="pill {claims['checks_passed']['status']}">{_status_label(claims['checks_passed']['status'])}</span><h2>2. Did checks run, and who controlled them?</h2></div>
<p>{esc(claims['checks_passed']['explanation'])}</p><p><strong>Trust domain:</strong> {esc(receipt['trust_domain'])}</p><p><strong>Cheat this defeats:</strong> claiming a check passed without running that exact command. A same-host AI-controlled check is evidence, but not independent evidence.</p>{check_blocks}</section>

<section class="question"><div class="question-head"><span class="pill {claims['checks_unchanged']['status']}">{_status_label(claims['checks_unchanged']['status'])}</span><h2>3. Did the AI change the rules used to judge itself?</h2></div>
<p>{esc(claims['checks_unchanged']['explanation'])}</p><p><strong>Cheat this defeats:</strong> weakening tests, specifications, policies, or verifier settings to manufacture a green result.</p>
{('<ul>' + ''.join(f'<li><code>{esc(path)}</code></li>' for path in receipt['integrity_paths']) + '</ul>') if receipt['integrity_paths'] else '<p>No likely integrity-control paths changed.</p>'}</section>

<section class="question"><h2>4. What does this report not prove?</h2><ul>
<li>That the supplied checks cover every requirement or possible bug.</li><li>That the machine running this report is uncompromised.</li><li>That the AI made no unlogged changes to external systems.</li><li>That changed tests, specifications, or policies are legitimate without human review.</li></ul></section>

<section class="question"><h2>Evidence receipt</h2><div class="facts">
<div class="fact"><span>Repository</span><code>{esc(receipt['repository'])}</code></div><div class="fact"><span>Branch</span>{esc(after['branch'])}</div>
<div class="fact"><span>Commit</span><code>{after['head']}</code></div><div class="fact"><span>Snapshot</span><code>{after['sha256']}</code></div>
<div class="fact"><span>Receipt</span><code>{receipt['receipt_sha256']}</code></div><div class="fact"><span>Renderer SHA-256</span><code>{receipt['renderer']['sha256']}</code></div>
<div class="fact"><span>Tool</span>trust_report.py {TOOL_VERSION}</div><div class="fact"><span>Trust domain</span>{esc(receipt['trust_domain'])}</div></div>
<details><summary>Show machine-readable receipt</summary><pre>{esc(receipt_json)}</pre></details></section>
<p class="muted">This report presents evidence; it does not ask you to trust the AI that authored the work.</p>
</main></body></html>"""


def write_report(receipt: dict[str, Any], output: Path) -> tuple[Path, Path]:
    html_path = output.expanduser().resolve()
    json_path = html_path.with_suffix(".json")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(receipt), encoding="utf-8")
    json_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
    return html_path, json_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, help="path inside the Git repository to inspect")
    parser.add_argument("--output", type=Path, default=Path("trust-report.html"), help="HTML report path")
    parser.add_argument("--claim", default="The current repository changes are ready to accept.", help="claim being evaluated")
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="COMMAND",
        help="explicit verification command to run in the repo; repeatable; never auto-detected",
    )
    parser.add_argument("--timeout", type=float, default=300, help="timeout in seconds for each check")
    parser.add_argument(
        "--trust-domain",
        default="unspecified (do not assume independence)",
        help="who controls the machine/process that runs the checks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo = find_repo(args.repo)
        receipt = build_receipt(repo, args.claim, args.check, args.timeout, args.trust_domain)
        html_path, json_path = write_report(receipt, args.output)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"trust_report: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"conclusion": receipt["conclusion"], "html": str(html_path), "receipt": str(json_path)}))
    return 1 if receipt["conclusion"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
