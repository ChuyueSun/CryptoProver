"""Content-addressed source and verifier receipts.

The harness has to distinguish an identical candidate from a fresh source
state.  File names and result-directory timing are not enough: a later seed or
reset must be able to prove that the package gate it cites ran against the same
workspace bytes.  This module is deliberately stdlib-only so both ``run.py``
and the host-side Docker launcher can use one canonical algorithm.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


_IGNORED_DIRS = frozenset({
    ".git", "target", "results", "claude_raw", "claude_memory",
    "__pycache__", ".mypy_cache", ".pytest_cache",
})
_IGNORED_FILES = frozenset({".DS_Store"})


def workspace_root(project: Path) -> Path:
    """Return the outermost contiguous Cargo workspace containing ``project``."""
    project = project.resolve()
    found: Path | None = None
    current = project
    while True:
        if (current / "Cargo.toml").is_file():
            found = current
        parent = current.parent
        if parent == current or not (parent / "Cargo.toml").is_file():
            break
        current = parent
    return found or project


def _iter_relevant_files(root: Path) -> Iterable[Path]:
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED_DIRS)
        base = Path(current)
        for name in sorted(names):
            if name in _IGNORED_FILES:
                continue
            path = base / name
            if path.is_file() or path.is_symlink():
                yield path


def _file_entry(path: Path, root: Path) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    if path.is_symlink():
        target = os.readlink(path)
        data = target.encode("utf-8", "surrogateescape")
        return {
            "path": rel,
            "kind": "symlink",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return {"path": rel, "kind": "file", "sha256": digest.hexdigest(), "size": size}


def source_tree_receipt(project: Path) -> dict[str, Any]:
    """Return a deterministic receipt for all relevant workspace source bytes.

    Generated build/runtime directories are intentionally excluded; Cargo
    manifests, lockfiles, source, and local build configuration remain covered.
    The manifest lets a reviewer inspect a mismatch without relying on a bare
    opaque hash.
    """
    root = workspace_root(project)
    files = [_file_entry(path, root) for path in _iter_relevant_files(root)]
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 1,
        "workspace_root": str(root),
        "file_count": len(files),
        "tree_hash": hashlib.sha256(canonical).hexdigest(),
        "files": files,
    }


def gate_signature(command: Iterable[str], *, tool_paths: Iterable[Path] = ()) -> dict[str, Any]:
    """Bind a verifier receipt to exact argv plus the checked tool bytes."""
    tools: list[dict[str, str]] = []
    for path in sorted((Path(p) for p in tool_paths), key=lambda p: str(p)):
        if path.is_file():
            tools.append({"path": str(path.resolve()), "sha256": _file_entry(path, path.parent)["sha256"]})
        else:
            tools.append({"path": str(path), "sha256": "missing"})
    payload = {"argv": [str(part) for part in command], "tools": tools}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "signature": hashlib.sha256(encoded).hexdigest()}


def receipt_key(tree_hash: str, gate_signature_value: str) -> str:
    return hashlib.sha256(f"{tree_hash}\0{gate_signature_value}".encode()).hexdigest()


def write_immutable_json(path: Path, value: Any) -> None:
    """Write a receipt once, or verify an existing receipt is byte-equivalent."""
    payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text() != payload:
            raise RuntimeError(f"immutable receipt collision: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def accepted_promotion_tree_hash(receipt_path: Path) -> str:
    """Return the only tree hash a seed receipt is allowed to promote.

    A rejected/partial candidate has no reusable authority, even when its
    source files happen to be present beside the receipt.  Keep this check in
    the shared primitive so host launchers cannot accidentally reimplement a
    looser JSON interpretation.
    """
    try:
        value = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid promotion receipt: {receipt_path}") from exc
    if value.get("decision") != "ACCEPTED":
        raise ValueError("promotion receipt is not ACCEPTED")
    disposition = value.get("terminal_disposition") or {}
    if disposition.get("state") != "ACCEPTED" or not disposition.get("reusable"):
        raise ValueError("promotion receipt lacks an accepted reusable disposition")
    tree = value.get("final_tree_receipt") or {}
    tree_hash = tree.get("tree_hash")
    if not isinstance(tree_hash, str) or not tree_hash:
        raise ValueError("promotion receipt lacks final_tree_receipt.tree_hash")
    return tree_hash


def _main() -> None:
    parser = argparse.ArgumentParser(description="Compute a canonical workspace source receipt")
    parser.add_argument("project", type=Path, nargs="?")
    parser.add_argument("--tree-hash", action="store_true", help="print only the tree hash")
    parser.add_argument("--accepted-tree-hash", type=Path,
                        help="validate an ACCEPTED promotion receipt and print its tree hash")
    args = parser.parse_args()
    if args.accepted_tree_hash is not None:
        if args.project is not None or args.tree_hash:
            parser.error("--accepted-tree-hash cannot be combined with a project or --tree-hash")
        try:
            print(accepted_promotion_tree_hash(args.accepted_tree_hash))
        except ValueError as exc:
            parser.error(str(exc))
        return
    if args.project is None:
        parser.error("project is required unless --accepted-tree-hash is used")
    receipt = source_tree_receipt(args.project)
    if args.tree_hash:
        print(receipt["tree_hash"])
    else:
        print(json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    _main()
