#!/usr/bin/env python3
"""check_false_contract.py — harness-private verifier for an agent's
counterexample WITNESS against a FROZEN contract.

This is the witness-checker for extension E7 (docs/extension_spec.md). It is not
an agent-facing proof tool: the agent writes `false_contract_claims.json`, and
`run.py` invokes this checker after the round. The agent supplies only a witness
(concrete values for the fn's params); the predicate (`requires`/`ensures`) is
pulled from the spec_check SNAPSHOT, never from the agent — so a
strawman-weakened predicate can't sneak a false classification past the gate.
The check: substitute the witness, assert every `requires` conjunct HOLDS and
the `ensures` FAILS, and let `cargo verus` decide.

  verified  → requires(witness) ∧ ¬ensures(witness) is machine-proved ⇒ the
              contract is FALSE at the witness (sound; no false positives).
  unconfirmed → verus did not discharge it. Verus/Z3 is sound-but-incomplete
              (nonlinear, triggers, timeout), so this is NOT "the contract is
              true" — only "not machine-confirmed false". Callers treat the
              verified set as a LOWER BOUND.

Usage:
    python skills/check_false_contract.py \
        --snapshot <spec_snapshot.json> --project <cargo_root> \
        --file <abs path to the .rs> --function <fn_name> \
        --witness '{"x":"0","y":"p() + 1"}' [--rlimit 30] \
        [--marker-prefix runid8] [--keep]

Witness values are Verus-expressible CLOSED terms (e.g. "0", "p() + 1",
"2u128"). They are inserted into a generated `let`, so they are syntax-guarded
before injection: no statement separators, braces, comments, or proof-bypass
tokens. A value only definable via an existential or an opaque spec-fn output
cannot be substituted → the check returns unconfirmed.

Prints JSON; exit 0 iff verified.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(os.environ.get(
        "CLI_LOG_PATH", Path(__file__).parents[1] / "cli.log")))],
)
logger = logging.getLogger("check_false_contract")

_FC_FN_RE = re.compile(
    r"(?m)^[ \t]*proof\s+fn\s+_fc_[A-Za-z0-9_]*\s*\(\s*\)\s*\{"
)


def _sanitize_marker_prefix(prefix: str | None) -> str:
    cleaned = re.sub(r"\W+", "_", prefix or "").strip("_")
    return (cleaned[:32] or secrets.token_hex(4))


def _marker_names(prefix: str | None = None) -> tuple[str, str]:
    marker_prefix = _sanitize_marker_prefix(prefix)
    return (f"_fc_{marker_prefix}_witness_check",
            f"_fc_{marker_prefix}_tripwire")


def _derive_module(target: Path, project: Path) -> str | None:
    try:
        rel = target.resolve().relative_to(project.resolve())
    except ValueError:
        return None
    parts = list(rel.parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts:
        parts[-1] = parts[-1].removesuffix(".rs")
    if parts and parts[-1] in ("mod", "lib"):
        parts = parts[:-1]
    return "::".join(parts)


def _cargo_verus_cmd(project: Path, module: str, rlimit: float) -> list[str]:
    cmd = ["cargo", "verus", "verify"]
    try:
        mpkg = re.search(r'^\s*name\s*=\s*"([^"]+)"',
                         (project / "Cargo.toml").read_text(), re.M)
        if mpkg:
            cmd += ["-p", mpkg.group(1)]
    except OSError:
        pass
    return cmd + ["--", "--verify-module", module, "--rlimit", str(rlimit)]


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    """Split on `sep` only at paren/bracket/brace depth 0."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out if x.strip()]


def _params_from_header(header: str) -> list[tuple[str, str]]:
    """Extract (name, type) pairs from a `fn NAME(<params>)` header."""
    m = re.search(r"\bfn\s+\w+\s*(?:<[^>]*>)?\s*\(", header)
    if not m:
        return []
    i = m.end() - 1  # at the '('
    depth, j = 0, i
    for j in range(i, len(header)):
        if header[j] == "(":
            depth += 1
        elif header[j] == ")":
            depth -= 1
            if depth == 0:
                break
    inner = header[i + 1:j]
    params = []
    for p in _split_top_level(inner):
        if ":" not in p:
            continue  # e.g. a bare `self`
        name, typ = p.split(":", 1)
        params.append((name.strip(), typ.strip()))
    return params


def _clean_requires(requires: str) -> str:
    """Defensively trim legacy over-captured `requires` snapshots at the first
    clause keyword."""
    for kw in ("ensures", "decreases"):
        requires = re.split(rf"\b{kw}\b", requires)[0]
    return requires.strip().rstrip(",")


def _clean_clause(c: str) -> str:
    for kw in ("ensures", "decreases"):
        c = re.split(rf"\b{kw}\b", c)[0]
    return c.strip().rstrip(",")


_FORBIDDEN_EXPR = re.compile(
    r"\b(?:assume_specification|assume|admit)\b|external_body|no_method_body"
)


def _balanced_delims(expr: str) -> bool:
    stack = []
    pairs = {")": "(", "]": "["}
    for ch in expr:
        if ch in "([":
            stack.append(ch)
        elif ch in ")]":
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def _validate_witness_expr(name: str, expr: object) -> str | None:
    if not isinstance(expr, str):
        return f"witness value for {name} must be a string expression"
    if not expr.strip():
        return f"witness value for {name} is empty"
    if any(ch in expr for ch in ";\n\r{}"):
        return f"witness value for {name} is not a single closed expression"
    if "/*" in expr or "*/" in expr or "//" in expr:
        return f"witness value for {name} contains a comment"
    forbidden = _FORBIDDEN_EXPR.search(expr)
    if forbidden:
        return (f"witness value for {name} contains forbidden construct "
                f"'{forbidden.group(0)}'")
    if not _balanced_delims(expr):
        return f"witness value for {name} has unbalanced delimiters"
    return None


def build_check_fn(sig: dict, witness: dict,
                   marker_name: str | None = None) -> tuple[str | None, str | None]:
    """Build the witness-substituted proof fn, or (None, reason) if it can't be
    expressed (→ unconfirmed)."""
    header = sig.get("header", "")
    if re.search(r"\(\s*&?\s*(?:mut\s+)?self\b", header):
        return None, "method (self) contracts not supported by the substituter"
    params = _params_from_header(header)
    if not params:
        return None, "could not parse params from header"
    missing = [n for n, _ in params if n not in witness]
    if missing:
        return None, f"witness missing values for params: {missing}"
    for n, _ in params:
        reason = _validate_witness_expr(n, witness[n])
        if reason:
            return None, reason
    reqs = _split_top_level(_clean_requires(sig.get("requires", "")))
    # The postcondition is the CONJUNCTION of the ensures clauses, so the
    # contract is false at the witness iff that conjunction fails:
    # ¬(e1 ∧ … ∧ en). Build a single negated, parenthesized conjunction — NOT
    # `!(e1, e2)`, which is invalid Verus.
    ens_clauses = _split_top_level(_clean_clause(sig.get("ensures", "")))
    if not ens_clauses:
        return None, "no ensures clause to refute"
    ens_neg = "!(" + " && ".join(f"({c})" for c in ens_clauses) + ")"
    lets = "\n".join(
        f"        let {n}: {t} = {witness[n]};" for n, t in params)
    req_asserts = "\n".join(f"        assert({r});" for r in reqs)
    marker_name = marker_name or _marker_names()[0]
    body = (
        f"proof fn {marker_name}() {{\n"
        f"{lets}\n"
        f"{req_asserts}\n"
        f"        assert({ens_neg});\n"
        f"    }}\n"
    )
    return body, None


def _normalize_witness(sig: dict, witness: object) -> tuple[dict | None, str | None]:
    """Accept the documented object form, plus legacy scalar form for one param."""
    if isinstance(witness, dict):
        return witness, None
    params = _params_from_header(sig.get("header", ""))
    if len(params) == 1:
        name = params[0][0]
        if isinstance(witness, str):
            return {name: witness}, None
        return None, (
            f"witness for sole param {name} must be a string expression "
            "or JSON object {param: value}")
    if not params:
        return None, "witness must be a JSON object {param: value}; could not parse params"
    return None, (
        "witness must be a JSON object {param: value}; scalar witnesses "
        "are only accepted for one-param functions")


def _strip_fc_markers(text: str) -> str:
    """Remove zero-arg `proof fn _fc_*` scratch/checker blocks (balanced-brace).

    This covers old fixed checker markers, nonce markers, and agent scratch fns
    with the reserved `_fc_` prefix. A leaked checker run or mid-probe scratch fn
    then self-heals on the next injection instead of poisoning the shared
    worktree with a duplicate-definition compile error. Idempotent; returns
    `text` unchanged when no marker is present.

    Critical because resumes share ONE worktree: a single missed restore would
    otherwise break every future check of that file."""
    while True:
        m = _FC_FN_RE.search(text)
        if not m:
            break
        depth, end = 0, m.end() - 1            # at the opening '{'
        for end in range(m.end() - 1, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
        if depth != 0:
            return text
        line_end = text.find("\n", end)
        close_line = text[end:] if line_end == -1 else text[end:line_end]
        if "// verus!" in close_line:
            return text
        start = m.start()
        while start > 0 and text[start - 1] in " \t":   # trim indent
            start -= 1
        if start > 0 and text[start - 1] == "\n":        # and a leading blank
            start -= 1
        stop = end + 1
        if stop < len(text) and text[stop] == "\n":      # and trailing newline
            stop += 1
        text = text[:start] + text[stop:]
    return text


def _inject(path: Path, fn_text: str) -> str:
    """Insert `fn_text` just before the closing `} // verus!` (or the last `}`),
    so it lives in the module with the spec vocabulary in scope. Returns the
    original text (for restore) — with any stale `_fc_*` markers already stripped,
    so the restore writes back a clean baseline even if a prior run leaked one."""
    orig = _strip_fc_markers(path.read_text())
    anchor = orig.rfind("} // verus!")
    if anchor == -1:
        # fall back to the final top-level closing brace
        anchor = orig.rstrip().rfind("}")
        if anchor == -1:
            raise ValueError("no verus! block / closing brace to inject before")
    new = orig[:anchor] + "\n" + fn_text + "\n" + orig[anchor:]
    path.write_text(new)
    return orig


_RUSTC_ERR_RE = re.compile(r"error\[E\d{3,4}\]")
_PARSE_ERR_PHRASES = (
    "unclosed delimiter",
    "mismatched closing delimiter",
    "this file contains an unclosed",
)


def _rustc_compile_failed(stderr: str) -> bool:
    """True iff the crate failed to COMPILE (rustc/parse errors), as distinct from
    a Verus SEMANTIC failure (`assertion failed` / `precondition not satisfied`).

    The distinction matters because a Verus assertion failure ALSO prints
    "could not compile", so that string can't discriminate. Only rustc errors
    carry `error[E####]` codes (e.g. E0428 duplicate def, E0308 type mismatch) or
    are unclosed-delimiter parse errors. When the crate doesn't compile, verus
    never analyzed the injected region, so the line-bucket tripwire/witness signals
    are unreliable and the result MUST be `verus_did_not_reach_region` — never a
    confirmed false-contract or a witness_assert_failed read off a compile error."""
    if _RUSTC_ERR_RE.search(stderr):
        return True
    return any(p in stderr for p in _PARSE_ERR_PHRASES)


def _err_lines_for(stderr: str, basename: str) -> set[int]:
    """Line numbers of verus errors that point at `basename`."""
    return {int(m.group(1))
            for m in re.finditer(rf"{re.escape(basename)}:(\d+):", stderr)}


def _outside_ranges(lines: set[int], *ranges: range) -> set[int]:
    """Lines not covered by any trusted injected-region range."""
    return {ln for ln in lines if not any(ln in r for r in ranges)}


def _classify_checked_region(compile_failed: bool, module_not_clean: bool,
                             tripwire_fired: bool, witness_failed: bool
                             ) -> tuple[bool, str]:
    """Return (verified, failure_class) for the trusted-region signals."""
    verified = ((not compile_failed) and (not module_not_clean)
                and tripwire_fired and not witness_failed)
    if compile_failed:
        return False, "verus_did_not_reach_region"
    if module_not_clean:
        return False, "module_not_clean"
    if verified:
        return True, "false_contract"
    if witness_failed:
        return False, "witness_assert_failed"
    if not tripwire_fired:
        return False, "verus_did_not_reach_region"
    return False, "unconfirmed"


def run(snapshot: Path, project: Path, file: Path, function: str,
        witness: object, rlimit: float, keep: bool, timeout: float = 120.0,
        marker_prefix: str | None = None) -> dict:
    def fail(cls, reason, **extra):
        return {"okay": False, "verified": False, "classification": cls,
                "reason": reason, "function": function, **extra}

    snap = json.loads(snapshot.read_text())
    files = snap.get("files", {})
    # Resolve the snapshot entry by EXACT resolved path; fall back to basename
    # but then INJECT INTO THE SNAPSHOT'S file, never a caller-supplied path that
    # might differ — contract and injection target must be the same file.
    want = str(file.resolve())
    entry_key = None
    if want in files:
        entry_key = want
    else:
        for k in files:
            try:
                if Path(k).resolve() == file.resolve():
                    entry_key = k; break
            except OSError:
                continue
        if entry_key is None:
            cands = [k for k in files if Path(k).name == file.name]
            if len(cands) == 1:
                entry_key = cands[0]
            elif len(cands) > 1:
                return fail("unconfirmed", f"ambiguous basename {file.name} "
                            f"matches {len(cands)} snapshot files")
    if entry_key is None:
        return fail("unconfirmed", f"{file.name} not in snapshot")
    sigs = files[entry_key].get("sigs", {})
    if function not in sigs:
        return fail("unconfirmed", f"{function} not in snapshot for {file.name}")
    sig = sigs[function]
    inject_path = Path(entry_key)
    if not inject_path.is_file():
        return fail("file_missing", f"snapshot file not on disk: {inject_path}")
    module = _derive_module(inject_path, project)
    if not module:
        return fail("unconfirmed", f"could not derive module for {inject_path}")

    witness, reason = _normalize_witness(sig, witness)
    if witness is None:
        return fail("unconfirmed", reason)

    marker_name, tripwire_name = _marker_names(marker_prefix)
    fn_text, reason = build_check_fn(sig, witness, marker_name)
    if fn_text is None:
        return fail("unconfirmed", reason)

    tripwire = (f"proof fn {tripwire_name}() {{\n        assert(false);\n    }}\n")
    block = fn_text + "\n" + tripwire
    orig = _inject(inject_path, block)
    timed_out = False
    try:
        cmd = _cargo_verus_cmd(project, module, rlimit)
        logger.info("check_false_contract: %s::%s cmd=%s",
                    inject_path.name, function, " ".join(cmd))
        # `cargo verus` spawns descendants (z3); run it in its own session and
        # kill the whole group on timeout, matching the harness lifecycle
        # discipline used for the main claude/verus subprocesses.
        proc = subprocess.Popen(
            cmd, cwd=str(project), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        try:
            _, stderr = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
            proc.communicate()
            timed_out = True
            stderr, rc = "", None
    finally:
        if not keep:
            inject_path.write_text(orig)
    if timed_out:
        return fail("unconfirmed", f"verus timed out after {timeout:.0f}s",
                    failure_class="verus_timeout", returncode=None)

    # Line ranges of the two injected fns in the (restored) modified file —
    # recomputed from the same anchor + block we injected.
    anchor = orig.rfind("} // verus!")
    if anchor == -1:
        anchor = orig.rstrip().rfind("}")
    base_line = orig[:anchor].count("\n") + 2          # first injected line
    w_nlines = fn_text.count("\n")
    w_range = range(base_line, base_line + w_nlines + 1)
    t_start = base_line + w_nlines + 1                  # blank line then tripwire
    t_range = range(t_start, t_start + tripwire.count("\n") + 1)

    err_lines = _err_lines_for(stderr, inject_path.name)
    witness_failed = any(ln in w_range for ln in err_lines)
    tripwire_fired = any(ln in t_range for ln in err_lines)
    foreign_err_lines = sorted(_outside_ranges(err_lines, w_range, t_range))
    module_not_clean = bool(foreign_err_lines)

    # If the crate did not COMPILE (rustc/parse errors — e.g. a leaked-injection
    # duplicate def, or the agent's mid-reconstruction module is syntactically
    # broken), verus never analyzed the injected region. The line-bucket signals
    # above are then unreliable: an `error[E0428]` on the tripwire line looks like
    # the tripwire "fired", and an `error[E0308]` on a witness line looks like the
    # witness "failed". So a compile failure forces `verus_did_not_reach_region`,
    # NEVER a confirmed false-contract and never witness_assert_failed read off a
    # compile error. (Verus semantic failures carry no E-code, so a real assertion
    # failure in a compiling crate still flows to the normal classification.)
    compile_failed = _rustc_compile_failed(stderr)

    # VERIFIED iff the crate compiled, verus actually checked this region (tripwire
    # fired), no error fell on the witness fn (all its asserts —
    # requires(w) ∧ ¬ensures(w) — held), AND no independent error in this module
    # fell outside the injected ranges. If the module is already dirty, line
    # bucketing is not enough evidence for either a verified false contract or a
    # trustworthy witness failure.
    verified, failure_class = _classify_checked_region(
        compile_failed, module_not_clean, tripwire_fired, witness_failed)
    return {
        "okay": verified,
        "verified": verified,
        "classification": "false_contract" if verified else "unconfirmed",
        "function": function,
        "file": inject_path.name,
        "witness": witness,
        "returncode": rc,
        "failure_class": failure_class,
        "tripwire_fired": tripwire_fired,
        "witness_failed": witness_failed,
        "compile_failed": compile_failed,
        "module_not_clean": module_not_clean,
        "foreign_error_lines": foreign_err_lines,
        "stderr_excerpt": stderr[-1200:],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--file", type=Path, required=True)
    ap.add_argument("--function", required=True)
    ap.add_argument("--witness", required=True, help="JSON object {param: value}")
    ap.add_argument("--rlimit", type=float, default=30.0)
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="kill cargo verus (+ its z3 group) after N seconds")
    ap.add_argument("--marker-prefix",
                    help="reserved _fc_<prefix> marker namespace for this check")
    ap.add_argument("--keep", action="store_true",
                    help="leave the injected fn in place (debug)")
    args = ap.parse_args()
    try:
        witness = json.loads(args.witness)
    except json.JSONDecodeError as e:
        print(json.dumps({"okay": False, "verified": False,
                          "classification": "unconfirmed",
                          "reason": f"bad --witness JSON: {e}"}))
        return 2
    result = run(args.snapshot, args.project, args.file, args.function,
                 witness, args.rlimit, args.keep, args.timeout,
                 marker_prefix=args.marker_prefix)
    print(json.dumps(result, indent=2))
    return 0 if result.get("verified") else 1


if __name__ == "__main__":
    sys.exit(main())
