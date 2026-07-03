#!/usr/bin/env python3
"""Inventory actionable and axiom-by-convention admits.

Usage:
    python skills/admit_inventory.py <target.rs> [--siblings <a.rs> <b.rs>]
    python skills/admit_inventory.py <target.rs> --snapshot spec_snapshot.json \
        --changed-siblings-from snapshots/round_0
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.admits import inventory_files  # noqa: E402


def _files_from_snapshot(snapshot: Path) -> list[Path]:
    try:
        raw = json.loads(snapshot.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if "files" in raw:
        return [Path(p) for p in raw.get("files", {}).keys()]
    file_ = raw.get("file")
    return [Path(file_)] if file_ else []


def _changed_sibling_files(target: Path, files: list[Path], baseline_dir: Path) -> list[Path]:
    out: list[Path] = []
    target = target.resolve()
    for path in files:
        path = path.resolve()
        if path == target:
            continue
        if not path.exists():
            continue
        baseline = baseline_dir / path.name
        try:
            current = path.read_bytes()
            before = baseline.read_bytes()
        except OSError:
            out.append(path)
            continue
        if current != before:
            out.append(path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Inventory Verus admit() sites")
    ap.add_argument("target", type=Path)
    ap.add_argument("--siblings", type=Path, nargs="*", default=[])
    ap.add_argument("--snapshot", type=Path,
                    help="Use files from spec_snapshot.json (target + siblings)")
    ap.add_argument("--changed-siblings-from", type=Path,
                    help="With --snapshot, count target plus only siblings whose current contents differ from this baseline snapshot dir")
    args = ap.parse_args()

    if args.snapshot:
        snapshot_files = _files_from_snapshot(args.snapshot)
        if args.changed_siblings_from:
            files = [args.target] + _changed_sibling_files(
                args.target,
                snapshot_files,
                args.changed_siblings_from,
            )
        else:
            files = snapshot_files
        if not files:
            files = [args.target]
    else:
        files = [args.target] + list(args.siblings or [])
    files = [p.resolve() for p in files if p.exists()]
    result = inventory_files(files)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["okay_for_complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
