#!/usr/bin/env python3
"""List all public signatures from one module.

This is the `use crate::foo::bar::*` import-aware seeding primitive, but
on-demand: the LLM runs it when it wants every sig from a specific
module pulled into view.

Usage:
    python skills/search_module.py "curve25519_dalek::field::common_lemmas" \\
        --project <root>

    # Or by file path:
    python skills/search_module.py --file src/field/common_lemmas.rs \\
        --project <root>
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib import catalog  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(os.environ.get(
        "CLI_LOG_PATH", Path(__file__).parents[1] / "cli.log")))],
)
logger = logging.getLogger("search_module")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("module", nargs="?",
                    help="Module path like `crate::field::common_lemmas`")
    ap.add_argument("--file", type=Path, default=None,
                    help="Alternative: the .rs file path")
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--catalog-cache", type=Path, default=None)
    ap.add_argument("--vstd-root", type=Path, default=None, help="Path to vstd source — when set, vstd entries are added to the catalog and become queryable like project entries (no grep needed)")
    ap.add_argument("--include-private", action="store_true",
                    help="Include entries whose source path contains internals/")
    args = ap.parse_args()

    if not args.module and not args.file:
        ap.error("must supply either <module> or --file")

    entries = catalog.build(args.project, args.catalog_cache, args.vstd_root)

    if args.module:
        # Accept `crate::foo::bar` or just `foo::bar`.
        needle = args.module.removeprefix("crate::")
        matched = [e for e in entries if e.module_path.endswith(needle)]
    else:
        rel = str(args.file.resolve().relative_to(args.project.resolve()))
        matched = [e for e in entries if e.file == rel]

    if not args.include_private:
        matched = [e for e in matched if "internals" not in e.module_path]

    logger.info("search_module: module=%s file=%s matches=%d",
                args.module, args.file, len(matched))

    print(json.dumps({
        "module": args.module or str(args.file),
        "count": len(matched),
        "signatures": [
            {"name": e.name, "kind": e.kind, "signature": e.signature,
             "line": e.line, "source": e.source}
            for e in matched
        ],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
