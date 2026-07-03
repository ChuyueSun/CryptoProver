"""Axiom-aware admit() counting and inventory, plus admit-skeleton creation.

Single source of truth for the question "how many actionable admits
are in this file?" Both the harness's COMPLETE gate and the
`admit_inventory` CLI flow through here, so they cannot disagree.

Also the inverse direction — turning proven source into an `admit()`
skeleton. `admit_proof_fn_bodies` / `admit_proof_blocks` (and their brace
helpers `find_proof_fn_body_brace` / `find_matching_brace`) are ported
from inference-dalek's `eval/starting_state.py` — the mode-aware admitter
behind `construct_admitted_state`, which builds the `eval/admitted-*`
branches fed to `run.py --admitted-ref`. It admits ONLY `proof fn` bodies
and inline `proof { ... }` blocks; it skips `axiom_*` (trusted), leaves
`spec fn` definitions intact, and preserves all exec code. This is
deliberately NOT `code_utils.strip_fn_body_to_admit`, which is mode-blind
and would wipe an exec fn's body wholesale.

The counter and its regexes are the single source of truth for the
harness COMPLETE gate and `skills/admit_inventory.py`. DO NOT
reimplement the algorithm elsewhere — call `count_non_axiom` /
`classify_admit_lines` / `inventory_file` from this module.

The algorithm strips comments/strings, finds real `proof fn axiom_*`
body spans with the same brace finder used by the admit skeletonizer,
then classifies each surviving `admit()` line by whether its source
position is inside one of those spans. This handles nested axiom bodies
and Verus contract expressions such as `requires ({ ... })` or
`if c { ... } else { ... }` without treating those contract braces as
the function body.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable


_AXIOM_PREFIX = r"(?:pub(?:\s*\([^)]*\))?|broadcast|open|closed)"

# Same header shape, but captures the full `axiom_*` name. Used by the
# harness's axiom-integrity gate (see run.py) to diff the set of axiom
# declarations against a pre-run baseline.
_AXIOM_FN_NAME_RE = re.compile(
    rf"^\s*(?:{_AXIOM_PREFIX}\s+)*proof\s+fn\s+(axiom_\w+)",
    re.MULTILINE,
)

# A single Rust char literal: 'x' or an escape like '\n' / '\'' / '\\'.
# Deliberately requires a closing quote, so a lifetime (`'a`) does NOT match
# and passes through as ordinary code.
_CHAR_LIT_RE = re.compile(r"'(?:\\.|[^'\\])'")


def strip_comments_strings(text: str) -> str:
    """Blank Rust comments and string/char-literal *contents* so a later
    ``"admit()" in line`` test matches only real code.

    Output is the same length as the input (every consumed char maps to
    exactly one output char — a space, the char itself, or a preserved
    newline), so 1-indexed line numbers from ``splitlines()`` are unchanged.

    Handles ``//`` line comments, ``/* */`` block comments (Rust-nestable),
    ``"..."`` strings (with ``\\`` escapes), ``r"..."`` / ``r#"..."#`` raw
    strings, and ``'x'`` char literals (but not ``'a`` lifetimes). Best-effort,
    not a full lexer — its job is only to stop an ``admit()`` that lives inside
    a comment or a string literal (e.g. ``let _ = "https://x"; admit();``, where
    a naive ``split("//")`` would hide the *real* admit) from being miscounted
    in EITHER direction.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        # line comment: blank to (not including) the newline
        if two == "//":
            while i < n and text[i] != "\n":
                out.append(" "); i += 1
            continue
        # block comment (nestable): blank through the matching */
        if two == "/*":
            depth = 1
            out.append(" "); out.append(" "); i += 2
            while i < n and depth > 0:
                t = text[i:i + 2]
                if t == "/*":
                    depth += 1; out.append(" "); out.append(" "); i += 2
                elif t == "*/":
                    depth -= 1; out.append(" "); out.append(" "); i += 2
                else:
                    out.append("\n" if text[i] == "\n" else " "); i += 1
            continue
        # raw string: r"..." / r#"..."# (N hashes) — keep the leading `r`
        if c == "r":
            j = i + 1
            h = 0
            while j < n and text[j] == "#":
                h += 1; j += 1
            if j < n and text[j] == '"':
                out.append("r")
                out.extend(" " * h)
                out.append(" ")  # opening quote
                i = j + 1
                close = '"' + ("#" * h)
                while i < n:
                    if text[i:i + len(close)] == close:
                        out.extend(" " * len(close)); i += len(close); break
                    out.append("\n" if text[i] == "\n" else " "); i += 1
                continue
            # not a raw string — fall through and emit the `r` as code
        # normal string literal
        if c == '"':
            out.append(" "); i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    # Escaped pair (incl. `\`<newline> line-continuation):
                    # blank the backslash but PRESERVE a following newline,
                    # else line numbers downstream shift by one.
                    out.append(" ")
                    out.append("\n" if text[i + 1] == "\n" else " ")
                    i += 2; continue
                if text[i] == '"':
                    out.append(" "); i += 1; break
                out.append("\n" if text[i] == "\n" else " "); i += 1
            continue
        # char literal 'x' / '\n' (NOT a lifetime 'a) — blank it so a quote
        # inside (e.g. '"') can't open a phantom string
        if c == "'":
            m = _CHAR_LIT_RE.match(text, i)
            if m:
                out.extend(" " * (m.end() - i)); i = m.end()
                continue
        out.append(c); i += 1
    return "".join(out)


def classify_admit_lines(text: str) -> dict:
    """Walk `text` line by line, returning the 1-indexed line numbers
    of `admit()` calls partitioned by whether they live inside a
    `proof fn axiom_*` body.

    Returns: {"non_axiom_lines": list[int], "axiom_lines": list[int]}.
    """
    masked = strip_comments_strings(text)
    axiom_spans: list[tuple[int, int]] = []
    for match in _AXIOM_FN_NAME_RE.finditer(masked):
        body_brace = find_proof_fn_body_brace(masked, match.start())
        if body_brace is None:
            continue
        closing = find_matching_brace(masked, body_brace)
        if closing is None:
            continue
        axiom_spans.append((body_brace, closing))

    non_axiom: list[int] = []
    axiom: list[int] = []
    offset = 0
    for idx, line in enumerate(masked.splitlines(keepends=True), start=1):
        col = line.find("admit()")
        if col >= 0:
            pos = offset + col
            if any(start <= pos <= end for start, end in axiom_spans):
                axiom.append(idx)
            else:
                non_axiom.append(idx)
        offset += len(line)
    return {"non_axiom_lines": non_axiom, "axiom_lines": axiom}


def axiom_fn_names(text: str) -> set[str]:
    """Names of `proof fn axiom_*` declarations in `text`.

    The COMPLETE gate excludes `admit()` inside `proof fn axiom_*` bodies
    (axioms-by-convention). That exclusion is a fake-green vector: an agent
    could route a proof through a NEW `proof fn axiom_cheat() { admit() }`
    and the counter would silently ignore it. The harness diffs this set
    against a pre-run baseline so any agent-introduced axiom is caught.
    """
    masked = strip_comments_strings(text)
    return {m.group(1) for m in _AXIOM_FN_NAME_RE.finditer(masked)}


# Proof-bypass constructs that are prompt-forbidden (see prompt.md). Each
# discharges a proof obligation WITHOUT an SMT proof and WITHOUT leaving an
# `admit()` or a new `axiom_*` for the COMPLETE gate's counters to catch:
#   assume(e)                  — adds `e` as a free hypothesis; `assume(false)`
#                                closes any goal.
#   #[verifier::external_body] — skips the body, trusting the contract verbatim.
# `\bassume\b` does NOT match `assume_specification(` (no word boundary before
# the `_`), so the assume-expression form is matched on its own.
_ASSUME_RE = re.compile(r"\bassume\s*\(")
_EXTERNAL_BODY_RE = re.compile(r"\bexternal_body\b")


def count_forbidden_constructs(text: str) -> dict[str, int]:
    """Count proof-bypass constructs in `text`, ignoring comments and string
    literals (so a mention in prose / a URL never trips the count).

    Returns ``{"assume": int, "external_body": int}``. Used by the harness's
    forbidden-construct integrity gate (run.py), which diffs these counts
    against a pre-run baseline — only AGENT-introduced constructs fail a round,
    so a pre-existing `external_body` in seeded dalek source is tolerated.
    """
    code = strip_comments_strings(text)
    return {
        "assume": len(_ASSUME_RE.findall(code)),
        "external_body": len(_EXTERNAL_BODY_RE.findall(code)),
    }


def count_non_axiom(text: str) -> int:
    """Count `admit()` lines outside `proof fn axiom_*` bodies.

    Drop-in replacement for run.py's former local
    `_count_llm_target_admits` — same algorithm, same answers.
    """
    return len(classify_admit_lines(text)["non_axiom_lines"])


def inventory_file(path: Path) -> dict:
    """Return the JSON shape used by skills/admit_inventory.py for a
    single file. Per-admit entries carry `{file, line}` only — the
    scanner does not track function names (a previous attempt
    using a heuristic Rust parser was dropped — see commit history)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    cls = classify_admit_lines(text)
    file_str = str(path)
    return {
        "file": file_str,
        "non_axiom_admits": [{"file": file_str, "line": ln}
                             for ln in cls["non_axiom_lines"]],
        "axiom_admits": [{"file": file_str, "line": ln}
                         for ln in cls["axiom_lines"]],
        "non_axiom_count": len(cls["non_axiom_lines"]),
        "axiom_count": len(cls["axiom_lines"]),
    }


def inventory_files(paths: Iterable[Path]) -> dict:
    """Aggregate `inventory_file` across multiple paths."""
    files: list[dict] = []
    non_axiom: list[dict] = []
    axiom: list[dict] = []
    for path in paths:
        inv = inventory_file(Path(path))
        files.append(inv)
        non_axiom.extend(inv["non_axiom_admits"])
        axiom.extend(inv["axiom_admits"])
    return {
        "okay_for_complete": len(non_axiom) == 0,
        "non_axiom_count": len(non_axiom),
        "axiom_count": len(axiom),
        "non_axiom_admits": non_axiom,
        "axiom_admits": axiom,
        "files": files,
    }


# ---------- admit-skeleton creation (mode-aware) ------------------------
# The inverse of counting: turn proven source into an `admit()` skeleton,
# preserving signatures + requires/ensures/decreases. Ported from
# inference-dalek `eval/starting_state.py` (the core of
# `construct_admitted_state`). Mode-aware on purpose: only `proof fn`
# bodies and inline `proof { ... }` blocks are admitted; `axiom_*` fns are
# skipped (trusted), and `spec fn` definitions plus all exec code are left
# intact. Contrast `code_utils.strip_fn_body_to_admit`, which is mode-blind
# and would replace an exec fn's executable body with `admit()`.

# Matches `proof fn name`, `pub proof fn name`, `pub(crate) proof fn name`.
_PROOF_FN_RE = re.compile(r"(?:pub(?:\s*\([^)]*\))?\s+)?proof\s+fn\s+\w+")

# Matches `proof {` opening an inline proof block inside an exec function.
_PROOF_BLOCK_RE = re.compile(r"\bproof\s*\{")


def find_matching_brace(code: str, brace_pos: int) -> int | None:
    """Find the matching closing brace for an opening brace at *brace_pos*."""
    depth = 0
    for i in range(brace_pos, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def find_proof_fn_body_brace(code: str, fn_start: int) -> int | None:
    """Find the opening brace of a proof fn body in Verus source.

    Unlike a naive "first `{` at paren depth 0" scan, this handles Verus
    `requires`/`ensures` clauses whose expressions contain braces at paren
    depth 0 — e.g. `if (cond) { expr } else { expr }`,
    `forall|k| ... ==> { expr }`, or `(expr) by { ... }`.

    Heuristic: the body `{` appears on a line where the text before it is
    whitespace, part of the `fn` signature line (simple one-liner), a
    multiline-argument close `)`, or a final contract clause ending in `,`.
    Clause-internal braces are preceded by keywords or operators on the
    same line and are skipped.
    """
    paren_depth = 0
    for i in range(fn_start, len(code)):
        ch = code[i]
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        elif ch == "{" and paren_depth == 0:
            line_start = code.rfind("\n", 0, i) + 1
            prefix = code[line_start:i].strip()
            # Body brace: empty line after clauses, simple fn line, multiline
            # argument close, or final contract clause (`ensures e, {`).
            if (prefix == "" or re.search(r"\bfn\b", prefix)
                    or prefix == ")" or prefix.endswith(",")):
                return i
    return None


_INT_RETURN_TYPES = ("int", "nat", "u64", "u32", "u16", "u8",
                     "i64", "i32", "usize")


def _admit_body_for_return(sig: str) -> str:
    """A type-correct `admit()` body for a proof fn with signature `sig`.

    Verus proof fns return `()` by default (→ bare `admit()`); a few
    return `(name: Type)`, which needs a trailing value so the body still
    type-checks (`bool` → `true`, an integer type → `0`).

    Boundary (documented & kept): only the *named* return form
    `-> (name: Type)` is recognised — the Verus convention for proof fns
    that return a value. An *unnamed* `-> bool` falls through to a bare
    `admit()` with no trailing value. This is intentional: `admit()`
    assumes `false`, so SMT accepts the body regardless of the return
    type, and real Verus proof fns use the named form. Pinned by
    `tests/test_admits.py::AdmitProofFnBodies.test_unnamed_return_falls_through`.
    """
    m = re.search(r"->\s*\(\s*\w+\s*:\s*(\w+)", sig)
    ret = m.group(1) if m else None
    if ret == "bool":
        return "{\n    admit();\n    true\n}"
    if ret in _INT_RETURN_TYPES:
        return "{\n    admit();\n    0\n}"
    return "{\n    admit()\n}"


def _splice_edits(code: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply `(start, end, replacement)` edits to `code` (`end` exclusive).

    Edits are collected against the *original* `code` and must be ascending
    and non-overlapping — which holds here because regex matches are walked
    left-to-right and each edit spans a single fn body / proof block.
    Collecting first and splicing once avoids per-edit offset bookkeeping.
    """
    out: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        out.append(code[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(code[cursor:])
    return "".join(out)


def admit_proof_fn_bodies(code: str) -> str:
    """Replace every `proof fn` body with an `admit()` skeleton.

    Preserves signatures + requires/ensures/decreases; only the body (the
    outermost brace pair at paren depth 0) is replaced. `axiom_*` fns keep
    their bodies (trusted axioms); `spec fn` / `exec fn` bodies are not
    matched at all. The admit body is made return-type-correct (see
    `_admit_body_for_return`) so the result still type-checks.
    """
    edits: list[tuple[int, int, str]] = []
    for match in _PROOF_FN_RE.finditer(code):
        name = re.search(r"\bfn\s+(\w+)", match.group(0))
        if name and name.group(1).startswith("axiom_"):
            continue  # trusted axioms keep their bodies
        body_brace = find_proof_fn_body_brace(code, match.start())
        if body_brace is None:
            continue
        closing = find_matching_brace(code, body_brace)
        if closing is None:
            continue
        new_body = _admit_body_for_return(code[match.start():body_brace])
        edits.append((body_brace, closing + 1, new_body))
    return _splice_edits(code, edits)


def admit_proof_blocks(code: str) -> str:
    """Replace the contents of inline `proof { ... }` blocks with admit().

    These appear inside exec functions between statements. Only the proof
    scaffolding is hollowed out (`{ admit(); }`); the surrounding exec code
    is preserved.

    No `axiom_*` skip is needed here (unlike `admit_proof_fn_bodies` and
    `strip_proof_from_fns`): this only matches inline `proof { … }` blocks,
    which live in exec-fn bodies. An `axiom_*` is a `proof fn` whose body is
    `{ admit() }` — it has no inline `proof {}` block to match, so no axiom
    body is reachable here.
    """
    edits: list[tuple[int, int, str]] = []
    for match in _PROOF_BLOCK_RE.finditer(code):
        brace = code.index("{", match.start())
        closing = find_matching_brace(code, brace)
        if closing is None:
            continue
        edits.append((brace, closing + 1, "{ admit(); }"))
    return _splice_edits(code, edits)
