#!/usr/bin/env python3
"""Generate a diff.md showing admitted vs final vs ground-truth for one module.

Given:
- current target file (post-agent-edits)
- the admitted baseline (read from git at --admitted-ref)
- the ground-truth version (read from git at --truth-ref)

Emit a single markdown file containing:
1. Full diff: admitted → final (what the agent did)
2. Full diff: final → ground-truth (what the agent "missed" or did differently)
3. Side-by-side proof-body comparison for each function

Usage:
    python skills/diff_view.py <target.rs> \\
        --admitted-ref eval/admitted-layerA-debug \\
        --truth-ref main \\
        --out results/<run>/<task>/diff.md
"""
from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from pathlib import Path


_FN_BODY_RE = re.compile(
    r"^\s*(?:#\[[^\]]+\]\s*)*(?:pub(?:\s*\([^)]+\))?\s+)?"
    r"(?:broadcast\s+)?(?:(?:open|closed)\s+)?"
    r"(?P<mode>proof|spec|exec)\s+fn\s+(?P<name>\w+)",
    re.MULTILINE,
)


def git_show(repo: Path, ref: str, file_rel: str) -> str:
    """Return file contents at ref. Empty string if missing."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{ref}:{file_rel}"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        return proc.stdout
    except subprocess.CalledProcessError:
        return ""


def find_repo_root(path: Path) -> Path:
    """Walk up from `path` until a .git directory is found."""
    p = path.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return path.parent


def extract_fn_bodies(text: str) -> dict[str, str]:
    """Return {fn_name: body_text} for each proof/spec fn. Body is the
    substring from the opening `{` to its matched `}`."""
    out: dict[str, str] = {}
    for m in _FN_BODY_RE.finditer(text):
        name = m.group("name")
        # Walk forward to find the opening `{` that starts the body,
        # skipping over params/requires/ensures.
        i = m.end()
        depth = 0
        brace_start = None
        while i < len(text):
            c = text[i]
            if c == "(" or c == "[":
                depth += 1
            elif c == ")" or c == "]":
                depth -= 1
            elif c == "{" and depth == 0:
                brace_start = i
                break
            elif c == ";" and depth == 0:
                break
            i += 1
        if brace_start is None:
            continue
        # Match the body braces
        depth = 1
        i = brace_start + 1
        while i < len(text) and depth > 0:
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        if depth == 0:
            out[name] = text[brace_start:i]
    return out


def unified_diff(a: str, b: str, label_a: str, label_b: str, n_context: int = 3) -> str:
    lines = difflib.unified_diff(
        a.splitlines(keepends=True),
        b.splitlines(keepends=True),
        fromfile=label_a, tofile=label_b, n=n_context,
    )
    return "".join(lines)


def render_markdown(
    target_rel: str,
    admitted: str, final: str, truth: str,
    admitted_ref: str, truth_ref: str,
) -> str:
    parts: list[str] = []
    parts.append(f"# Diff — `{target_rel}`\n")
    parts.append(f"- **admitted baseline**: `{admitted_ref}`")
    parts.append(f"- **final (agent output)**: working tree")
    parts.append(f"- **ground-truth**: `{truth_ref}`\n")

    # Summary
    admitted_bodies = extract_fn_bodies(admitted)
    final_bodies = extract_fn_bodies(final)
    truth_bodies = extract_fn_bodies(truth)
    all_fns = sorted(set(admitted_bodies) | set(final_bodies) | set(truth_bodies))

    parts.append("## Per-function status")
    parts.append("")
    parts.append("| Function | Was admitted | Agent changed | Matches ground-truth |")
    parts.append("|---|---|---|---|")
    for fn in all_fns:
        a = admitted_bodies.get(fn, "")
        f = final_bodies.get(fn, "")
        t = truth_bodies.get(fn, "")
        was_admitted = "admit()" in a
        agent_changed = a.strip() != f.strip()
        matches_truth = f.strip() == t.strip() if t else "—"
        parts.append(
            f"| `{fn}` | "
            f"{'✅' if was_admitted else '—'} | "
            f"{'✅' if agent_changed else '—'} | "
            f"{'✅ exact' if matches_truth is True else '❌ differs' if matches_truth is False else '—'} |"
        )
    parts.append("")

    # Diff 1: admitted → final (what the agent did)
    parts.append("## Diff 1 — admitted → final (what the agent wrote)")
    parts.append("")
    diff = unified_diff(admitted, final, f"admitted ({admitted_ref})", "final (agent)")
    if diff.strip():
        parts.append("```diff")
        parts.append(diff)
        parts.append("```")
    else:
        parts.append("_(no changes)_")
    parts.append("")

    # Diff 2: final → ground-truth (what the agent missed or did differently)
    parts.append("## Diff 2 — final → ground-truth")
    parts.append("")
    if not truth.strip():
        parts.append(f"_(no ground-truth available at ref `{truth_ref}`)_")
    else:
        diff = unified_diff(final, truth, "final (agent)", f"ground-truth ({truth_ref})")
        if diff.strip():
            parts.append("```diff")
            parts.append(diff)
            parts.append("```")
        else:
            parts.append("_(agent matches ground-truth exactly)_")
    parts.append("")

    # Per-function bodies
    parts.append("## Per-function proof bodies")
    parts.append("")
    for fn in all_fns:
        parts.append(f"### `{fn}`")
        parts.append("")
        parts.append(f"**admitted** (`{admitted_ref}`):")
        parts.append("```rust")
        parts.append(admitted_bodies.get(fn, "(not present)"))
        parts.append("```")
        parts.append("")
        parts.append(f"**final** (agent output):")
        parts.append("```rust")
        parts.append(final_bodies.get(fn, "(not present)"))
        parts.append("```")
        parts.append("")
        parts.append(f"**ground-truth** (`{truth_ref}`):")
        parts.append("```rust")
        parts.append(truth_bodies.get(fn, "(not present)"))
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", type=Path,
                    help="Current (post-agent-edit) .rs file")
    ap.add_argument("--admitted-ref", required=True,
                    help="Git ref for the admitted baseline (e.g. eval/admitted-start)")
    ap.add_argument("--truth-ref", default="main",
                    help="Git ref for the ground-truth version (default: main)")
    ap.add_argument("--repo", type=Path, default=None,
                    help="Repo root (auto-detected from target if omitted)")
    ap.add_argument("--out", type=Path, required=True,
                    help="Output diff.md path")
    args = ap.parse_args()

    target = args.target.resolve()
    if not target.exists():
        print(f"[error] target not found: {target}", file=sys.stderr)
        return 1
    repo = (args.repo or find_repo_root(target)).resolve()
    rel = str(target.relative_to(repo))

    final = target.read_text()
    admitted = git_show(repo, args.admitted_ref, rel)
    truth = git_show(repo, args.truth_ref, rel)

    md = render_markdown(rel, admitted, final, truth,
                         args.admitted_ref, args.truth_ref)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md)
    print(f"Wrote {args.out} ({len(md)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
