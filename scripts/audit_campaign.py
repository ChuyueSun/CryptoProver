#!/usr/bin/env python3
"""scripts/audit_campaign.py — post-campaign verification audit.

For each module in LAYER_SETS["ALL"]:
  - cargo verus verify against the live worktree
  - spec_check.py verify against the latest spec_snapshot.json
  - per-module raw outputs under results/<audit_run>/<target_id>/
Also:
  - tier-1 grep over the project (assume(), external_body deltas, axiom_ defs)
  - tier-3 suspicious-shape scan (assume_specification, trivial bodies,
    rlimit>200, spinoff/integer_ring/nonlinear)
  - lemma enumeration for tier-4 (every non-axiom proof fn)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from run_layer import AXIOM_MODULES, LAYER_SETS, module_to_file  # noqa: E402
from lib.results import target_id_from_path, write_json  # noqa: E402

ADMITTED_START_REF = "eval/admitted-start"
VERUS_TIMEOUT = 900
SKILLS = HERE / "skills"

# ---------- subprocess wrappers ----------

def _run_json(cmd: list[str]) -> tuple[dict, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(p.stdout), ""
    except json.JSONDecodeError:
        return {}, p.stderr[-2000:]


def run_verus(target: Path, module: str, project: Path, rlimit: float) -> dict:
    data, err = _run_json([
        sys.executable, str(SKILLS / "verus_check.py"), str(target),
        "--project", str(project),
        "--module", module,
        "--rlimit", str(rlimit),
        "--timeout", str(VERUS_TIMEOUT),
    ])
    if not data:
        return {"okay": False, "messages": [{"data": "bad json from verus_check"}],
                "returncode": -1, "stderr_tail": err}
    return data


def run_spec_verify(target: Path, snapshot: Path) -> dict:
    data, err = _run_json([
        sys.executable, str(SKILLS / "spec_check.py"), "verify",
        str(target), "--against", str(snapshot),
    ])
    if not data:
        return {"okay": False, "drift": [{"change": f"bad json: {err[:200]}"}],
                "new_functions": {}}
    return data


# ---------- snapshot picker ----------

def find_latest_snapshot(target_id: str,
                         results_root: Path) -> tuple[Path, str] | None:
    best: tuple[float, Path, str] | None = None
    for snap in results_root.glob(f"*/{target_id}/spec_snapshot.json"):
        if not snap.is_file():
            continue
        mt = snap.stat().st_mtime
        run_id = snap.parent.parent.name
        if best is None or mt > best[0]:
            best = (mt, snap, run_id)
    return None if best is None else (best[1], best[2])


# ---------- per-module audit ----------

def audit_one_module(module: str, project: Path, results_root: Path,
                     audit_root: Path, rlimit: float) -> dict:
    rec: dict = {"module": module}
    try:
        target = module_to_file(module, project)
    except FileNotFoundError:
        rec.update(skipped=True, reason="file_not_found")
        return rec

    tid = target_id_from_path(target)
    rec["file"] = str(target.relative_to(project))
    rec["target_id"] = tid
    out_dir = audit_root / tid
    out_dir.mkdir(parents=True, exist_ok=True)

    snap_info = find_latest_snapshot(tid, results_root)
    if snap_info is None:
        rec["snapshot_from_run"] = None
        rec["spec_drift_okay"] = None
        rec["spec_drift_count"] = 0
        rec["new_functions_count"] = 0
    else:
        snap_path, snap_run = snap_info
        spec_res = run_spec_verify(target, snap_path)
        rec["snapshot_from_run"] = snap_run
        rec["spec_drift_okay"] = bool(spec_res.get("okay"))
        rec["spec_drift_count"] = len(spec_res.get("drift") or [])
        rec["new_functions_count"] = sum(
            len(v) for v in (spec_res.get("new_functions") or {}).values()
        )
        write_json(out_dir / "spec_check.json", spec_res)

    t0 = time.time()
    verus_res = run_verus(target, module, project, rlimit)
    rec["verus_duration_s"] = round(time.time() - t0, 2)
    rec["verus_okay"] = bool(verus_res.get("okay"))
    rec["verus_errors_count"] = len(verus_res.get("messages") or [])
    rec["verus_returncode"] = verus_res.get("returncode")
    write_json(out_dir / "verus.json", verus_res)
    return rec


# ---------- tier-1 grep + tier-3 shapes + lemma enumeration ----------

ASSUME_RE = re.compile(r"\bassume\s*\(")
EXTERNAL_BODY_RE = re.compile(r"#\[verifier::external_body\]")
AXIOM_FN_RE = re.compile(r"\bproof\s+fn\s+(axiom_[A-Za-z0-9_]*)\b")
RLIMIT_ATTR_RE = re.compile(r"#\[verifier::rlimit\(\s*(\d+(?:\.\d+)?)\s*\)\]")
SPIN_PROVER_RE = re.compile(
    r"#\[verifier::(spinoff_prover|integer_ring|nonlinear)\]"
)
ASSUME_SPEC_RE = re.compile(r"\b(assume_specification|assume_external)\b")
PROOF_FN_HEADER_RE = re.compile(
    r"(?P<pre>(?:pub(?:\([^)]*\))?\s+)?proof\s+fn\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*))[^{;]*"
)


def axiom_module_files(project: Path) -> set[Path]:
    out: set[Path] = set()
    for m in AXIOM_MODULES:
        try:
            out.add(module_to_file(m, project).resolve())
        except FileNotFoundError:
            pass
    return out


def git_subdir(project: Path) -> str:
    """Return the project's path relative to the git root, with trailing slash.

    `git show ref:path` requires path to be repo-root-relative. When the
    project is a subdirectory of the repo (as in dalek-lite-mvp's case where
    the worktree git root is one level above the curve25519-dalek crate),
    callers must prefix `<subdir>/` to a project-relative path.
    """
    p = subprocess.run(["git", "rev-parse", "--show-prefix"],
                       cwd=str(project), capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def git_show(project: Path, ref: str, rel: str, subdir: str = "") -> str:
    p = subprocess.run(["git", "show", f"{ref}:{subdir}{rel}"],
                       cwd=str(project), capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""


def strip_line_comments(text: str) -> str:
    """Replace // line-comment contents with spaces, preserving offsets.

    Block comments (/* ... */) are not stripped; they're rare in proof
    bodies and the false positives we saw were all `//`-style.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            if j < 0:
                out.append(' ' * (n - i))
                i = n
            else:
                out.append(' ' * (j - i))
                i = j
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def iter_proof_fns(text: str):
    """Yield (name, header_start, body_start, body_end) for each proof fn.

    body_start/body_end are None when the declaration ends in `;` (trait
    sig, no body). Brace counting is naïve but adequate for shape checks.
    """
    for m in PROOF_FN_HEADER_RE.finditer(text):
        name = m.group("name")
        i = m.end()
        while i < len(text) and text[i] not in "{;":
            i += 1
        if i >= len(text):
            continue
        if text[i] == ";":
            yield name, m.start(), None, None
            continue
        depth = 1
        j = i + 1
        while j < len(text) and depth > 0:
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        if depth == 0:
            yield name, m.start(), i + 1, j - 1


def _line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def tier1_cheat_grep(project: Path, axiom_files: set[Path]) -> dict:
    src = project / "src"
    subdir = git_subdir(project)
    out = {
        "assume_outside_axiom_files": [],
        "new_external_body": [],
        "new_axiom_proof_fns": [],
        "git_subdir": subdir,  # for debuggability
    }
    for rs in sorted(src.rglob("*.rs")):
        text = rs.read_text(errors="replace")
        rel = str(rs.relative_to(project))
        if rs.resolve() not in axiom_files:
            scan_text = strip_line_comments(text)
            for m in ASSUME_RE.finditer(scan_text):
                out["assume_outside_axiom_files"].append(
                    {"file": rel, "line": _line_of(scan_text, m.start())}
                )
        original = git_show(project, ADMITTED_START_REF, rel, subdir)
        cur_eb = len(EXTERNAL_BODY_RE.findall(text))
        old_eb = len(EXTERNAL_BODY_RE.findall(original))
        if cur_eb > old_eb:
            out["new_external_body"].append(
                {"file": rel, "added": cur_eb - old_eb,
                 "baseline_lines": original.count("\n"),
                 "current_lines": text.count("\n")}
            )
        cur_ax = {m.group(1) for m in AXIOM_FN_RE.finditer(text)}
        old_ax = {m.group(1) for m in AXIOM_FN_RE.finditer(original)}
        added = sorted(cur_ax - old_ax)
        if added:
            out["new_axiom_proof_fns"].append({"file": rel, "added": added})
    out["clean"] = (
        not out["assume_outside_axiom_files"]
        and not out["new_external_body"]
        and not out["new_axiom_proof_fns"]
    )
    return out


def tier3_and_enumerate(project: Path,
                        axiom_files: set[Path]) -> tuple[dict, list[dict]]:
    src = project / "src"
    flagged = {
        "assume_specification": [],
        "trivial_body_assert_false": [],
        "trivial_body_unreached": [],
        "empty_body": [],
        "rlimit_over_200": [],
        "spinoff_or_nonlinear": [],
    }
    lemmas: list[dict] = []
    for rs in sorted(src.rglob("*.rs")):
        text = rs.read_text(errors="replace")
        rel = str(rs.relative_to(project))
        is_axiom_file = rs.resolve() in axiom_files

        for m in ASSUME_SPEC_RE.finditer(text):
            flagged["assume_specification"].append(
                {"file": rel, "line": _line_of(text, m.start()),
                 "match": m.group(0)}
            )
        for m in RLIMIT_ATTR_RE.finditer(text):
            try:
                n = float(m.group(1))
            except ValueError:
                continue
            if n > 200:
                flagged["rlimit_over_200"].append(
                    {"file": rel, "line": _line_of(text, m.start()),
                     "rlimit": n}
                )
        for m in SPIN_PROVER_RE.finditer(text):
            flagged["spinoff_or_nonlinear"].append(
                {"file": rel, "line": _line_of(text, m.start()),
                 "kind": m.group(1)}
            )

        for name, hstart, bstart, bend in iter_proof_fns(text):
            line = _line_of(text, hstart)
            is_axiom_fn = name.startswith("axiom_")
            if bstart is not None and not is_axiom_fn and not is_axiom_file:
                body = text[bstart:bend]
                body = re.sub(r"//[^\n]*", "", body)
                body = re.sub(r"/\*.*?\*/", "", body, flags=re.S).strip()
                if re.fullmatch(r"assert\s*\(\s*false\s*\)\s*;?", body):
                    flagged["trivial_body_assert_false"].append(
                        {"file": rel, "line": line, "fn": name}
                    )
                elif re.fullmatch(r"unreached\s*\(\s*\)\s*;?", body):
                    flagged["trivial_body_unreached"].append(
                        {"file": rel, "line": line, "fn": name}
                    )
                elif body == "":
                    flagged["empty_body"].append(
                        {"file": rel, "line": line, "fn": name}
                    )

            if (not is_axiom_file and not is_axiom_fn
                    and bstart is not None):
                lemmas.append({"file": rel, "line": line, "fn": name})

    flagged["total_findings"] = sum(
        len(v) for v in flagged.values() if isinstance(v, list)
    )
    return flagged, lemmas


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--results-root", type=Path, default=Path("results"))
    ap.add_argument("--run-id", default="audit_001")
    ap.add_argument("--rlimit", type=float, default=80.0)
    ap.add_argument("--jobs", type=int, default=1)
    args = ap.parse_args()

    project = args.project.resolve()
    results_root = args.results_root.resolve()
    audit_root = results_root / args.run_id
    audit_root.mkdir(parents=True, exist_ok=True)

    modules = LAYER_SETS["ALL"]

    if args.jobs > 1:
        # Warm target/ once so concurrent jobs don't race the cargo build cache.
        try:
            first_file = module_to_file(modules[0], project)
            print(f"[warm-up] cargo verus verify {modules[0]} ...", flush=True)
            run_verus(first_file, modules[0], project, args.rlimit)
        except FileNotFoundError:
            pass

    per_module: list[dict] = []
    if args.jobs <= 1:
        for i, m in enumerate(modules, 1):
            print(f"[{i}/{len(modules)}] {m}", flush=True)
            per_module.append(audit_one_module(
                m, project, results_root, audit_root, args.rlimit,
            ))
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futs = {
                ex.submit(audit_one_module, m, project, results_root,
                          audit_root, args.rlimit): m for m in modules
            }
            done = 0
            for f in as_completed(futs):
                done += 1
                rec = f.result()
                print(f"[{done}/{len(modules)}] {rec.get('module')} "
                      f"verus={rec.get('verus_okay')} "
                      f"drift={rec.get('spec_drift_okay')}",
                      flush=True)
                per_module.append(rec)

    # Preserve dependency order in the report.
    by_name = {r["module"]: r for r in per_module}
    per_module_ordered = [by_name[m] for m in modules if m in by_name]
    audited = [r for r in per_module_ordered if not r.get("skipped")]
    skipped = [r["module"] for r in per_module_ordered if r.get("skipped")]
    n_verus_ok = sum(1 for r in audited if r.get("verus_okay"))
    n_drift_ok = sum(1 for r in audited if r.get("spec_drift_okay"))

    print("[tier1] cheat grep ...", flush=True)
    axiom_files = axiom_module_files(project)
    tier1 = tier1_cheat_grep(project, axiom_files)
    write_json(audit_root / "cheat_grep.json", tier1)

    print("[tier3] suspicious shapes + lemma enumeration ...", flush=True)
    tier3, lemmas = tier3_and_enumerate(project, axiom_files)
    write_json(audit_root / "suspicious_shapes.json", tier3)
    write_json(audit_root / "lemmas_to_audit.json",
               {"count": len(lemmas), "lemmas": lemmas})

    report = {
        "audit_run_id": args.run_id,
        "campaign_runs_audited": ["sweep_all_001", "residue_001",
                                  "montgomery_retry_001", "hard_tail_001"],
        "project_root": str(project),
        "modules_total": len(modules),
        "modules_audited": len(audited),
        "modules_verus_okay": n_verus_ok,
        "modules_spec_drift_okay": n_drift_ok,
        "modules_skipped": skipped,
        "axiom_modules": sorted(AXIOM_MODULES),
        "per_module": per_module_ordered,
        "tier1_clean": tier1.get("clean", False),
        "tier3_suspicious_count": tier3.get("total_findings", 0),
        "lemmas_to_audit_count": len(lemmas),
    }
    write_json(audit_root / "verify_report.json", report)

    print()
    print(f"=== {args.run_id} ===")
    print(f"  modules: {n_verus_ok}/{len(audited)} verus_okay, "
          f"{n_drift_ok}/{len(audited)} spec_drift_okay "
          f"(skipped {len(skipped)})")
    print(f"  tier1 clean: {tier1.get('clean')}")
    print(f"  tier3 suspicious: {tier3.get('total_findings')}")
    print(f"  lemmas to audit (tier4): {len(lemmas)}")
    all_green = (n_verus_ok == len(audited)
                 and n_drift_ok == len(audited)
                 and tier1.get("clean"))
    return 0 if all_green else 1


if __name__ == "__main__":
    raise SystemExit(main())
