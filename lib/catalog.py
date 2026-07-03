"""Canonical catalog of Verus symbols in a project.

Walks a Rust source tree, extracts function signatures (proof/spec/exec),
expands lemma_*! macros statically, and builds one in-memory index the
search skills share. Cached to disk as JSON so repeated skill invocations
don't re-parse the tree.

Keep this file small: all the heuristic filtering that bit inference-dalek
(commit d6601e3's 8-filter collapse) lives downstream in the search skills.
Catalog itself is just "parse and remember".
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional


# ---------------- Data model ----------------

@dataclass
class CatalogEntry:
    name: str              # `lemma_foo_bar`
    kind: str              # `proof` | `spec` | `open_spec` | `exec` | `axiom`
    signature: str         # full header (from `fn` through `{` or `;`)
    file: str              # repo-relative path
    line: int              # 1-indexed
    module_path: str       # e.g. `curve25519_dalek::field::common_lemmas`
    source: str            # `source` | `macro_expansion` | `vstd`


# ---------------- Regexes ----------------

# Capture both `pub proof fn`, `pub open spec fn`, etc. Handles optional
# broadcast / open / closed keywords.
_FN_RE = re.compile(
    r"""
    ^\s*
    (?:pub(?:\s*\([^)]+\))?\s+)?           # visibility (optional)
    (?:broadcast\s+)?                       # broadcast keyword (optional)
    (?:(open|closed)\s+)?                   # open/closed (optional)
    (proof|spec|exec)?\s*                   # mode
    fn\s+
    (?P<name>\w+)                           # function name
    (?P<rest>[^;{]*)                        # parameters + return + requires/ensures up to body
    (?P<end>[;{])                           # end: either `;` (extern) or `{` (body start)
    """,
    re.VERBOSE | re.MULTILINE,
)

# Matches `lemma_*!(NAME, TYPE)` invocations in common_lemmas/ files.
# dalek-lite uses this pattern to generate families of lemmas (e.g.
# `lemma_pow2_mul_div_mod_small_mul_uN!(u8, 8);`).
_MACRO_INVOCATION_RE = re.compile(
    r"(?P<macro>\w+)!\s*\(\s*(?P<args>[^)]+)\)\s*;",
    re.MULTILINE,
)

# Matches a `macro_rules! name { ... }` block; we only need `name`.
_MACRO_DEF_RE = re.compile(
    r"macro_rules!\s+(?P<name>\w+)\s*\{",
    re.MULTILINE,
)

# Matches `#[verifier::type_invariant]\n  (pub)? (closed|open)? spec fn NAME`
# When this attribute is present on a spec fn inside `impl <Type> { ... }`, the
# resulting predicate is automatically tied to <Type>: callers can invoke it via
# `use_type_invariant(p: <Type>)` in proof contexts. The catalog otherwise misses
# these because they're attribute-driven, not searchable as a `proof fn`.
_TYPE_INVARIANT_RE = re.compile(
    r"#\[\s*verifier\s*::\s*type_invariant\s*\][^\n]*\n\s*"
    r"(?:pub(?:\s*\([^)]+\))?\s+)?(?:closed\s+|open\s+)?spec\s+fn\s+(?P<predicate>\w+)",
    re.MULTILINE,
)

# Matches `impl[<...>] <Type> {` (used to find the impl block enclosing a
# type_invariant). Only top-level impls — we don't try to handle `impl X for Y`.
_IMPL_BLOCK_RE = re.compile(
    r"^\s*impl(?:\s*<[^>]+>)?\s+(?P<type>[A-Z][\w]*)\s*\{",
    re.MULTILINE,
)

# Matches `pub broadcast group NAME { member1, member2, ... }`. vstd uses these
# to bundle related broadcast lemmas (e.g. `group_mul_basics`).
_BROADCAST_GROUP_RE = re.compile(
    r"pub\s+broadcast\s+group\s+(?P<name>\w+)\s*\{(?P<body>[^}]*)\}",
    re.DOTALL,
)


# ---------------- Public API ----------------

def build(
    project_root: Path,
    cache_path: Optional[Path] = None,
    vstd_root: Optional[Path] = None,
) -> list[CatalogEntry]:
    """Build (or load from cache) the catalog for `project_root`.

    Cache invalidation: a hash of all .rs file paths + mtimes. When the
    tree changes, rebuild. vstd_root, if provided, contributes its own
    fingerprint so the cache is invalidated when vstd is updated.
    """
    project_root = project_root.resolve()
    vstd_root = vstd_root.resolve() if vstd_root else None
    entries: list[CatalogEntry] = []

    fingerprint = _tree_fingerprint(project_root)
    if vstd_root:
        fingerprint += ":" + _tree_fingerprint(vstd_root)
    if cache_path and cache_path.exists():
        cached = json.loads(cache_path.read_text())
        if cached.get("fingerprint") == fingerprint:
            return [CatalogEntry(**e) for e in cached["entries"]]

    for rs in sorted(project_root.rglob("*.rs")):
        if _should_skip(rs, project_root):
            continue
        entries.extend(_parse_file(rs, project_root))

    # vstd is the standard library verus code (exec/spec/proof fns the
    # agent will cite via `vstd::...::lemma_*`). Index it as a separate
    # root so entries get tagged `source="vstd"`.
    if vstd_root and vstd_root.exists():
        for rs in sorted(vstd_root.rglob("*.rs")):
            if _should_skip(rs, vstd_root):
                continue
            entries.extend(_parse_file(rs, vstd_root, source_tag="vstd",
                                       module_prefix="vstd"))

    # Expand macros in common_lemmas/* (dalek-lite convention).
    entries.extend(_expand_macro_lemmas(project_root, entries))

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "fingerprint": fingerprint,
            "entries": [asdict(e) for e in entries],
        }, indent=2))

    return entries


# ---------------- Internals ----------------

def _should_skip(rs: Path, root: Path) -> bool:
    rel = rs.relative_to(root)
    parts = rel.parts
    # Skip target/, build artifacts, tests (keep integration tests? for MVP no).
    skip_dirs = {"target", "build", ".cargo", "node_modules",
                 ".verus_intermediates", "outputs", "experiment_results"}
    return any(p in skip_dirs for p in parts)


def _parse_file(
    path: Path, root: Path,
    source_tag: str = "source",
    module_prefix: Optional[str] = None,
) -> Iterable[CatalogEntry]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    rel = str(path.relative_to(root))
    module_path = _module_path(rel)
    if module_prefix:
        module_path = f"{module_prefix}::{module_path}" if module_path else module_prefix
    out: list[CatalogEntry] = []

    for m in _FN_RE.finditer(text):
        mode = m.group(2) or "exec"  # default mode if not annotated
        name = m.group("name")
        rest = m.group("rest").strip()
        end = m.group("end")
        signature = f"fn {name}{rest}".strip()
        if mode in ("proof", "spec"):
            signature = f"{mode} {signature}"
        # Capture preceding `///` doc comment lines. These hold the
        # human-readable purpose of the lemma — without them the search
        # skills only return the type sig, forcing the agent to grep -B
        # the source for context.
        doc = _collect_doc_comment(text, m.start())
        if doc:
            signature = f"{doc}\n{signature}"
        # Approximate: if the body is an `assume(false)` or extern, mark axiom.
        kind = mode
        if end == ";":
            kind = "axiom" if mode in ("proof", "spec") else "exec"
        line = text.count("\n", 0, m.start()) + 1
        out.append(CatalogEntry(
            name=name, kind=kind, signature=signature,
            file=rel, line=line, module_path=module_path, source=source_tag,
        ))
    # Add synthetic entries for Verus attributes that aren't picked up by
    # the `_FN_RE` pass — type_invariants and broadcast groups (X2 in dev_log).
    out.extend(_collect_verus_attrs(text, rel, module_path, source_tag))
    return out


def _collect_verus_attrs(
    text: str, rel: str, module_path: str, source_tag: str,
) -> list[CatalogEntry]:
    """Emit synthetic CatalogEntries for `#[verifier::type_invariant]` and
    `pub broadcast group <NAME>` declarations. These aren't proof fns so
    the main `_FN_RE` pass misses them, but they're critical for proof
    discovery — the agent needs to know they exist."""
    out: list[CatalogEntry] = []

    # 1. Type invariants — find each, walk back to the enclosing `impl <Type>`
    for m in _TYPE_INVARIANT_RE.finditer(text):
        impl_matches = list(_IMPL_BLOCK_RE.finditer(text[:m.start()]))
        if not impl_matches:
            continue
        type_name = impl_matches[-1].group("type")
        line = text.count("\n", 0, m.start()) + 1
        sig = (
            f"// {type_name} has #[verifier::type_invariant] (predicate: "
            f"{m.group('predicate')}). The invariant holds for any value of "
            f"type {type_name} and can be invoked in proof contexts via "
            f"`use_type_invariant(p)` where `p: {type_name}`. This is the "
            f"standard pattern for `From<&{type_name}>` impls and similar "
            f"trait methods that lack a `requires` clause: invoke "
            f"`use_type_invariant(p)` at the top of the proof block to "
            f"materialize the invariant facts. To reason about CLOSED "
            f"coordinate/field accessors of an opaque type, the unlock is a "
            f"tiny SAME-MODULE exporter lemma (e.g. `lemma_unfold_edwards` in "
            f"`specs/edwards_specs.rs`, ensuring `edwards_x(p)==p.X`, …). If that "
            f"spec file is IN YOUR EDITABLE SCOPE and the lemma was stripped, "
            f"reconstruct it there with that equality `ensures` and an empty body "
            f"(legal only because it sits beside the closed defs in the same "
            f"module). If the spec file is FROZEN (not in your editable list), do "
            f"NOT edit it — CALL the existing bridge lemma, or repair an editable "
            f"caller/lemma instead. Either way, do NOT invent new `*_field_*` "
            f"accessors and do NOT raw-field-access an opaque type as a "
            f"substitute for the bridge."
        )
        out.append(CatalogEntry(
            name=f"type_invariant_for_{type_name}",
            kind="proof",
            signature=sig,
            file=rel, line=line,
            module_path=module_path,
            source="verus_attr",
        ))

    # 2. Broadcast groups — name + members. Importing one bundles N broadcast
    # lemmas at once; agents proving complex math often need these.
    for m in _BROADCAST_GROUP_RE.finditer(text):
        name = m.group("name")
        # Members: identifiers in the body (filter out `,` and whitespace)
        members = re.findall(r"\b(\w+)\b", m.group("body"))
        line = text.count("\n", 0, m.start()) + 1
        member_preview = ", ".join(members[:8])
        if len(members) > 8:
            member_preview += f", ... ({len(members)} total)"
        sig = (
            f"// pub broadcast group {name} {{ ... }}\n"
            f"// Use with `broadcast use {name};` inside a proof to enable "
            f"all {len(members)} member lemmas at once.\n"
            f"// Members: {member_preview}"
        )
        out.append(CatalogEntry(
            name=f"group_{name}",
            kind="proof",
            signature=sig,
            file=rel, line=line,
            module_path=module_path,
            source="verus_attr",
        ))

    return out


def _collect_doc_comment(text: str, fn_start: int) -> str:
    """Return contiguous `///` doc-comment lines immediately preceding `fn_start`.

    Skips intervening blank lines and `#[attr]` lines (those are part of the
    fn's own attribute set, not separator content).
    """
    lines: list[str] = []
    # Walk backward line-by-line from fn_start.
    cursor = fn_start
    while cursor > 0:
        # Find start of the current line.
        line_start = text.rfind("\n", 0, cursor)
        if line_start == -1:
            line_start = 0
        else:
            line_start += 1
        line = text[line_start:cursor].rstrip("\r\n")
        stripped = line.strip()
        if not stripped:
            # Blank line — keep walking but if we already saw any content,
            # stop (blank line breaks contiguity).
            if lines:
                break
        elif stripped.startswith("#["):
            # Attribute on the fn — skip
            pass
        elif stripped.startswith("///"):
            # Strip just the leading `///` and at most one space after.
            content = stripped[3:]
            if content.startswith(" "):
                content = content[1:]
            lines.append(content)
        else:
            # Anything else means the doc-comment block has ended.
            break
        cursor = line_start - 1
        if cursor < 0:
            break
    if not lines:
        return ""
    # Reverse and prefix with `///` so consumers can recognize it as a doc.
    lines.reverse()
    return "\n".join(f"/// {ln}" for ln in lines)


def _module_path(rel_path: str) -> str:
    """Convert `curve25519-dalek/src/field/common_lemmas.rs` →
       `curve25519_dalek::field::common_lemmas`."""
    p = Path(rel_path)
    # Strip the crate-name prefix up to `src`
    parts = list(p.parts)
    if "src" in parts:
        parts = parts[parts.index("src") + 1:]
    parts[-1] = parts[-1].removesuffix(".rs")
    if parts[-1] in ("mod", "lib"):
        parts = parts[:-1]
    # Rustify hyphens in crate names (only matters if we ever include them)
    return "::".join(p.replace("-", "_") for p in parts)


def _expand_macro_lemmas(root: Path, seed_entries: list[CatalogEntry]) -> list[CatalogEntry]:
    """Statically expand `lemma_*!(NAME, TYPE)` invocations in common_lemmas/.

    Conservative: we only synthesize a new entry when a macro of matching
    name was *defined* somewhere in the tree and the invocation parses as
    `<prefix>_<name_suffix>` — i.e., we can predict the expanded fn name.
    """
    defined: set[str] = set()
    for rs in root.rglob("*.rs"):
        if _should_skip(rs, root):
            continue
        try:
            text = rs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _MACRO_DEF_RE.finditer(text):
            defined.add(m.group("name"))

    out: list[CatalogEntry] = []
    for rs in root.rglob("common_lemmas/*.rs"):
        if _should_skip(rs, root):
            continue
        try:
            text = rs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(rs.relative_to(root))
        module_path = _module_path(rel)
        for m in _MACRO_INVOCATION_RE.finditer(text):
            macro = m.group("macro")
            if macro not in defined:
                continue
            args = [a.strip() for a in m.group("args").split(",")]
            if not args:
                continue
            # Convention in dalek-lite: first arg is the generated fn name
            # (e.g. `lemma_bitwise_or_zero_is_id!(lemma_u8_bitwise_or_zero_is_id, u8)`
            # → fn lemma_u8_bitwise_or_zero_is_id). Take it verbatim; a leading
            # `$` means the macro is parameterized differently and we skip.
            first_arg = args[0]
            if not re.match(r"^\w+$", first_arg):
                continue
            synthesized_name = first_arg
            line = text.count("\n", 0, m.start()) + 1
            out.append(CatalogEntry(
                name=synthesized_name,
                kind="proof",
                signature=f"proof fn {synthesized_name}(...)  /* expanded from {macro}!({', '.join(args)}) */",
                file=rel,
                line=line,
                module_path=module_path,
                source="macro_expansion",
            ))
    return out


def _tree_fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    for rs in sorted(root.rglob("*.rs")):
        if _should_skip(rs, root):
            continue
        try:
            st = rs.stat()
        except OSError:
            continue
        h.update(str(rs.relative_to(root)).encode())
        h.update(str(st.st_mtime_ns).encode())
        h.update(str(st.st_size).encode())
    return h.hexdigest()


# ---------------- CLI ----------------

def _main():
    import argparse
    ap = argparse.ArgumentParser(description="Build or inspect the catalog")
    ap.add_argument("project_root")
    ap.add_argument("--cache", help="JSON cache path", default=None)
    ap.add_argument("--vstd-root", help="Also index vstd source tree", default=None)
    ap.add_argument("--print", action="store_true", help="Print entries")
    args = ap.parse_args()

    entries = build(
        Path(args.project_root),
        Path(args.cache) if args.cache else None,
        Path(args.vstd_root) if args.vstd_root else None,
    )
    if args.print:
        for e in entries:
            print(f"{e.source:8} {e.kind:10} {e.name:50} {e.file}:{e.line}")
    by_source = {}
    for e in entries:
        by_source[e.source] = by_source.get(e.source, 0) + 1
    print(f"\n{len(entries)} entries  ({by_source})")


if __name__ == "__main__":
    _main()
