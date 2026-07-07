#!/usr/bin/env python3
"""Run cargo verus on a target module. Return axle-compatible JSON.

Usage:
    python skills/verus_check.py <file.rs> [--project <cargo_root>] [--module <name>]
    python skills/verus_check.py --whole-crate --project <cargo_root>

Output (stdout):
    {"okay": bool,
     "messages": [{file, line, column, severity, data, message}],
     "errors": [...], "message_texts": [...], "error_texts": [...],
     "failed_declarations": [name, ...]}

Exit code mirrors cargo verus (0 = okay, non-zero = failed).
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(os.environ.get(
        "CLI_LOG_PATH", Path(__file__).parents[1] / "cli.log")))],
)
logger = logging.getLogger("verus_check")


# Pattern: "error: message" + "  --> path/file.rs:LINE:COL" pairs
_ERR_HEADER_RE = re.compile(r"^(?P<severity>error|warning|note)(?:\[[^\]]+\])?:\s*(?P<msg>.+)")
_ERR_LOC_RE = re.compile(r"^\s*-->\s*(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)")
_FAILED_DECL_RE = re.compile(r"error:\s*(?:precondition|postcondition|assertion|invariant)[^`]*`(?P<name>[^`]+)`")
_VERUS_SUMMARY_RE = re.compile(
    r"verification results::\s*(?P<verified>\d+)\s+verified,\s*"
    r"(?P<errors>\d+)\s+errors?",
    re.I,
)
# A Verus *internal* interpreter panic crashes the process before structured
# diagnostics are emitted; the only actionable user-code span rides in the panic
# blob as: as_string: "curve25519-dalek/src/edwards.rs:341:33: 341:39 (#0)"
_PANIC_SPAN_RE = re.compile(
    r'as_string:\s*"(?P<file>[^"]+\.rs):(?P<line>\d+):(?P<col>\d+):\s*\d+:\d+'
)
_PANIC_MSG_RE = re.compile(
    r"thread 'interpreter'.*?panicked at [^\n]+:\n(?P<msg>.+)",
    re.S,
)


def find_cargo_root(target: Path) -> Path:
    p = target.parent if target.is_file() else target
    while p != p.parent:
        if (p / "Cargo.toml").exists():
            return p
        p = p.parent
    return target.parent


def parse_diagnostics(stderr: str) -> list[dict]:
    messages: list[dict] = []
    lines = stderr.splitlines()
    i = 0
    while i < len(lines):
        m = _ERR_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        sev, msg = m.group("severity"), m.group("msg")
        file_, line, col = None, 0, 0
        # Look at next few lines for a location pointer
        for j in range(i + 1, min(i + 6, len(lines))):
            lm = _ERR_LOC_RE.match(lines[j])
            if lm:
                file_, line, col = lm.group("file"), int(lm.group("line")), int(lm.group("col"))
                break
        messages.append({
            "file": file_ or "",
            "line": line,
            "column": col,
            "severity": sev,
            "data": msg,
        })
        i += 1
    return messages


def extract_failed_declarations(stderr: str) -> list[str]:
    names = set()
    for m in _FAILED_DECL_RE.finditer(stderr):
        names.add(m.group("name"))
    return sorted(names)


def parse_verification_summary(stdout: str, stderr: str) -> dict:
    """Parse Verus' raw final VC summary, if cargo/verus emitted one."""
    text = "\n".join(part for part in (stdout or "", stderr or "") if part)
    matches = list(_VERUS_SUMMARY_RE.finditer(text))
    if not matches:
        return {"verified_count": None, "raw_verus_error_count": None}
    match = matches[-1]
    return {
        "verified_count": int(match.group("verified")),
        "raw_verus_error_count": int(match.group("errors")),
    }


def assess_truncation(raw_summary: dict, returncode: int,
                      error_messages: list) -> tuple[bool, list[dict]]:
    """Detect a verifier run that died before reporting completion.

    Verus prints "verification results:: N verified, M errors" exactly once
    per completed run; when that line is absent the run aborted
    mid-verification (internal worker panic, build failure, crash) and every
    error parsed from it is a LOWER BOUND, not the remaining frontier.
    Observed live: a vir/src/poly.rs worker panic truncated whole-crate runs
    for ~40 rounds across three attempts while the parsed queue said "only 1
    error remains" (field_floor stage3, 2026-07-04).

    Returns (truncated, extra_messages). extra_messages carries a labeled
    note for the agent/feedback path — and, when the dead run would otherwise
    look green (rc==0, zero errors), a fail-closed error so a run that never
    reported completing can never read as verified.
    """
    truncated = raw_summary.get("verified_count") is None
    if not truncated:
        return False, []
    extra: list[dict] = []
    has_real_error = any(
        isinstance(m, dict) and m.get("severity") == "error"
        for m in error_messages
    )
    if returncode == 0 and not has_real_error:
        extra.append({
            "file": "", "line": 0, "column": 0, "severity": "error",
            "data": ("[verus_check] verifier exited 0 but never printed its "
                     "'verification results' summary — treating as NOT "
                     "verified (fail closed)."),
        })
    extra.append({
        "file": "", "line": 0, "column": 0, "severity": "note",
        "data": ("[verus_check] TRUNCATED RUN: the verifier exited without "
                 "its final 'verification results' summary (internal panic "
                 "or build failure). The error list above is PARTIAL — a "
                 "lower bound, not the remaining frontier. Do NOT conclude "
                 "'only these remain'; re-check each editable file with a "
                 "module-scoped verus_check."),
    })
    return True, extra


def extract_verus_panic_messages(stderr: str) -> list[dict]:
    """Promote Verus interpreter-panic spans from stderr into structured errors.

    A Verus *internal* panic crashes the process before normal diagnostics are
    emitted, so `parse_diagnostics` sees only the generic "could not compile"
    line — and run.py's next-round feedback (built from `messages[]`) would hand
    the agent that useless line instead of the actual location. The actionable
    user-code span lives only in the panic blob (`as_string: "<file>:<l>:<c>:
    ..."`); lift it into a structured error so the round-history path shows it.
    """
    spans = []
    for m in _PANIC_SPAN_RE.finditer(stderr):
        spans.append({
            "file": m.group("file"),
            "line": int(m.group("line")),
            "column": int(m.group("col")),
            "severity": "error",
            "data": ("Verus internal panic while interpreting this expression; "
                     "rewrite or simplify the local proof expression."),
        })
    return spans[:3]


def derive_module(target: Path, project_root: Path) -> str:
    rel = target.resolve().relative_to(project_root.resolve())
    parts = list(rel.parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts:
        parts[-1] = parts[-1].removesuffix(".rs")
    if parts and parts[-1] in ("mod", "lib"):
        parts = parts[:-1]
    return "::".join(parts)


def default_whole_crate_target(project_root: Path) -> Path:
    """Pick a stable diagnostic anchor for targetless whole-crate checks."""
    for rel in ("src/lib.rs", "src/main.rs", "Cargo.toml"):
        candidate = project_root / rel
        if candidate.exists():
            return candidate
    return project_root


def resolve_target_and_project(
    target_arg: Path | None,
    project_arg: Path | None,
    whole_crate: bool,
) -> tuple[Path, Path]:
    """Resolve CLI target/project, tolerating targetless whole-crate checks."""
    if target_arg is None:
        if not whole_crate or project_arg is None:
            raise ValueError(
                "target is required unless --whole-crate and --project are both supplied")
        project = project_arg.resolve()
        return default_whole_crate_target(project).resolve(), project

    if project_arg is not None:
        project = project_arg.resolve()
        target = target_arg if target_arg.is_absolute() else project / target_arg
        return target.resolve(), project

    target = target_arg.resolve()
    project = find_cargo_root(target).resolve()
    return target, project


def target_usage_messages(target: Path, project: Path, whole_crate: bool) -> list[dict]:
    mode = "whole-crate" if whole_crate else "module"
    examples = (
        f"python3 /opt/harness/skills/verus_check.py {project / 'src' / 'ristretto.rs'} "
        f"--project {project} --whole-crate; "
        f"python3 /opt/harness/skills/verus_check.py "
        f"{project / 'src' / 'lemmas' / 'edwards_lemmas' / 'niels_addition_correctness.rs'} "
        f"--project {project}"
    )
    return [{
        "severity": "error",
        "data": (
            f"File not found: {target}. Invalid verus_check {mode} target: "
            "the positional target must be an absolute or project-relative .rs "
            "file path, not a crate name or module name. Use --whole-crate for "
            f"package-wide checks. Examples: {examples}"
        ),
        "line": 0,
        "column": 0,
        "file": str(target),
    }]


def summarize_messages(messages: list[dict]) -> str:
    """Compact, grouped-by-file view of error messages for at-a-glance reading,
    so the agent uses this complete structured view instead of falling back on
    `cargo verus | grep | head` (which truncates and causes false "it's fixed"
    conclusions, then expensive whole-crate rechecks)."""
    errs = [m for m in messages if m.get("severity") == "error"]
    if not errs:
        return ""
    by_file: dict[str, list[dict]] = {}
    for m in errs:
        fp = m.get("file") or "?"
        # src-relative path disambiguates same-basename files — this codebase
        # has both a top-level scalar.rs and a backend .../u64/scalar.rs.
        f = fp.split("/src/", 1)[1] if "/src/" in fp else fp.split("/")[-1]
        by_file.setdefault(f, []).append(m)
    out = []
    for f in sorted(by_file):
        ms = by_file[f]
        locs = ", ".join(str(m.get("line", 0)) for m in ms)
        kinds: dict[str, int] = {}
        for m in ms:
            k = (m.get("data") or "").strip().split("\n")[0][:50]
            kinds[k] = kinds.get(k, 0) + 1
        kindstr = "; ".join(f"{k} x{n}" if n > 1 else k
                            for k, n in kinds.items())
        out.append(f"{f}: {len(ms)} error(s) @ lines {locs} — {kindstr}")
    return "\n".join(out)


def diagnostic_text(message: object) -> str:
    """Human-readable one-line text for a structured Verus diagnostic."""
    if not isinstance(message, dict):
        return str(message)
    data = str(message.get("data") or message.get("message") or "")
    file_ = str(message.get("file") or "")
    line = int(message.get("line") or 0)
    column = int(message.get("column") or 0)
    loc = file_
    if line:
        loc += f":{line}" if loc else str(line)
        if column:
            loc += f":{column}"
    return f"{loc}: {data}" if loc and data else (data or loc)


def with_errors_alias(result: dict) -> dict:
    """Expose structured diagnostics plus text aliases for legacy consumers."""
    result.setdefault("verified_count", None)
    result.setdefault("raw_verus_error_count", None)
    result.setdefault("truncated", False)
    result.setdefault("num_errors", result.get("error_count"))
    result.setdefault("num_verified", result.get("verified_count"))
    messages = result.get("messages", [])
    if not isinstance(messages, list):
        messages = []
        result["messages"] = messages
    for message in messages:
        if isinstance(message, dict) and "message" not in message:
            message["message"] = str(message.get("data") or "")
    result["errors"] = messages
    result["message_texts"] = [diagnostic_text(m) for m in messages]
    result["error_texts"] = [
        diagnostic_text(m) for m in messages
        if not isinstance(m, dict) or m.get("severity") == "error"
    ]
    return result


def run(target: Path, project_root: Path, module: str | None, timeout: int,
        rlimit: float | None = None, whole_crate: bool = False) -> dict:
    import os, signal as _signal
    # whole_crate: verify the ENTIRE package (no --verify-module). Needed when
    # an edit changes an `open spec fn` that other modules unfold — only a
    # whole-crate pass re-checks every consumer (e.g. the --no-bridge-specs
    # rung, where montgomery::to_edwards's frozen proof PINS the reconstructed
    # bridge and must be re-verified against it each round).
    mod = None if whole_crate else (module or derive_module(target, project_root))
    # `cargo verus verify` runs `cargo build` and passes post-`--` args to verus.
    #
    # We use `--verify-module M` (NOT `--verify-only-module M`). The latter
    # checks ONLY the top-level module M — sub-modules like `M::decompress`,
    # `M::tests`, and any `mod X { }` blocks inside the file are SILENTLY
    # SKIPPED. This was a real harness bug: ristretto.rs has `mod decompress`
    # holding step_1/step_2; their proofs went unverified for the entire
    # campaign because `--verify-only-module ristretto` excluded them.
    # `--verify-module M` includes M and all its descendants.
    cmd = ["cargo", "verus", "verify"]
    # If project_root is inside a Cargo workspace, scope verification to the
    # member package so other workspace deps (e.g. vstd) aren't re-verified
    # and won't reject --verify-module that points to a member-local module.
    try:
        cargo_toml = (project_root / "Cargo.toml").read_text()
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', cargo_toml, re.M)
        pkg_name = m.group(1) if m else None
        parent_toml = project_root.parent / "Cargo.toml"
        if pkg_name and parent_toml.exists() and "[workspace]" in parent_toml.read_text():
            cmd += ["-p", pkg_name]
    except Exception:
        pass
    verus_args: list[str] = []
    if mod:
        verus_args += ["--verify-module", mod]
    if rlimit is not None:
        verus_args += ["--rlimit", str(rlimit)]
    if verus_args:
        cmd += ["--"] + verus_args

    logger.info("verus_check: cmd=%s cwd=%s", " ".join(cmd), project_root)
    # Use Popen + start_new_session=True so cargo verus + rust_verify + z3 all
    # share a process group we can SIGKILL together. subprocess.run's timeout
    # only kills the direct child (cargo verus), leaving z3 orphaned.
    proc = subprocess.Popen(
        cmd, cwd=str(project_root),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the entire process group (cargo verus, rust_verify, z3, ...)
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        timeout_msgs = [{
            "file": str(target), "line": 0, "column": 0,
            "severity": "error",
            "data": (f"verus timed out after {timeout}s and was killed "
                     f"(cargo + z3 + rust_verify). Proof likely too complex "
                     f"for SMT — split into smaller lemmas with explicit "
                     f"intermediate `assert(...) by (...)` steps."),
        }]
        return with_errors_alias({
            "okay": False,
            "error_count": 1,
            "summary": summarize_messages(timeout_msgs),
            "messages": timeout_msgs,
            "warning_count": 0,
            "failed_declarations": [],
            "returncode": -9,
            # A killed run never reported completing; its counts are partial.
            "truncated": True,
            "stderr_tail": "",
        })

    # `proc.communicate()` already captured stdout/stderr above
    stderr = stderr or ""
    raw_summary = parse_verification_summary(stdout, stderr)
    all_messages = parse_diagnostics(stderr)
    errors = [m for m in all_messages if m["severity"] == "error"]
    warnings = [m for m in all_messages if m["severity"] == "warning"]
    # A Verus interpreter panic crashes before structured diagnostics exist, so
    # the normal parser is blind: `errors` holds only the generic, location-less
    # "could not compile" line. In that case ONLY, promote the panic's user-code
    # span(s) (lifted from stderr) so run.py's next-round feedback shows the
    # agent the actionable file:line instead of a dead-end compile error.
    panic_errors = extract_verus_panic_messages(stderr)
    generic_only = (
        proc.returncode != 0
        and panic_errors
        and not any(m.get("file") and m.get("line") for m in errors)
    )
    if generic_only:
        errors = panic_errors + errors[:1]
    has_error = bool(errors) or proc.returncode != 0
    # If verus hit its SMT resource limit (docs/diagnostics.md §3 — a recurring
    # nonlinear-arith failure), attach a clearly-labeled harness hint so the
    # agent gets actionable guidance instead of just the raw "rlimit exceeded"
    # line. It rides in `messages[]` (severity "note") on purpose: run.py carries
    # messages forward into next-round feedback, so a top-level field would be
    # dropped. Appended AFTER `has_error` is computed, so `okay` is unaffected.
    if any(re.search(r"rlimit|resource limit exceeded", m["data"], re.I)
           for m in errors):
        errors = errors + [{
            "file": "", "line": 0, "column": 0, "severity": "note",
            "data": ("[verus_check hint] SMT resource limit hit. First raise "
                     "--rlimit (e.g. --rlimit 80). If the failing step is "
                     "nonlinear (assert_nonlinear_by, products like x*y), don't "
                     "brute-force one `by (nonlinear_arith)` block — decompose "
                     "into small distributivity steps "
                     "(lemma_mul_is_distributive_add, "
                     "crate::lemmas::common_lemmas::mul_lemmas). Avoid "
                     "`broadcast use` here. See docs/diagnostics.md §3."),
        }]
    # Truncation policy: a run with no final Verus summary never reported
    # completing — flag it so run.py holds it indeterminate (like a timeout)
    # instead of scoring its partial error list as the frontier, and fail
    # closed if it would otherwise look green.
    truncated, truncation_msgs = assess_truncation(
        raw_summary, proc.returncode, errors)
    if truncation_msgs:
        errors = errors + truncation_msgs
        has_error = has_error or any(
            m.get("severity") == "error" for m in truncation_msgs)
    return with_errors_alias({
        "okay": not has_error,
        "truncated": truncated,
        "error_count": sum(1 for m in errors if m.get("severity") == "error"),
        # Grouped, COMPLETE error view — read this (or `messages`), not a
        # truncated `cargo verus | grep | head`.
        "summary": summarize_messages(errors),
        # `messages` holds real errors, plus — on an rlimit failure — one
        # labeled harness hint (severity "note"); see the block above.
        "messages": errors,
        "warning_count": len(warnings),
        "failed_declarations": extract_failed_declarations(stderr),
        "returncode": proc.returncode,
        **raw_summary,
        # Raw stderr tail kept for context only — it is TRUNCATED (last 4000
        # chars), so for many-error whole-crate runs it omits earlier errors.
        # Use `summary`/`messages[]` as the source of truth, not this.
        "stderr_tail": stderr[-4000:] if stderr else "",
    })


def main() -> None:
    ap = argparse.ArgumentParser(description="Run cargo verus; emit JSON")
    ap.add_argument("target", type=Path, nargs="?")
    ap.add_argument("--project", type=Path, default=None,
                    help="Cargo project root (auto-detected if omitted)")
    ap.add_argument("--module", help="Override the --verify-module argument "
                    "(module path to scope verification to)")
    ap.add_argument("--whole-crate", action="store_true",
                    help="Verify the whole package (no --verify-module). Used "
                         "by the --no-bridge-specs rung so cross-module spec "
                         "consumers (montgomery::to_edwards) are re-checked.")
    ap.add_argument("--timeout", type=int, default=None,
                    help="Verus check timeout in seconds (default 300 for "
                         "module checks, 900 for --whole-crate).")
    ap.add_argument("--rlimit", type=float, default=None,
                    help="Pass --rlimit FLOAT to verus (SMT resource limit, "
                         "roughly seconds). Default 10. Increase for "
                         "complex proofs that hit per-fn rlimit ceilings.")
    args = ap.parse_args()

    try:
        target, project = resolve_target_and_project(
            args.target, args.project, args.whole_crate)
    except ValueError as e:
        ap.error(str(e))

    if not target.exists() or not target.is_file() or target.suffix != ".rs":
        nf_msgs = target_usage_messages(target, project, args.whole_crate)
        print(json.dumps(with_errors_alias({
            "okay": False,
            "error_count": 1,
            "summary": summarize_messages(nf_msgs),
            "messages": nf_msgs,
            "warning_count": 0,
            "failed_declarations": [],
            "returncode": 1,
            "stderr_tail": "",
        })), flush=True)
        sys.exit(1)

    logger.info("verus_check target=%s project=%s module=%s",
                target, project, args.module)

    # Whole-crate verification of the full dalek crate runs ~590s; default to a
    # budget above that so the agent's own `--whole-crate` calls don't hit the
    # 300s module-check wall (mirrors run.py's harness-side _WHOLE_CRATE timeout).
    eff_timeout = (args.timeout if args.timeout is not None
                   else (900 if args.whole_crate else 300))
    result = run(target, project, args.module, eff_timeout, args.rlimit,
                 whole_crate=args.whole_crate)
    logger.info("verus_check result: okay=%s errors=%d",
                result["okay"],
                sum(1 for m in result["messages"] if m["severity"] == "error"))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["okay"] else 1)


if __name__ == "__main__":
    main()
