#!/usr/bin/env python3
"""Keyword / substring search over the canonical catalog, with optional
LLM-driven query expansion to bridge the vocabulary gap between an
informal user query (e.g. "no zero divisors prime field") and the formal
lemma names actually in the catalog (e.g. `lemma_euclid_prime`).

Implementation:
- Ranking stays the cheap tokenized substring matcher
  (matches-in-name beat matches-in-signature). No embeddings.
- Before ranking, the query is fanned out into up to 3 alternate phrasings
  by a haiku-tier `claude -p` call (60s/attempt, retried once). The catalog
  is scored against every variant; each entry keeps its best score across
  variants. Top-K reports which variant each result matched on, so the agent
  can see why a lemma surfaced.
- Expansions are cached by SHA-256(query) under
  `<results_root>/.query_expand_cache.json`. Repeat queries are free.
- `--no-expand` disables the LLM call and reverts to the original
  single-query path. Useful for tests + when an interactive caller already
  knows the formal vocabulary.

Usage:
    python skills/search_semantic.py "pow2 mul div" --project <root> [-n 5]
"""
import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib import catalog  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(os.environ.get(
        "CLI_LOG_PATH", Path(__file__).parents[1] / "cli.log")))],
)
logger = logging.getLogger("search_semantic")


_EXPANSION_ERROR_MARKERS = (
    "not logged in",
    "please run /login",
    "api error",
    "authentication failed",
    "reauthentication failed",
    "login required",
)


def _tokens(s: str) -> set[str]:
    return set(w.lower() for w in re.findall(r"\w+", s) if w)


def _rank(entry: catalog.CatalogEntry, query_tokens: set[str]) -> int:
    name_tokens = _tokens(entry.name)
    sig_tokens = _tokens(entry.signature)
    name_hits = len(query_tokens & name_tokens)
    sig_hits = len(query_tokens & sig_tokens)
    # Each name-hit is worth 3 sig-hits. Axioms demoted (they're rarely
    # what the agent wants to call directly).
    score = 3 * name_hits + sig_hits
    if entry.kind == "axiom":
        score = max(0, score - 1)
    return score


def _looks_like_expansion_error(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _EXPANSION_ERROR_MARKERS)


def _write_expand_cache(cache_path: Path, cache: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    except OSError as exc:
        logger.warning("could not write expansion cache: %s", exc)


def _sanitize_cached_variants(query: str, variants: object) -> tuple[list[str], int]:
    """Drop poisoned/non-string cached alternates while preserving order."""
    clean = [query]
    dropped = 0
    if not isinstance(variants, list):
        return clean, 1
    for value in variants:
        if not isinstance(value, str):
            dropped += 1
            continue
        v = value.strip()
        if not v:
            dropped += 1
            continue
        if _looks_like_expansion_error(v):
            dropped += 1
            continue
        if v == query:
            continue
        if v not in clean:
            clean.append(v)
        else:
            dropped += 1
    return clean, dropped


# Kept deliberately SHORT: the dominant latency cost is haiku *generating*
# this output, so we ask for 3 terse lines, not 5 verbose ones. The three
# angles are the ones the token-overlap ranker actually scores against —
# snake_case `lemma_*` name fragments (match entry names) and formal math
# terms / textbook names (match signatures). Prose proof-pattern restatements
# were dropped: long and low-signal for substring matching.
_EXPAND_PROMPT = (
    "Rewrite this informal query for searching a Verus proof library into "
    "exactly 3 alternate phrasings, one per line — no numbering, bullets, or "
    "commentary, each under ~12 words. Use the formal vocabulary the catalog "
    "uses:\n"
    "- line 1: likely snake_case `lemma_*` name fragments "
    "(e.g. lemma_euclid_prime, lemma_mod_zero_iff)\n"
    "- line 2: precise math terms + any textbook lemma name "
    "(e.g. prime, divides, mod, cancellation, Euclid's lemma)\n"
    "- line 3: terse restatement with related formal vocabulary the query "
    "missed\n"
    "\n"
    "Query: {query}"
)


def _expand_query(query: str, cache_path: Path) -> tuple[list[str], dict]:
    """Return (variants, meta). `variants` always starts with the original
    query verbatim, followed by up to 3 alternate phrasings produced by a
    `claude -p --model haiku` call (retried once). Cached by sha256(query)."""
    h = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            cache = {}
    if h in cache:
        cached_variants, dropped = _sanitize_cached_variants(query, cache[h])
        if dropped:
            if len(cached_variants) > 1:
                cache[h] = cached_variants
                _write_expand_cache(cache_path, cache)
                logger.warning(
                    "query expansion cache sanitized for %r "
                    "(dropped %d poisoned/invalid variant(s))",
                    query, dropped)
                return cached_variants, {
                    "cache": "hit_sanitized",
                    "key": h,
                    "dropped_variants": dropped,
                }
            del cache[h]
            _write_expand_cache(cache_path, cache)
            logger.warning(
                "query expansion cache entry for %r was fully poisoned; "
                "discarded and retrying expansion", query)
        else:
            logger.info("query expansion cache HIT for %r (%d variants)",
                        query, len(cached_variants))
            return cached_variants, {"cache": "hit", "key": h}

    t0 = time.time()
    # --no-session-persistence keeps this sub-call from polluting the project
    # session dir; --strict-mcp-config skips MCP-server discovery (the agent's
    # MCP servers are irrelevant here, and a slow/failing one — e.g. a dead
    # connector — adds latency variance to every call). Retry once: the
    # dominant cost is haiku generating the phrasings, whose latency is
    # variable and occasionally exceeds the per-attempt budget, so a single
    # fresh attempt rescues most timeouts and empty replies.
    cmd = ["claude", "-p", "--model", "haiku",
           "--no-session-persistence", "--strict-mcp-config",
           _EXPAND_PROMPT.format(query=query)]
    # This nested `claude -p` must reach api.anthropic.com DIRECTLY, never through
    # a claude-tap proxy the parent run set via ANTHROPIC_BASE_URL (peel_run.sh
    # tap / docker --tap). claude-tap forwards Messages traffic but rejects the
    # CLI's startup auth/HEAD probe to a bare base URL ("Not logged in"), which
    # silently kills query expansion. Strip the override so the sub-call uses the
    # default endpoint; everything else (auth token, PATH, …) is inherited.
    _sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
    last_err = "no output"
    for attempt in (1, 2):
        try:
            # 60s/attempt: a real haiku generation via `claude -p` measures
            # ~20-49s here (prompt size barely matters), so 30s clipped the
            # upper half of the distribution. 60s covers the observed range in
            # one attempt; the retry backstops true outliers / empty replies.
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=60, env=_sub_env)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            last_err = str(exc)
            logger.warning("query expansion attempt %d FAILED (%s)", attempt, exc)
            continue
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            last_err = f"rc={proc.returncode}"
            detail = (stderr or stdout).strip()
            if detail:
                last_err += f": {detail[:200]}"
            logger.warning("query expansion attempt %d FAILED (%s)",
                           attempt, last_err)
            continue
        if (_looks_like_expansion_error(stdout)
                or _looks_like_expansion_error(stderr)):
            last_err = "claude expansion returned an auth/API error banner"
            logger.warning("query expansion attempt %d FAILED (%s)",
                           attempt, last_err)
            continue
        alternates = [ln.strip() for ln in stdout.splitlines()
                      if ln.strip()][:5]
        if not alternates:
            # Empty/blank reply (e.g. a nonzero exit that printed nothing to
            # stdout) used to slip through as a "successful" 0-variant
            # expansion. Treat it as a failure so it retries / is visible.
            last_err = f"no alternates (rc={proc.returncode})"
            logger.warning("query expansion attempt %d produced no alternates (rc=%d)",
                           attempt, proc.returncode)
            continue
        variants = [query] + alternates
        cache[h] = variants
        _write_expand_cache(cache_path, cache)
        elapsed = time.time() - t0
        logger.info("query expansion: %r → %d variants (%.1fs, attempt %d)",
                    query, len(variants), elapsed, attempt)
        return variants, {"cache": "miss_filled", "key": h,
                          "elapsed_s": elapsed, "attempts": attempt}

    logger.warning("query expansion FAILED after 2 attempts (%s) — "
                   "falling back to original query only", last_err)
    return [query], {"cache": "miss_failed", "key": h, "error": last_err}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="Natural-language or keyword query")
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--catalog-cache", type=Path, default=None)
    ap.add_argument("--vstd-root", type=Path, default=None, help="Path to vstd source — when set, vstd entries are added to the catalog and become queryable like project entries (no grep needed)")
    ap.add_argument("-n", "--num-results", type=int, default=5)
    ap.add_argument("--no-expand", action="store_true",
                    help="Disable LLM-driven query expansion; behave like the original single-query keyword matcher.")
    ap.add_argument("--expand-cache", type=Path, default=None,
                    help="Path to query-expansion cache JSON. Defaults to <catalog-cache dir>/.query_expand_cache.json, or <project>/.query_expand_cache.json.")
    args = ap.parse_args()

    entries = catalog.build(args.project, args.catalog_cache, args.vstd_root)

    # Build the variant list. Always at least the original query.
    if args.no_expand:
        variants: list[str] = [args.query]
        expand_meta = {"cache": "disabled"}
    else:
        cache_dir = (args.catalog_cache.parent if args.catalog_cache
                     else args.project)
        cache_path = (args.expand_cache if args.expand_cache
                      else cache_dir / ".query_expand_cache.json")
        variants, expand_meta = _expand_query(args.query, cache_path)

    # Score each entry as max-over-variants. Keep track of which variant
    # produced the best score so the agent can see why each result matched.
    best: dict[str, tuple[int, int]] = {}  # entry.name -> (score, variant_idx)
    name_to_entry: dict[str, catalog.CatalogEntry] = {}
    for idx, v in enumerate(variants):
        vt = _tokens(v)
        if not vt:
            continue
        for e in entries:
            s = _rank(e, vt)
            if s <= 0:
                continue
            cur = best.get(e.name)
            if cur is None or s > cur[0]:
                best[e.name] = (s, idx)
                name_to_entry[e.name] = e

    ordered = sorted(best.items(), key=lambda kv: -kv[1][0])
    top = ordered[:args.num_results]

    top_score = top[0][1][0] if top else 0
    logger.info("search_semantic: query=%r variants=%d matches=%d top_score=%s",
                args.query, len(variants), len(best), top_score)

    print(json.dumps({
        "query": args.query,
        "variants": variants,
        "expansion": expand_meta,
        "total_matches": len(best),
        "results": [
            {
                "name": name_to_entry[name].name,
                "kind": name_to_entry[name].kind,
                "signature": name_to_entry[name].signature,
                "file": name_to_entry[name].file,
                "line": name_to_entry[name].line,
                "module": name_to_entry[name].module_path,
                "source": name_to_entry[name].source,
                "score": score,
                "matched_variant_idx": variant_idx,
                "matched_variant": variants[variant_idx],
            }
            for name, (score, variant_idx) in top
        ],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
