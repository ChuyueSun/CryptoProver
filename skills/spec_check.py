#!/usr/bin/env python3
"""Specification integrity gate.

Snapshots function signatures + requires/ensures/decreases before the
agent runs; verifies the snapshot after. Failure = the agent weakened a
spec and must not be allowed to claim success.

Scope of "signature" for this check:
    - `fn` header (name, params, return type)
    - `requires` clauses (textual; normalized whitespace)
    - `ensures` clauses
    - `decreases` clause
    - `#[verifier::external_body]` attribute (forbidden to *add*)

What's allowed to change freely: the function body — EXCEPT, when
`verify --check-spec-defs` is passed, the body of a `spec fn`. A `spec fn`'s
body IS its definition (the meaning a frozen `requires`/`ensures` is written
in), so in a proof-reconstruction experiment where specs are frozen, changing
an existing spec definition can hollow out a frozen contract without touching
any clause text. `--check-spec-defs` snapshots every existing spec-fn body and
fails the round on any change (new spec fns are still allowed; deletion is
caught as `removed`). This freezes "everything reachable from the contract"
structurally, since the contract's meaning is exactly its spec closure.

Usage:
    python skills/spec_check.py snapshot <file.rs> --out <snapshot.json>
    python skills/spec_check.py verify   <file.rs> --against <snapshot.json>
    # In harness-launched rounds, verify may fall back to $SPEC_SNAPSHOT.

`verify` exits 0 if no blocking drift remains. The raw `drift` list is always
preserved for the harness; generated `lemma_*` contract changes under
`/lemmas/` are also reported as `allowed_generated_contract_drift` so proof
agents do not mistake legal reconstructed-lemma repairs for frozen-spec drift.
"""
import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.admits import strip_comments_strings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(os.environ.get(
        "CLI_LOG_PATH", Path(__file__).parents[1] / "cli.log")))],
)
logger = logging.getLogger("spec_check")


_GENERATED_CONTRACT_FIELDS = {"requires", "ensures", "decreases"}


# Find each `pub? (broadcast)? (open|closed)? (proof|spec|exec)? fn NAME` and
# capture through the end of any `requires`/`ensures`/`decreases` blocks,
# stopping at the first `{` that opens the body. Simple brace-matching; we
# accept a small risk of misparse on esoteric code and log when that happens.
_FN_START_RE = re.compile(
    r"(?P<attr>(?:#\[[^\]]+\]\s*)*)"
    r"(?P<vis>pub(?:\s*\([^)]+\))?\s+)?"
    r"(?P<broadcast>broadcast\s+)?"
    r"(?P<openness>(?:open|closed)\s+)?"
    r"(?P<mode>(?:proof|spec|exec)\s+)?"
    r"\bfn\s+(?P<name>\w+)",
    re.MULTILINE,
)


# Attributes that tune verification budget rather than the proof obligation.
# Adding/changing these is NOT a spec change — the function still has to
# meet the same requires/ensures. The agent must be able to bump them on
# hard files (the round-10 ristretto narrative documented this need).
_BUDGET_ATTR_RE = re.compile(
    r"#\[\s*verifier::(?:rlimit|spinoff_prover|integer_ring|nonlinear)\s*\([^)]*\)\s*\]"
)


def _strip_budget_attrs(s: str) -> str:
    """Remove budget-tuning attributes so they don't count as drift."""
    return _BUDGET_ATTR_RE.sub("", s)


_COSMETIC_ATTR_RE = re.compile(
    r"#\[\s*(?:inline(?:\s*\([^)]*\))?|cold)\s*\]"
)


def _strip_cosmetic_attrs(s: str) -> str:
    """Remove Rust codegen attributes that do not change Verus obligations."""
    return _COSMETIC_ATTR_RE.sub("", s)


def _frozen_header(header: str) -> str:
    """Normalize the contract-frozen function header for drift comparison."""
    return _normalize(_strip_cosmetic_attrs(_strip_budget_attrs(_header_only(header))))


def _extract_sigs(text: str) -> dict[str, dict]:
    """Return {key: {name, header, requires, ensures, decreases, mode,
                     external_body, spec_body, line}}.

    The KEY is the fn name, EXCEPT a name that appears more than once (trait
    impls for `T` and `&T`, etc.) is disambiguated by occurrence index:
    `name`, `name#1`, `name#2`, … — so a later same-named fn no longer
    overwrites an earlier one. This matters for the freeze gates: dalek has
    real duplicate `spec fn`s (e.g. edwards.rs `neg_spec`/`neg_req`), and
    keying by bare name would leave the first occurrence's body unmonitored.
    The real `name` is kept in the dict for drift reporting.

    Note: `header` is only the fn shape (attributes + name + params + return
    type), not the contract clauses. It excludes budget-tuning attributes
    (`#[verifier::rlimit(N)]`, etc.) so the agent can adjust them without
    tripping the drift gate. Other `#[verifier::*]` attrs remain captured.
    """
    sigs: dict[str, dict] = {}
    name_counts: dict[str, int] = {}
    # Match fn-starts over a comment/string-masked copy so prose like
    # `// a recursive defn equals a polynomial` can't register a phantom
    # `fn equals` (which then "drifts" the instant the agent edits that
    # comment — a false SPEC_DRIFT that killed peel_corefloor_005). The mask is
    # length-preserving, so offsets index back into the ORIGINAL `text` for the
    # real header/body below.
    masked = strip_comments_strings(text)
    for m in _FN_START_RE.finditer(masked):
        name = m.group("name")
        start = m.start()
        header_end = _find_header_end(text, m.end())
        if header_end is None:
            logger.warning("spec_check: could not find header end for %s", name)
            continue
        header = text[start:header_end]
        line = text.count("\n", 0, start) + 1
        attrs = m.group("attr") or ""
        mode = (m.group("mode") or "").strip()
        # For a `spec fn` with an in-language body (`{ … }`, not an
        # `external_body`/`uninterp` `;` declaration), capture the body — it is
        # the function's *definition*. `_find_header_end` returns the index OF
        # the opening `{` for a body, or the index AFTER the `;` for a decl, so
        # `text[header_end] == "{"` distinguishes the two.
        spec_body = ""
        if mode == "spec" and header_end < len(text) and text[header_end] == "{":
            body_end = _find_matching_brace(text, header_end)
            if body_end is not None:
                spec_body = _normalize(text[header_end + 1:body_end - 1])
            else:
                logger.warning("spec_check: could not find body end for spec fn %s", name)
        occ = name_counts.get(name, 0)
        name_counts[name] = occ + 1
        key = name if occ == 0 else f"{name}#{occ}"
        sigs[key] = {
            "name": name,
            "header": _frozen_header(header),
            "requires": _section(header, "requires"),
            "ensures": _section(header, "ensures"),
            "decreases": _section(header, "decreases"),
            "mode": mode,
            "external_body": "external_body" in attrs,
            "spec_body": spec_body,
            "line": line,
        }
    return sigs


def _extract_fn_spans(text: str) -> dict[str, dict]:
    """Return source spans for function declarations keyed like `_extract_sigs`.

    Spans are raw character offsets into `text`. `header_end` is the opening
    body `{` for ordinary definitions, or just after the declaration `;`.
    """
    spans: dict[str, dict] = {}
    name_counts: dict[str, int] = {}
    masked = strip_comments_strings(text)
    for m in _FN_START_RE.finditer(masked):
        name = m.group("name")
        start = m.start()
        header_end = _find_header_end(text, m.end())
        if header_end is None:
            continue
        occ = name_counts.get(name, 0)
        name_counts[name] = occ + 1
        key = name if occ == 0 else f"{name}#{occ}"
        full_end = header_end
        if header_end < len(text) and text[header_end] == "{":
            body_end = _find_matching_brace(text, header_end)
            if body_end is None:
                logger.warning("spec_check: could not find body end for %s", name)
                continue
            full_end = body_end
        spans[key] = {
            "name": name,
            "full_start": start,
            "header_end": header_end,
            "full_end": full_end,
        }
    return spans


def restore_frozen_spec_drift(
    baseline_text: str, current_text: str, drift: list[dict]
) -> tuple[str, list[str]]:
    """Restore drifted frozen function specs from `baseline_text`.

    Header/contract drift restores only the function header so proof-body edits
    survive. Spec-definition drift restores the whole spec function because the
    body is the definition. Returns `(new_text, unresolved_descriptions)`.
    """
    baseline_spans = _extract_fn_spans(baseline_text)
    current_spans = _extract_fn_spans(current_text)
    by_key: dict[str, list[dict]] = {}
    unresolved: list[str] = []
    for entry in drift:
        key = str(entry.get("key") or entry.get("function") or "")
        if not key:
            unresolved.append("<unknown>")
            continue
        by_key.setdefault(key, []).append(entry)

    patches: list[tuple[int, int, str]] = []
    for key, entries in by_key.items():
        base = baseline_spans.get(key)
        cur = current_spans.get(key)
        label = f"{entries[0].get('file', '')}::{entries[0].get('function', key)}"
        if base is None or cur is None:
            unresolved.append(label)
            continue
        replace_whole = any(e.get("field") == "spec_body" or
                            e.get("change") in {"removed", "file_missing"}
                            for e in entries)
        if replace_whole:
            patches.append((
                cur["full_start"],
                cur["full_end"],
                baseline_text[base["full_start"]:base["full_end"]],
            ))
        else:
            patches.append((
                cur["full_start"],
                cur["header_end"],
                baseline_text[base["full_start"]:base["header_end"]],
            ))

    if not patches:
        return current_text, unresolved

    new_text = current_text
    for start, end, replacement in sorted(patches, reverse=True):
        new_text = new_text[:start] + replacement + new_text[end:]
    return new_text, unresolved


def _iter_code(text: str, start: int = 0):
    """Yield `(i, char)` for each character of `text[start:]` that is real
    CODE — skipping `//` and `/* */` comments and `"…"` string literals (with
    `\\` escapes). Bare `'` is NOT treated as a quote: in Rust it is ambiguous
    with lifetimes, so a char-literal `'}'` is yielded as code (a deliberate,
    documented limitation shared by both scanners below). On an unterminated
    comment or string the generator stops early (yields nothing further), so a
    caller scanning for a delimiter returns its not-found value (`None`)."""
    i, n, in_str = start, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                return
            i = nl + 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            if close == -1:
                return
            i = close + 2
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        yield i, c
        i += 1


def _find_matching_brace(text: str, open_idx: int) -> int | None:
    """Given `open_idx` = the index of a `{`, return the index just PAST its
    matching `}` (comment/string-aware via `_iter_code`). None if unbalanced."""
    depth = 0
    for i, c in _iter_code(text, open_idx):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _find_header_end(text: str, start: int) -> int | None:
    """Walk forward from `start` past `requires`/`ensures`/`decreases` blocks
    (comment/string-aware via `_iter_code`) and stop at the `{` that opens the
    body or the `;` of an externless declaration. `()`/`[]` are depth-tracked
    so a `{`/`;` inside them is not mistaken for the body opener."""
    depth = 0
    skip_until = -1
    masked = strip_comments_strings(text)
    for i, c in _iter_code(text, start):
        if i < skip_until:
            continue
        if c == "(" or c == "[":
            depth += 1
        elif c == ")" or c == "]":
            depth -= 1
        elif c == "{" and depth == 0:
            if _word_before(masked, i) in {"requires", "ensures", "decreases"}:
                block_end = _find_matching_brace(text, i)
                if block_end is None:
                    return None
                skip_until = block_end
                continue
            return i
        elif c == ";" and depth == 0:
            return i + 1
    return None


def _word_before(masked: str, idx: int) -> str:
    """Return the identifier immediately before `idx` in a masked source string."""
    j = idx - 1
    while j >= 0 and masked[j].isspace():
        j -= 1
    end = j + 1
    while j >= 0 and (masked[j].isalnum() or masked[j] == "_"):
        j -= 1
    return masked[j + 1:end]


def _is_word_at(masked: str, idx: int, word: str) -> bool:
    if not masked.startswith(word, idx):
        return False
    before_ok = idx == 0 or not (masked[idx - 1].isalnum() or masked[idx - 1] == "_")
    end = idx + len(word)
    after_ok = end == len(masked) or not (masked[end].isalnum() or masked[end] == "_")
    return before_ok and after_ok


def _header_only(header: str) -> str:
    """Trim contract clauses from a full fn header, leaving name/params/return."""
    masked = strip_comments_strings(header)
    depth = 0
    for i, c in _iter_code(header):
        if c == "(" or c == "[":
            depth += 1
        elif c == ")" or c == "]":
            depth -= 1
        elif depth == 0:
            for kw in ("requires", "ensures", "decreases"):
                if _is_word_at(masked, i, kw):
                    return header[:i]
    return header


def _section(header: str, keyword: str) -> str:
    """Extract `requires { ... }` (or `ensures` / `decreases`) as normalized text."""
    # Verus accepts both `requires P` and `requires P1, P2` forms. We match
    # `keyword` followed by either a `{ ... }` block or a comma-separated
    # expression until the next clause keyword / `;`.
    masked = strip_comments_strings(header)
    m = re.search(rf"\b{keyword}\b", masked)
    if not m:
        return ""
    i = m.end()
    while i < len(header) and header[i].isspace():
        i += 1

    if i < len(header) and header[i] == "{":
        end = _find_matching_brace(header, i)
        return _normalize(header[i:end] if end is not None else header[i:])

    depth = 0
    end = len(header)
    for j, c in _iter_code(header, i):
        if c == "(" or c == "[":
            depth += 1
        elif c == ")" or c == "]":
            depth -= 1
        elif c == ";" and depth == 0:
            end = j
            break
        if depth == 0 and j > i:
            for kw in ("requires", "ensures", "decreases"):
                if _is_word_at(masked, j, kw):
                    end = j
                    return _normalize(header[i:end])
    return _normalize(header[i:end])


def _normalize(s: str) -> str:
    return " ".join(s.split())


# ---------------- Sibling helper discovery ----------------

def discover_sibling_helpers(project: Path, target: Path) -> list[Path]:
    """Return sibling helper files that the agent may append new lemmas to.

    Convention (dalek-lite): for a target like
        <project>/src/<area>.rs            (e.g. ristretto.rs)
    the sibling helpers live in EITHER
        <project>/src/lemmas/<area>_lemmas/*.rs   (directory layout — ristretto, edwards, field, window)
    OR
        <project>/src/lemmas/<area>_lemmas*.rs    (loose-file layout — scalar, montgomery)
    or any combination of the two.

    For a target already INSIDE `lemmas/<area>_lemmas/`, the siblings are
    the OTHER files in the same directory.

    The target itself is never in the returned list. `mod.rs` is excluded.
    """
    target = target.resolve()
    project = project.resolve()
    src = project / "src"
    if not src.exists():
        return []

    siblings_set: set[Path] = set()

    # Case 1: target is INSIDE a lemma helper dir — siblings are other files
    # in the same dir. Most helper dirs end in `_lemmas`; scalar uses the
    # historical `scalar_lemmas_` spelling.
    try:
        rel = target.relative_to(src)
    except ValueError:
        return []
    parts = rel.parts
    if (
        len(parts) >= 3
        and parts[0] == "lemmas"
        and (parts[1].endswith("_lemmas") or parts[1].endswith("_lemmas_"))
    ):
        helper_dir = src / "lemmas" / parts[1]
        for f in sorted(helper_dir.glob("*.rs")):
            if f.resolve() != target and f.name != "mod.rs":
                siblings_set.add(f.resolve())
        return sorted(siblings_set)

    # Case 2: target is <area>.rs at src/ top level — siblings are
    # lemmas/<area>_lemmas/*.rs (directory) AND lemmas/<area>_lemmas*.rs
    # (loose files at the top of lemmas/).
    if len(parts) == 1 and rel.suffix == ".rs":
        area = rel.stem
        lemmas_root = src / "lemmas"
        if not lemmas_root.is_dir():
            return []

        # Liberal prefix match: ANY file or directory under lemmas/ whose
        # name starts with "<area>_" (or is named exactly "<area>_lemmas")
        # is a sibling helper module. Covers all observed layouts:
        #   ristretto.rs   → lemmas/ristretto_lemmas/*.rs       (dir)
        #   edwards.rs     → lemmas/edwards_lemmas/*.rs         (dir)
        #   field.rs       → lemmas/field_lemmas/*.rs           (dir)
        #   window.rs      → (no lemma helpers; specs only)
        #   scalar.rs      → lemmas/scalar_lemmas.rs, scalar_lemmas_extra.rs,
        #                    scalar_batch_invert_lemmas.rs,
        #                    scalar_montgomery_lemmas.rs       (loose files)
        #                  + lemmas/scalar_lemmas_/*.rs,
        #                    lemmas/scalar_byte_lemmas/*.rs    (dirs)
        #   montgomery.rs  → lemmas/montgomery_lemmas.rs,
        #                    montgomery_curve_lemmas.rs,
        #                    montgomery_pow_chain_lemmas.rs    (loose files)
        prefix = f"{area}_"
        for entry in sorted(lemmas_root.iterdir()):
            if entry.name == "mod.rs":
                continue
            if not entry.name.startswith(prefix):
                continue
            if entry.is_file() and entry.suffix == ".rs":
                siblings_set.add(entry.resolve())
            elif entry.is_dir():
                for f in sorted(entry.glob("*.rs")):
                    if f.name != "mod.rs":
                        siblings_set.add(f.resolve())

    return sorted(siblings_set)


# ---------------- Commands ----------------

def cmd_snapshot(args) -> int:
    files = [args.target] + list(args.siblings or [])
    snapshot = {"files": {}}
    total_sigs = 0
    for f in files:
        text = f.read_text()
        sigs = _extract_sigs(text)
        snapshot["files"][str(f)] = {"sigs": sigs}
        total_sigs += len(sigs)
    args.out.write_text(json.dumps(snapshot, indent=2))
    logger.info("spec_check snapshot: %d files / %d sigs → %s",
                len(files), total_sigs, args.out)
    return 0


def _verify_one(file_path: str, original: dict, current: dict,
                check_spec_defs: bool = False) -> list[dict]:
    """Return drift entries for one file. Each entry carries a `file` key
    so callers can route by source file.

    `check_spec_defs`: also treat a change to an existing `spec fn`'s BODY
    (its definition) as drift — used when specs are frozen so the agent
    cannot weaken a contract by redefining the vocabulary it's written in."""
    drift: list[dict] = []
    # `key` disambiguates duplicate fn names (name, name#1, …); `fn_name` is the
    # real source name reported in drift entries.
    for key, orig in original.items():
        fn_name = orig.get("name", key)
        if key not in current:
            drift.append({"file": file_path, "function": fn_name,
                          "key": key,
                          "change": "removed",
                          "original_line": orig.get("line")})
            continue
        cur = current[key]
        for field_ in ("header", "requires", "ensures", "decreases", "mode"):
            if orig[field_] != cur[field_]:
                drift.append({
                    "file": file_path,
                    "function": fn_name,
                    "key": key,
                    "change": "modified",
                    "field": field_,
                    "original": orig[field_][:400],
                    "current": cur[field_][:400],
                    "line": cur.get("line"),
                })
        # Spec-definition freeze: an existing `spec fn`'s body is its meaning.
        # Guarded on `"spec_body" in orig` for back-compat with pre-field
        # snapshots (won't false-positive on an older baseline).
        if (check_spec_defs and orig.get("mode") == "spec"
                and "spec_body" in orig
                and orig.get("spec_body", "") != cur.get("spec_body", "")):
            drift.append({
                "file": file_path,
                "function": fn_name,
                "key": key,
                "change": "spec_def_modified",
                "field": "spec_body",
                "original": (orig.get("spec_body") or "")[:400],
                "current": (cur.get("spec_body") or "")[:400],
                "line": cur.get("line"),
            })
        if cur["external_body"] and not orig["external_body"]:
            drift.append({
                "file": file_path, "function": fn_name,
                "key": key,
                "change": "external_body_added",
                "line": cur.get("line"),
            })
    return drift


def _is_generated_lemma_contract_drift(drift: dict) -> bool:
    """Return True for generated-lemma contract drift allowed in experiments.

    Keep this predicate aligned with `run._partition_generated_contract_drift`.
    `cmd_verify` still returns these entries in raw `drift`; the filtered view
    only makes the agent-facing `okay` signal match the experiment rules.
    """
    parts = Path(str(drift.get("file", ""))).parts
    return (
        "lemmas" in parts
        and str(drift.get("function", "")).startswith("lemma_")
        and drift.get("field") in _GENERATED_CONTRACT_FIELDS
    )


def _partition_generated_contract_drift(
    drift: list[dict],
) -> tuple[list[dict], list[dict]]:
    allowed, blocked = [], []
    for entry in drift:
        if _is_generated_lemma_contract_drift(entry):
            allowed.append(entry)
        else:
            blocked.append(entry)
    return allowed, blocked


def resolve_against_snapshot(against: Path | None) -> Path:
    """Resolve the baseline snapshot path for `verify`.

    `--against` is the canonical, explicit form. The harness also sets
    SPEC_SNAPSHOT per task so agent-invoked compatibility calls that omit the
    flag still verify against the same authoritative baseline.
    """
    if against is not None:
        return against
    env_snapshot = os.environ.get("SPEC_SNAPSHOT")
    if env_snapshot:
        return Path(env_snapshot)
    raise ValueError("missing --against and SPEC_SNAPSHOT is not set")


def cmd_verify(args) -> int:
    try:
        against = resolve_against_snapshot(args.against)
    except ValueError as e:
        print(json.dumps({"okay": False, "error": str(e)}))
        return 2
    try:
        snapshot = json.loads(against.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"okay": False, "error": f"bad snapshot: {e}"}))
        return 1

    # Back-compat: old snapshots have top-level "sigs", new ones have "files".
    if "sigs" in snapshot and "files" not in snapshot:
        files_map = {snapshot.get("file", str(args.target)):
                     {"sigs": snapshot["sigs"]}}
    else:
        files_map = snapshot.get("files", {})

    drift: list[dict] = []
    new_fns: dict[str, list[str]] = {}
    for file_path, entry in files_map.items():
        p = Path(file_path)
        if not p.exists():
            drift.append({"file": file_path, "change": "file_missing"})
            continue
        current = _extract_sigs(p.read_text())
        original = entry["sigs"]
        drift.extend(_verify_one(file_path, original, current,
                                 check_spec_defs=args.check_spec_defs))
        added = sorted(set(current) - set(original))
        if added:
            new_fns[file_path] = added
            # New helper lemmas are allowed (prompt rule 4), but a NEW function
            # carrying `#[verifier::external_body]` is not: external_body skips
            # the body entirely (the contract is trusted, never SMT-checked), so
            # a fresh `external_body proof fn lemma_cheat(...) ensures GOAL {}`
            # discharges an obligation with zero proof — a fake-green vector in
            # the same class the `external_body_added` check guards for EXISTING
            # fns. Fold into drift so the round fails.
            for key in added:
                if current[key].get("external_body"):
                    drift.append({
                        "file": file_path,
                        "function": current[key].get("name", key),
                        "key": key,
                        "change": "external_body_new_fn",
                        "line": current[key].get("line"),
                    })

    allowed_generated_drift, blocking_drift = (
        _partition_generated_contract_drift(drift)
    )
    result = {
        "okay": len(blocking_drift) == 0,
        "drift": drift,
        "blocking_drift": blocking_drift,
        "allowed_generated_contract_drift": allowed_generated_drift,
        "raw_drift_count": len(drift),
        "blocking_drift_count": len(blocking_drift),
        "allowed_generated_contract_drift_count": len(allowed_generated_drift),
        "new_functions": new_fns,
    }
    print(json.dumps(result, indent=2))
    logger.info("spec_check verify: okay=%s raw_drift=%d blocking_drift=%d "
                "allowed_generated_contract_drift=%d files=%d",
                result["okay"], len(drift), len(blocking_drift),
                len(allowed_generated_drift), len(files_map))
    return 0 if result["okay"] else 1


def cmd_restore(args) -> int:
    """Restore drifted frozen specs in-place from a baseline file."""
    try:
        drift_obj = json.loads(args.drift_file.read_text())
        drift = drift_obj.get("drift", drift_obj) if isinstance(drift_obj, dict) else drift_obj
        if not isinstance(drift, list):
            raise ValueError("drift file must contain a JSON list or {'drift': list}")
        baseline_text = args.baseline.read_text()
        current_text = args.target.read_text()
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(json.dumps({"okay": False, "error": str(e)}))
        return 1

    restored_text, unresolved = restore_frozen_spec_drift(
        baseline_text, current_text, drift)
    changed = restored_text != current_text
    if unresolved:
        print(json.dumps({
            "okay": False,
            "unresolved": unresolved,
            "changed": False,
        }, indent=2))
        return 1
    if changed:
        args.target.write_text(restored_text)
    print(json.dumps({
        "okay": True,
        "unresolved": [],
        "changed": changed,
    }, indent=2))
    return 0


def cmd_list_siblings(args) -> int:
    siblings = discover_sibling_helpers(args.project, args.target)
    print(json.dumps({
        "target": str(args.target.resolve()),
        "siblings": [str(s) for s in siblings],
    }, indent=2))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snapshot")
    snap.add_argument("target", type=Path)
    snap.add_argument("--out", type=Path, required=True)
    snap.add_argument("--siblings", type=Path, nargs="*", default=[],
                      help="Additional files to snapshot alongside the target")
    snap.set_defaults(func=cmd_snapshot)

    ver = sub.add_parser("verify")
    ver.add_argument("target", type=Path)
    ver.add_argument("--against", type=Path, default=None)
    ver.add_argument("--project", type=Path, default=None,
                     help=argparse.SUPPRESS)
    ver.add_argument("--check-spec-defs", action="store_true",
                     help="Also fail on any change to an existing spec fn's "
                          "BODY (its definition). Use when specs are frozen so "
                          "a contract can't be weakened by redefining its "
                          "vocabulary. New spec fns remain allowed.")
    ver.set_defaults(func=cmd_verify)

    restore = sub.add_parser("restore")
    restore.add_argument("target", type=Path)
    restore.add_argument("--baseline", type=Path, required=True)
    restore.add_argument("--drift-file", type=Path, required=True)
    restore.set_defaults(func=cmd_restore)

    lst = sub.add_parser("list-siblings")
    lst.add_argument("target", type=Path)
    lst.add_argument("--project", type=Path, required=True)
    lst.set_defaults(func=cmd_list_siblings)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
