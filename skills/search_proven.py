#!/usr/bin/env python3
"""Query the ProvenRegistry — which lemmas have already been verified
in the current campaign, and where?

Format: a simple JSON file `<results_root>/proven_registry.json` with
shape {"proven": [{"name": str, "module": str, "file": str, "run_id": str,
"timestamp": str}, ...]}.

Read-only in the MVP. run.py writes to it on each successful round.

Usage:
    python skills/search_proven.py --results <results_root> [--name lemma_foo]
                                    [--module crate::field::specs]
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(os.environ.get(
        "CLI_LOG_PATH", Path(__file__).parents[1] / "cli.log")))],
)
logger = logging.getLogger("search_proven")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True,
                    help="Results root (e.g. ./results/)")
    ap.add_argument("--name", default=None)
    ap.add_argument("--module", default=None)
    args = ap.parse_args()

    reg_path = args.results / "proven_registry.json"
    if not reg_path.exists():
        print(json.dumps({"proven": [], "note": "registry is empty or missing"}))
        return

    data = json.loads(reg_path.read_text())
    proven = data.get("proven", [])

    if args.name:
        proven = [p for p in proven if p["name"] == args.name]
    if args.module:
        needle = args.module.removeprefix("crate::")
        proven = [p for p in proven if p["module"].endswith(needle)]

    logger.info("search_proven: name=%s module=%s matches=%d",
                args.name, args.module, len(proven))
    print(json.dumps({"count": len(proven), "proven": proven},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
