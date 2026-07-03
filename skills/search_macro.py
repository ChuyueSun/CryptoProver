#!/usr/bin/env python3
"""Static expansion of `lemma_*!(NAME, TYPE)` macro invocations.

dalek-lite's common_lemmas/ generates families of proof lemmas via
macros — e.g. `lemma_pow2_mul_div_mod_small_mul_uN!(u8, 8);` expands at
compile time to `lemma_u8_pow2_mul_div_mod_small_mul`. The MVP catalog
already captures these; this skill exposes them to the LLM via a
targeted query shape: "given this macro / this file, list every lemma
it generates."

Usage:
    python skills/search_macro.py --file src/common_lemmas/pow2.rs \\
        --project <root>

    python skills/search_macro.py --name-prefix lemma_u8_pow2 \\
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
logger = logging.getLogger("search_macro")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=None,
                    help="List macro-expanded entries in this file")
    ap.add_argument("--name-prefix", default=None,
                    help="Filter expanded names by prefix")
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--catalog-cache", type=Path, default=None)
    ap.add_argument("--vstd-root", type=Path, default=None, help="Path to vstd source — when set, vstd entries are added to the catalog and become queryable like project entries (no grep needed)")
    args = ap.parse_args()

    entries = catalog.build(args.project, args.catalog_cache, args.vstd_root)
    expanded = [e for e in entries if e.source == "macro_expansion"]

    if args.file:
        rel = str(args.file.resolve().relative_to(args.project.resolve()))
        expanded = [e for e in expanded if e.file == rel]
    if args.name_prefix:
        expanded = [e for e in expanded if e.name.startswith(args.name_prefix)]

    logger.info("search_macro: file=%s prefix=%s matches=%d",
                args.file, args.name_prefix, len(expanded))

    print(json.dumps({
        "total_expansions_in_catalog": sum(1 for e in entries if e.source == "macro_expansion"),
        "matches": len(expanded),
        "entries": [
            {"name": e.name, "signature": e.signature,
             "file": e.file, "line": e.line, "module": e.module_path}
            for e in expanded
        ],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
