"""Tests for semantic-search query expansion hygiene."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills"))

import search_semantic  # noqa: E402


class QueryExpansionCacheHygiene(unittest.TestCase):
    def test_nonzero_banner_output_is_not_cached(self):
        query = "prime divides product"
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / ".query_expand_cache.json"
            proc = SimpleNamespace(
                returncode=1,
                stdout="Not logged in \u00b7 Please run /login\n",
                stderr="",
            )
            with patch.object(search_semantic.subprocess, "run",
                              return_value=proc) as run:
                variants, meta = search_semantic._expand_query(query, cache)

            self.assertEqual(run.call_count, 2)
            self.assertEqual(variants, [query])
            self.assertEqual(meta["cache"], "miss_failed")
            if cache.exists():
                self.assertNotIn("Not logged in", cache.read_text())

    def test_error_banner_with_zero_exit_is_not_cached(self):
        query = "field byte encoding uniqueness"
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / ".query_expand_cache.json"
            proc = SimpleNamespace(
                returncode=0,
                stdout="Not logged in \u00b7 Please run /login\n",
                stderr="",
            )
            with patch.object(search_semantic.subprocess, "run",
                              return_value=proc):
                variants, meta = search_semantic._expand_query(query, cache)

            self.assertEqual(variants, [query])
            self.assertEqual(meta["cache"], "miss_failed")
            if cache.exists():
                self.assertNotIn("Please run /login", cache.read_text())

    def test_poisoned_cache_entry_is_removed_and_retried(self):
        query = "montgomery ladder step"
        key = search_semantic.hashlib.sha256(
            query.encode("utf-8")).hexdigest()[:16]
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / ".query_expand_cache.json"
            cache.write_text(json.dumps({
                key: [query, "Not logged in \u00b7 Please run /login"],
            }))
            proc = SimpleNamespace(
                returncode=0,
                stdout="lemma_ladder_step\nprojective representation\nscalar mul\n",
                stderr="",
            )
            with patch.object(search_semantic.subprocess, "run",
                              return_value=proc) as run:
                variants, meta = search_semantic._expand_query(query, cache)

            self.assertEqual(run.call_count, 1)
            self.assertEqual(meta["cache"], "miss_filled")
            self.assertEqual(
                variants,
                [query, "lemma_ladder_step", "projective representation",
                 "scalar mul"],
            )
            cached = json.loads(cache.read_text())
            self.assertNotIn("Not logged in", json.dumps(cached))

    def test_mixed_cache_entry_is_sanitized_without_rerunning(self):
        query = "canonical byte equality"
        key = search_semantic.hashlib.sha256(
            query.encode("utf-8")).hexdigest()[:16]
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / ".query_expand_cache.json"
            cache.write_text(json.dumps({
                key: [
                    query,
                    "lemma_canonical_bytes_equal",
                    "Not logged in \u00b7 Please run /login",
                ],
            }))
            with patch.object(search_semantic.subprocess, "run") as run:
                variants, meta = search_semantic._expand_query(query, cache)

            run.assert_not_called()
            self.assertEqual(meta["cache"], "hit_sanitized")
            self.assertEqual(variants, [query, "lemma_canonical_bytes_equal"])
            self.assertNotIn("Please run /login", cache.read_text())


if __name__ == "__main__":
    unittest.main()
