"""Feature 3 — cross-round discovery brief.

A single run of a hard target burns most of its budget on *rediscovery*:
re-reading the same source files and re-issuing the same catalog searches
that a prior attempt already ran. This module mines a finished task's trace
(`claude_raw/round_*.jsonl`) for the files the agent explored/edited and the
searches it issued, and persists a compact per-target brief. The next attempt
on the same target gets that brief injected so it starts from the prior map
instead of re-walking the tree.

Signal-vs-noise is the whole game: a brief that lists *everything* is as
useless as no brief. So the miner separates high-signal from low:
  - **edits** (files actually touched) rank above reads, and an edited file
    is dropped from the reads list so it is never double-listed;
  - **reads** are weighted by revisit count — files the agent kept returning
    to (read 2+×) are likely load-bearing dependencies and are surfaced with
    their counts, while files opened exactly once are compressed onto a single
    "skimmed once" line rather than given a bullet each;
  - **searches** are stripped down to `skill "query"` — the `--project` /
    `--catalog-cache` tails are machine-specific and redundant (the prompt
    already hands the agent the cache path), so they are noise here.

Deterministic, stdlib-only. Keyed by target_id (`target_path.stem`, the same
granularity as failure_memory). One markdown file per target under
`<results_root>/discovery_briefs/<target_id>.md`.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional

# Matches a search-skill invocation inside a Bash command, e.g.
#   python skills/search_module.py "vstd::arithmetic::mul" --project ...
_SEARCH_RE = re.compile(r"search_\w+\.py[^\n|&;><]*")
# The skill name at the head of such a fragment.
_SKILL_RE = re.compile(r"search_\w+\.py")
# First double-quoted argument (the semantic query / module path).
_QUOTED_RE = re.compile(r'"([^"]*)"')
# Named lookups that aren't quoted (search_macro / search_proven).
_NAMED_RE = re.compile(r"--(name-prefix|name)\s+(\S+)")


def _briefs_dir(results_root: Path) -> Path:
    return results_root / "discovery_briefs"


def path_for(results_root: Path, target_id: str) -> Path:
    return _briefs_dir(results_root) / f"{target_id}.md"


def _rel(fp: str, project: Path) -> str:
    """Relativize an absolute Read/Edit path to the project root.

    Falls back to the original (absolute) path on mismatch — e.g. vstd files,
    which legitimately live outside the project tree. Both sides are resolved
    so a symlinked project root (common with /tmp on macOS, or worktrees)
    still relativizes cleanly.
    """
    try:
        return str(Path(fp).resolve().relative_to(project.resolve()))
    except (ValueError, OSError):
        return fp


def _clean_search(frag: str) -> str:
    """Reduce a captured search invocation to its high-signal core.

    `search_module.py "crate::lemmas::pow_lemmas" --project /x --catalog-cache /y`
    becomes `search_module.py "crate::lemmas::pow_lemmas"`. The flags are
    machine-specific and identical across every search in a run, so they are
    pure noise in the brief.
    """
    skill_m = _SKILL_RE.search(frag)
    if not skill_m:
        return frag.strip()[:120]
    skill = skill_m.group(0)
    q = _QUOTED_RE.search(frag)
    if q:
        return f'{skill} "{q.group(1)}"'
    named = _NAMED_RE.search(frag)
    if named:
        return f"{skill} --{named.group(1)} {named.group(2)}"
    return skill


def mine(tdir: Path, project: Path,
         editable: Optional[set[str]] = None) -> dict:
    """Walk every round_*.jsonl in tdir and tally what the agent explored.

    `editable`, when given, is the set of project-relative paths the agent is
    *allowed* to edit (the experiment editable set). Edits to any other file
    are split out of the "start here" list and surfaced separately as a
    do-not-touch warning — a prior attempt's illegal edit to a frozen file must
    never be re-recommended as work to continue (that is a self-reinforcing
    `FROZEN_EDIT` loop). When `editable` is None the partition is skipped and
    behaviour is unchanged.
    """
    reads: Counter = Counter()
    edits: Counter = Counter()
    searches: list[str] = []
    raw_dir = tdir / "claude_raw"
    if not raw_dir.exists():
        return {"reads": [], "edits": [], "searches": [], "frozen_edits": []}
    for jf in sorted(raw_dir.glob("round_*.jsonl")):
        try:
            text = jf.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "assistant":
                continue
            for b in e.get("message", {}).get("content", []):
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = b.get("name")
                inp = b.get("input", {}) or {}
                if name == "Read":
                    fp = inp.get("file_path", "")
                    if fp.endswith(".rs"):
                        reads[_rel(fp, project)] += 1
                elif name in ("Edit", "Write"):
                    fp = inp.get("file_path", "")
                    if fp.endswith(".rs"):
                        edits[_rel(fp, project)] += 1
                elif name == "Bash":
                    for m in _SEARCH_RE.finditer(inp.get("command", "") or ""):
                        searches.append(_clean_search(m.group(0)))
    # A file that was edited is already the highest-signal entry — don't also
    # list it (always heavily) under reads. The dedup uses *all* edited paths
    # (editable or not) so a frozen file the agent illegally touched is not
    # resurfaced under reads either.
    edited_paths = set(edits)
    read_items = [(f, c) for f, c in reads.most_common(20)
                  if f not in edited_paths]
    # Partition edits against the editable set: legitimate work ("start here")
    # vs. edits to frozen/non-editable files (a do-not-touch warning). Order is
    # preserved by edit frequency via most_common below.
    frozen_edits: list[str] = []
    if editable is not None:
        kept = Counter()
        for f, c in edits.items():
            if f in editable:
                kept[f] = c
            else:
                frozen_edits.append(f)
        edits = kept
    # Dedupe cleaned searches preserving first-seen order.
    seen: set[str] = set()
    uniq_searches = []
    for s in searches:
        if s and s not in seen:
            seen.add(s)
            uniq_searches.append(s)
    return {
        "reads": read_items,                       # [(path, count), ...]
        "edits": [f for f, _ in edits.most_common(10)],
        "frozen_edits": frozen_edits[:10],
        "searches": uniq_searches[:15],
    }


def render(brief: dict) -> str:
    reads = brief.get("reads") or []
    edits = brief.get("edits") or []
    frozen_edits = brief.get("frozen_edits") or []
    searches = brief.get("searches") or []
    if not (reads or edits or frozen_edits or searches):
        return ""
    lines = [
        "A prior attempt on this target already explored the codebase. Use "
        "this map as your starting point — re-read these files and reuse these "
        "searches rather than re-discovering them from scratch.",
        "",
    ]
    if edits:
        lines.append("**Files the prior attempt edited** (its actual work — start here):")
        lines += [f"- `{f}`" for f in edits]
        lines.append("")
    if frozen_edits:
        lines.append(
            "**The prior attempt also edited these NON-editable (frozen) files "
            "— do NOT touch them.** Their changes do not count and fail the "
            "round as `FROZEN_EDIT`. If a proof needs a helper that lived here, "
            "relocate it into an editable file (e.g. a new top-level "
            "`proof fn lemma_*` in the editable target/API file itself):")
        lines += [f"- `{f}`" for f in frozen_edits]
        lines.append("")
    # Split reads by revisit count: files the agent kept returning to are the
    # load-bearing dependencies; files opened exactly once are likely skims and
    # get compressed onto a single line so they don't drown the signal.
    revisited = [(f, c) for f, c in reads if c >= 2]
    once = [f for f, c in reads if c < 2]
    if revisited:
        lines.append("**Dependencies it kept returning to** (re-read — likely load-bearing):")
        lines += [f"- `{f}` (read {c}×)" for f, c in revisited]
        lines.append("")
    if once:
        lines.append("Also opened once (lower signal): "
                     + ", ".join(f"`{f}`" for f in once))
        lines.append("")
    if searches:
        lines.append("**Catalog searches the prior attempt issued** (don't repeat these):")
        lines += [f"- `{s}`" for s in searches]
        lines.append("")
    return "\n".join(lines).strip()


def update(results_root: Path, target_id: str, tdir: Path, project: Path,
           editable: Optional[set[str]] = None) -> None:
    """Mine the just-finished task and persist its rendered brief.

    `editable` (project-relative paths the agent may edit) is forwarded to
    `mine` so edits to frozen files are surfaced as a do-not-touch warning
    rather than re-recommended as "start here".
    """
    brief = mine(tdir, project, editable=editable)
    rendered = render(brief)
    if not rendered:
        return
    out = path_for(results_root, target_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered)


def load_block(results_root: Path, target_id: str) -> str:
    """Return the prior brief for this target, or "" if none exists."""
    p = path_for(results_root, target_id)
    if not p.exists():
        return ""
    try:
        return p.read_text().strip()
    except OSError:
        return ""
