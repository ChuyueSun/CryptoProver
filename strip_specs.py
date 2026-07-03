#!/usr/bin/env python3
"""Strip `requires`/`ensures`/`decreases` clauses from fn headers, and/or
delete whole fn declarations.

For the spec-reconstruction experiment:
  - `--strip-fn NAME` strips header clauses from a named fn (signature
    + body kept). Repeatable. Omit to strip all fns.
  - `--delete-fn NAME` deletes the entire fn (leading doc comments +
    signature + body). Repeatable. Used to remove proof-only artifacts
    (lemmas, spec fns) so the agent has to invent them from callsites.
  - Default (no `--strip-fn`/`--delete-fn`): strip ALL fns in the file.

Loop clauses and assert-by clauses live inside fn bodies and are never
touched.

This is a top-level init/harness tool — a sibling of `run.py` / `admit.py`, NOT
an agent skill. It builds the experiment's starting state before a run; the
proof agent never invokes it during a round.

Usage:
    python strip_specs.py <file.rs> --in-place
    python strip_specs.py <file.rs> --out <out.rs> --strip-fn foo
    python strip_specs.py <file.rs> --in-place \\
        --strip-fn dispatcher --delete-fn lemma_a --delete-fn lemma_b

Output (stdout): JSON summary
    {"file": ..., "stripped": [...], "deleted": [...], "bytes_before": N, "bytes_after": M}
"""
import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(os.environ.get(
        "CLI_LOG_PATH", Path(__file__).parent / "cli.log")))],
)
logger = logging.getLogger("strip_specs")


# Same fn-header regex as spec_check.py — kept in sync deliberately so the
# two skills agree on which fns count.
_FN_START_RE = re.compile(
    r"(?P<attr>(?:#\[[^\]]+\]\s*)*)"
    r"(?P<vis>pub(?:\s*\([^)]+\))?\s+)?"
    r"(?P<broadcast>broadcast\s+)?"
    r"(?P<openness>(?:open|closed)\s+)?"
    r"(?P<mode>(?:proof|spec|exec)\s+)?"
    r"fn\s+(?P<name>\w+)",
    re.MULTILINE,
)

_CLAUSE_KEYWORDS = ("requires", "ensures", "decreases")
_HEADER_TERMINATORS_RE = re.compile(r"\b(requires|ensures|decreases|where)\b")


def _scan_header(text: str, start: int):
    """Walk forward from `start` past the fn header. Yield (kind, kw_start, kw_end)
    for each top-level `requires`/`ensures`/`decreases`/`where` keyword,
    then finally yield ("END", header_end, header_end) where header_end is
    the index of the body `{` or the `;` of an externless declaration.

    State tracking mirrors spec_check._find_header_end: handles `//` and
    `/* */` comments and `"..."` strings; skips inside `()` and `[]`. We
    only emit keywords found at `depth == 0` and outside strings/comments.

    Special case: once a clause keyword has been seen, any `{...}` blocks
    encountered at depth 0 (e.g. `ensures expr == { let x = ...; ... }`)
    are part of the clause body — we brace-match past them rather than
    treating them as the body opener.
    """
    i = start
    depth = 0
    in_str = False
    in_clause = False  # True after we've seen requires/ensures/decreases/where
    # True if the most recent non-whitespace/non-comment token at depth 0
    # was a `,`. Used to distinguish the body `{` (preceded by trailing
    # comma in Verus convention) from an inline block expression like
    # `ensures expr == { let x = ...; ... }` (preceded by an operator).
    saw_top_comma = False
    n = len(text)
    while i < n:
        c = text[i]
        # Skip line comments
        if not in_str and c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                return
            i = nl + 1
            continue
        # Skip block comments
        if not in_str and c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            if close == -1:
                return
            i = close + 2
            continue
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c in "([":
            depth += 1
            i += 1
            saw_top_comma = False
            continue
        if c in ")]":
            depth -= 1
            i += 1
            saw_top_comma = False
            continue
        if depth == 0:
            if c == "{":
                if in_clause and not saw_top_comma:
                    # Inline block expression inside a clause: e.g.
                    # `ensures expr == { let aA = ...; ... }`. Brace-match
                    # past it and keep scanning.
                    i = _find_body_end(text, i)
                    saw_top_comma = False
                    continue
                yield ("END", i, i)
                return
            if c == ";":
                yield ("END", i + 1, i + 1)
                return
            if c == ",":
                saw_top_comma = True
                i += 1
                continue
            if c.isspace():
                i += 1
                continue
            # Keyword? Only at depth 0, only at a word boundary
            if c.isalpha() and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
                m = _HEADER_TERMINATORS_RE.match(text, i)
                if m:
                    kw = m.group(1)
                    yield (kw, m.start(), m.end())
                    in_clause = True
                    saw_top_comma = False
                    i = m.end()
                    continue
            # Any other non-whitespace, non-comma char (an operator or
            # identifier) means we're inside a clause expression — the
            # next `{` would be an inline block, not the body opener.
            saw_top_comma = False
        i += 1


def _skip_ws_and_comments(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else n
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = close + 2 if close != -1 else n
            continue
        break
    return i


def _clause_body_end(text: str, kw_end: int, next_marker: int) -> int:
    """Given a keyword that ends at `kw_end` and the next header marker at
    `next_marker`, find the end of this clause's body.

    Two forms:
      - `requires { ... }` / `ensures { ... }` — brace block. End = matching `}`.
      - `requires expr, expr` — comma-separated expressions, terminated by
        the next keyword (already located as `next_marker`).
    """
    j = _skip_ws_and_comments(text, kw_end)
    if j < len(text) and text[j] == "{":
        # Brace-delimited body — find matching `}` (track nested braces,
        # strings, comments).
        depth = 0
        in_str = False
        k = j
        n = len(text)
        while k < n:
            c = text[k]
            if not in_str and c == "/" and k + 1 < n and text[k + 1] == "/":
                nl = text.find("\n", k)
                k = nl + 1 if nl != -1 else n
                continue
            if not in_str and c == "/" and k + 1 < n and text[k + 1] == "*":
                close = text.find("*/", k + 2)
                k = close + 2 if close != -1 else n
                continue
            if in_str:
                if c == "\\" and k + 1 < n:
                    k += 2
                    continue
                if c == '"':
                    in_str = False
                k += 1
                continue
            if c == '"':
                in_str = True
                k += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return k + 1
            k += 1
        return next_marker  # defensive fallback
    # Comma-list form: clause runs up to (but not including) next marker.
    return next_marker


def strip_text(text: str, only: Optional[set[str]] = None) -> tuple[str, list[str]]:
    """Return (stripped_text, list_of_fn_names_touched).

    If `only` is given, strip only fns whose name is in the set. Otherwise
    strip every fn in the file.
    """
    out: list[str] = []
    cursor = 0
    touched: list[str] = []

    for m in _FN_START_RE.finditer(text):
        fn_name = m.group("name")
        if only is not None and fn_name not in only:
            continue
        # Markers in order: clause keywords interleaved with the final END
        markers = list(_scan_header(text, m.end()))
        if not markers:
            continue
        # Find header_end (last marker should be END)
        header_end = markers[-1][2]

        # Collect clause ranges (kw_start, body_end) for requires/ensures/decreases
        # Each clause runs from its kw_start up to the next marker's start.
        clause_ranges: list[tuple[int, int]] = []
        positions = [(k, s) for (k, s, _) in markers]
        for idx, (kind, kw_start, kw_end) in enumerate(markers):
            if kind not in _CLAUSE_KEYWORDS:
                continue
            next_marker_start = positions[idx + 1][1] if idx + 1 < len(positions) else header_end
            body_end = _clause_body_end(text, kw_end, next_marker_start)
            # If the comma-list form leaks a trailing comma into the clause,
            # we keep it inside the deletion — cleaner output. The clause
            # spans [kw_start, body_end).
            clause_ranges.append((kw_start, body_end))

        if not clause_ranges:
            continue

        # Splice: emit text up to first clause start, skip the clause,
        # emit between clauses, skip next clause, etc.
        clause_ranges.sort()
        # Emit unchanged text up to first clause
        out.append(text[cursor:clause_ranges[0][0]])
        for i, (s, e) in enumerate(clause_ranges):
            # Skip [s, e) — and also any trailing comma + whitespace so we
            # don't leave dangling `,\n    {` in the header.
            j = e
            n = len(text)
            while j < n and text[j] in " \t":
                j += 1
            if j < n and text[j] == ",":
                j += 1
            # Pull in trailing newline if the line is now blank — keeps the
            # source tidy without aggressively reformatting.
            line_start = text.rfind("\n", 0, s) + 1
            prefix = text[line_start:s]
            if prefix.strip() == "" and j < n and text[j] == "\n":
                j += 1
                # Also drop the leading whitespace we already added
                if out and out[-1].endswith(prefix):
                    out[-1] = out[-1][:-len(prefix)] if prefix else out[-1]
            # Emit text between this clause and the next (or up to header_end)
            next_start = clause_ranges[i + 1][0] if i + 1 < len(clause_ranges) else header_end
            out.append(text[j:next_start])
        # Emit text from header_end onward (body + rest of file up to next fn)
        cursor = header_end
        touched.append(fn_name)

    out.append(text[cursor:])
    return "".join(out), touched


def _find_body_end(text: str, body_open: int) -> int:
    """Walk from `body_open` (index of `{`) to the matching `}`. Returns
    the index right after the closing `}`. Handles strings and comments.
    """
    depth = 0
    in_str = False
    i = body_open
    n = len(text)
    while i < n:
        c = text[i]
        if not in_str and c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else n
            continue
        if not in_str and c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = close + 2 if close != -1 else n
            continue
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n  # defensive


def _match_delim(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Walk from `open_idx` (index of `open_ch`) to the matching `close_ch`,
    returning the index right after it. Token-aware: skips `//`/`/* */`
    comments and `"..."` strings. Used to bracket `(...)` in assert headers
    where `_find_body_end` (braces only) doesn't apply."""
    depth = 0
    in_str = False
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if not in_str and c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else n
            continue
        if not in_str and c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = close + 2 if close != -1 else n
            continue
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n  # defensive


def _assert_stmt_end(text: str, paren_open: int, limit: int) -> int:
    """Given the `(` opening an `assert(...)` proof statement, return the
    index just past the end of the whole statement. Handles the three Verus
    forms:
      - `assert(EXPR);`
      - `assert(EXPR) by (lemma(...));`
      - `assert(EXPR) by { ... }`   (block form — no trailing `;`)
    """
    i = _match_delim(text, paren_open, "(", ")")          # past the `(...)`
    # skip whitespace / comments
    while i < limit:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < limit and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else limit
            continue
        if c == "/" and i + 1 < limit and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = close + 2 if close != -1 else limit
            continue
        break
    # optional `by (...)` / `by { ... }`
    if text[i:i + 2] == "by" and (i + 2 >= limit or not (text[i + 2].isalnum()
                                                         or text[i + 2] == "_")):
        i += 2
        while i < limit and text[i] in " \t\r\n":
            i += 1
        if i < limit and text[i] == "{":
            i = _find_body_end(text, i)
            while i < limit and text[i] in " \t\r\n":
                i += 1
            if i < limit and text[i] == ";":
                i += 1
            return i
        if i < limit and text[i] == "(":
            i = _match_delim(text, i, "(", ")")
    while i < limit and text[i] in " \t\r\n":
        i += 1
    if (text.startswith("requires", i)
            and (i + len("requires") >= limit
                 or not (text[i + len("requires")].isalnum()
                         or text[i + len("requires")] == "_"))):
        i = _stmt_semicolon_end(text, i, limit)
    if i < limit and text[i] == ";":
        i += 1
    return i


def _stmt_semicolon_end(text: str, start: int, limit: int) -> int:
    """Return the index just past the next top-level `;` outside comments,
    strings, and nested delimiters. Used for Verus proof precondition tails
    like `assert(x) by (bit_vector) requires ... ;`."""
    i = start
    depth = 0
    in_str = False
    while i < limit:
        c = text[i]
        if not in_str and c == "/" and i + 1 < limit and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else limit
            continue
        if not in_str and c == "/" and i + 1 < limit and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = close + 2 if close != -1 else limit
            continue
        if in_str:
            if c == "\\" and i + 1 < limit:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth == 0:
            return i + 1
        i += 1
    return i


def _proof_spans_in_body(text: str, body_open: int, body_end: int
                         ) -> list[tuple[int, int]]:
    """Return [(start, end), ...] spans of proof-only constructs inside the
    fn body `(body_open, body_end)`: `proof { ... }` blocks (at any nesting
    depth) and standalone `assert(...)` proof statements. Token-aware so a
    `proof`/`assert` inside a string or comment is ignored, and `assert!`
    (the exec macro) / `debug_assert` are NOT matched."""
    spans: list[tuple[int, int]] = []
    i = body_open + 1
    n = body_end
    in_str = False
    while i < n:
        c = text[i]
        if not in_str and c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = nl + 1 if nl != -1 else n
            continue
        if not in_str and c == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            i = close + 2 if close != -1 else n
            continue
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        # keyword at a word boundary?
        if (c in "pa") and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            kw = "proof" if text.startswith("proof", i) else (
                 "assert" if text.startswith("assert", i) else "")
            if kw:
                j = i + len(kw)
                # must be a real token end (not `proofy` / `asserts` / `assert!`)
                if j < n and (text[j].isalnum() or text[j] == "_" or text[j] == "!"):
                    i += 1
                    continue
                k = j
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if kw == "proof" and k < n and text[k] == "{":
                    end = _find_body_end(text, k)
                    spans.append((i, end))
                    i = end
                    continue
                if kw == "assert" and k < n and text[k] == "(":
                    end = _assert_stmt_end(text, k, n)
                    spans.append((i, end))
                    i = end
                    continue
        i += 1
    return spans


def strip_proof_from_fns(text: str, names: set[str]) -> tuple[str, list[str]]:
    """Remove proof-only content from the BODY of each named fn while keeping
    its signature, `requires`/`ensures`/`decreases` contract, and executable
    statements. Removes `proof { ... }` blocks and standalone `assert(...)`
    proof statements (which are spec-mode in Verus).

    Used by the `--no-anchor-proof` rung: it strips the anchor
    `decompress`'s own orchestration proof so the agent must reconstruct it,
    leaving the frozen contract (the only thing the soundness gate protects)
    untouched. Returns (new_text, list_of_fn_names_touched)."""
    # Collect removal spans across all targeted fns, then splice once.
    all_spans: list[tuple[int, int]] = []
    touched: list[str] = []
    for m in _FN_START_RE.finditer(text):
        name = m.group("name")
        if name not in names:
            continue
        # Floor guard: never strip proof content out of a trusted axiom, even
        # if named (e.g. by `strip-all`, which auto-names every fn). A normal
        # `axiom_*` body is `{ admit() }` with nothing to strip, so this is a
        # no-op on real axioms — but it makes floor-safety STRUCTURAL rather
        # than dependent on axiom bodies happening to contain no proof{}/assert.
        if name.startswith("axiom_"):
            continue
        markers = list(_scan_header(text, m.end()))
        if not markers:
            continue
        body_open = markers[-1][2]
        if body_open >= len(text) or text[body_open] != "{":
            continue  # forward decl (`;`) — no body
        body_end = _find_body_end(text, body_open)
        spans = _proof_spans_in_body(text, body_open, body_end)
        if spans:
            all_spans.extend(spans)
            touched.append(m.group("name"))

    if not all_spans:
        return text, []

    all_spans.sort()
    merged_spans: list[tuple[int, int]] = []
    for s, e in all_spans:
        if merged_spans and s <= merged_spans[-1][1]:
            prev_s, prev_e = merged_spans[-1]
            merged_spans[-1] = (prev_s, max(prev_e, e))
        else:
            merged_spans.append((s, e))
    all_spans = merged_spans

    out: list[str] = []
    cursor = 0
    for s, e in all_spans:
        # Pull in the leading indentation on the span's line and the trailing
        # newline so removing a block doesn't leave a blank, half-indented line.
        line_start = text.rfind("\n", 0, s) + 1
        if text[line_start:s].strip() == "":
            s = line_start
        j = e
        while j < len(text) and text[j] in " \t":
            j += 1
        if j < len(text) and text[j] == "\n":
            j += 1
        out.append(text[cursor:s])
        cursor = j
    out.append(text[cursor:])
    return "".join(out), touched


def _walk_back_doc_comments(text: str, start: int) -> int:
    """Walk backward from `start` over consecutive `///` doc-comment lines.
    Stops at the first non-doc-comment line (blank lines included — those
    separate fns)."""
    line_start = text.rfind("\n", 0, start) + 1
    new_start = line_start
    while new_start > 0:
        prev_line_end = new_start - 1
        prev_line_start = text.rfind("\n", 0, prev_line_end) + 1
        line = text[prev_line_start:prev_line_end]
        if line.lstrip().startswith("///"):
            new_start = prev_line_start
        else:
            break
    return new_start


def _walk_forward_trailing(text: str, end: int) -> int:
    """Consume the newline after a `}` and one immediately-following blank
    line so deletion doesn't leave a double blank gap."""
    if end < len(text) and text[end] == "\n":
        end += 1
    if end < len(text) and text[end] == "\n":
        end += 1
    return end


def _drop_orphan_outer_doc_comments(text: str) -> str:
    """Remove `///` lines that no longer document an item.

    A deleted fn can leave a doc comment separated from that fn by plain section
    comments. Rust still treats the `///` as an outer doc attribute, so if only
    comments/whitespace and a closing brace remain after it, the peeled file is
    syntactically invalid. Keep docs whenever the next non-trivia token is an
    item such as `pub fn`.
    """
    spans: list[tuple[int, int]] = []
    for m in _DOC_LINE_RE.finditer(text):
        j = _skip_ws_and_comments(text, m.end())
        if j >= len(text) or text[j] == "}":
            spans.append(m.span())
    if not spans:
        return text

    out: list[str] = []
    cursor = 0
    for s, e in spans:
        out.append(text[cursor:s])
        cursor = e
    out.append(text[cursor:])
    return "".join(out)


def delete_text(text: str, names: set[str]) -> tuple[str, list[str]]:
    """Delete every fn whose name is in `names`. Returns (new_text, deleted_names).

    Removes leading `///` doc comments and trailing blank line so the
    result stays tidy.
    """
    out_parts: list[str] = []
    cursor = 0
    deleted: list[str] = []

    for m in _FN_START_RE.finditer(text):
        fn_name = m.group("name")
        if fn_name not in names:
            continue
        markers = list(_scan_header(text, m.end()))
        if not markers:
            continue
        # The terminal marker is the body `{` or `;`. _scan_header yields
        # ("END", idx, idx).
        end_kind, end_idx, _ = markers[-1]
        if end_idx < len(text) and text[end_idx] == "{":
            body_end = _find_body_end(text, end_idx)
        else:
            # `;` form (e.g. external trait fn). body_end is past the `;`.
            body_end = end_idx
        del_start = _walk_back_doc_comments(text, m.start())
        del_end = _walk_forward_trailing(text, body_end)
        out_parts.append(text[cursor:del_start])
        cursor = del_end
        deleted.append(fn_name)

    out_parts.append(text[cursor:])
    out = "".join(out_parts)
    if deleted:
        out = _drop_orphan_outer_doc_comments(out)
    return out, deleted


_DOC_LINE_RE = re.compile(r"(?m)^[ \t]*///.*\n?")


def strip_doc_comments(text: str) -> tuple[str, int]:
    """Remove `///` outer doc-comment lines and report how many. These carry
    the informal spec / proof sketch in natural language; the formal-clause
    strip leaves them, so a genuinely "no spec" surface needs this too. Inner
    `//!` module docs and plain `//` code comments are left alone."""
    n = len(_DOC_LINE_RE.findall(text))
    return _DOC_LINE_RE.sub("", text), n


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Strip requires/ensures/decreases from fn headers; "
                    "optionally delete whole fns and/or /// doc comments.")
    ap.add_argument("target", type=Path)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--out", type=Path, help="Write output here")
    grp.add_argument("--in-place", action="store_true",
                     help="Overwrite the target file in place")
    ap.add_argument("--strip-fn", action="append", default=None,
                    help="Strip header clauses from this fn (repeatable). "
                         "Omit both --strip-fn and --delete-fn to strip "
                         "every fn in the file (default).")
    ap.add_argument("--delete-fn", action="append", default=None,
                    help="Delete this fn entirely — sig + body + leading "
                         "/// doc comments (repeatable). Used for proof-only "
                         "artifacts (lemmas, spec fns).")
    ap.add_argument("--strip-proof-fn", action="append", default=None,
                    help="Remove proof-only content (`proof { ... }` blocks "
                         "and standalone `assert(...)` statements) from this "
                         "fn's BODY, keeping its signature, requires/ensures/"
                         "decreases contract, and executable statements "
                         "(repeatable). Used by the --no-anchor-proof rung to "
                         "strip the anchor's own orchestration proof.")
    ap.add_argument("--strip-docs", action="store_true",
                    help="Also remove all `///` doc-comment lines (the "
                         "informal spec / proof sketch). Combine with the "
                         "default clause-strip for a genuinely no-spec surface "
                         "— only the fn signature survives.")
    args = ap.parse_args()

    text = args.target.read_text()
    bytes_before = len(text)
    deleted: list[str] = []
    stripped: list[str] = []
    proof_stripped: list[str] = []
    docs_removed = 0

    # Delete first so the strip pass walks the post-deletion text.
    if args.delete_fn:
        text, deleted = delete_text(text, set(args.delete_fn))

    # Proof-body strip before clause-strip so spans are computed against the
    # contract-bearing header still in place.
    if args.strip_proof_fn:
        text, proof_stripped = strip_proof_from_fns(text, set(args.strip_proof_fn))

    if args.strip_fn:
        text, stripped = strip_text(text, only=set(args.strip_fn))
    elif not args.delete_fn and not args.strip_proof_fn:
        # Default: no per-fn targeting at all → strip every fn's clauses.
        # (A bare --delete-fn or --strip-proof-fn is targeted, so it must NOT
        # trigger the strip-all fallback.)
        text, stripped = strip_text(text, only=None)

    if args.strip_docs:
        text, docs_removed = strip_doc_comments(text)

    dest = args.target if args.in_place else args.out
    dest.write_text(text)

    result = {
        "file": str(args.target),
        "out": str(dest),
        "stripped": stripped,
        "deleted": deleted,
        "proof_stripped": proof_stripped,
        "docs_removed": docs_removed,
        "bytes_before": bytes_before,
        "bytes_after": len(text),
    }
    logger.info("strip_specs: file=%s stripped=%d deleted=%d proof_stripped=%d docs=%d",
                args.target, len(stripped), len(deleted),
                len(proof_stripped), docs_removed)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
