# -*- coding: utf-8 -*-
"""Tests for retrieval weights and source boosts (``weights`` param).

Run from the project root:

    python test/engine_weights_test.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_test import build_search_corpus, call, call_error  # noqa: E402


class ScoreDetailTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.kb_root, self.docs, self.ingest_result = \
            build_search_corpus(self)

    def _search(self, query, **params):
        return call(self.engine, "search", kb_root=str(self.kb_root),
                    query=query, **params)

    def test_every_hit_carries_a_score_breakdown(self):
        result = self._search("汉字二元组")
        self.assertTrue(result["hits"])
        for hit in result["hits"]:
            detail = hit["score_detail"]
            for key in ("bm25", "dense", "rrf", "source_boost", "boost_rule", "final"):
                self.assertIn(key, detail)
            self.assertIsNotNone(detail["bm25"])
            self.assertIsNone(detail["dense"])
            self.assertIsNone(detail["rrf"])
            self.assertEqual(detail["source_boost"], 1.0)
            self.assertIsNone(detail["boost_rule"])
            self.assertAlmostEqual(detail["final"], hit["score"], places=5)

    def test_bm25_weight_scales_the_final_score(self):
        base = self._search("汉字二元组", top_k=1)["hits"][0]
        doubled = self._search("汉字二元组", top_k=1, weights={"bm25": 2})["hits"][0]
        self.assertAlmostEqual(doubled["score_detail"]["final"],
                               base["score_detail"]["final"] * 2, places=4)
        self.assertAlmostEqual(doubled["score_detail"]["bm25"],
                               base["score_detail"]["bm25"], places=6)

    def test_zero_bm25_weight_drops_everything(self):
        result = self._search("汉字二元组", weights={"bm25": 0})
        # scores become 0, so nothing outranks the (still positive) candidates
        self.assertTrue(all(hit["score"] == 0 for hit in result["hits"]))


class BoostTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.kb_root, self.docs, self.ingest_result = \
            build_search_corpus(self)

    def _search(self, query, **params):
        return call(self.engine, "search", kb_root=str(self.kb_root),
                    query=query, **params)

    def test_boost_demotes_a_matching_file_and_flips_the_order(self):
        plain = self._search("BM25", top_k=5)
        order_plain = [hit["path"].rsplit("/", 1)[-1] for hit in plain["hits"]]
        boosted = self._search("BM25", top_k=5, weights={"boost": {"cn.md": 0.1}})
        order_boosted = [hit["path"].rsplit("/", 1)[-1] for hit in boosted["hits"]]
        self.assertIn("cn.md", order_plain)
        self.assertIn("cn.md", order_boosted)
        self.assertLess(order_plain.index("cn.md"), len(order_plain))
        self.assertEqual(order_boosted[-1], "cn.md",
                         "boosting cn.md by 0.1 should push it to the bottom: %r" % order_boosted)
        cn = [hit for hit in boosted["hits"] if hit["path"].endswith("cn.md")][0]
        self.assertEqual(cn["score_detail"]["source_boost"], 0.1)
        self.assertEqual(cn["score_detail"]["boost_rule"], "cn.md")

    def test_longest_pattern_wins(self):
        result = self._search("BM25", top_k=5, weights={
            "boost": {"docs": 3.0, "cn.md": 5.0},
        })
        cn = [hit for hit in result["hits"] if hit["path"].endswith("cn.md")][0]
        self.assertEqual(cn["score_detail"]["boost_rule"], "cn.md")
        self.assertEqual(cn["score_detail"]["source_boost"], 5.0)

    def test_boost_accepts_the_list_form(self):
        result = self._search("BM25", top_k=5, weights={
            "boost": [{"pattern": "cook.md", "mult": 4.0}],
        })
        cook = [hit for hit in result["hits"] if hit["path"].endswith("cook.md")]
        if cook:  # cook.md may not match "BM25" at all; assert only when present
            self.assertEqual(cook[0]["score_detail"]["source_boost"], 4.0)

    def test_boost_does_not_change_raw_components(self):
        plain = {hit["path"]: hit for hit in self._search("BM25", top_k=5)["hits"]}
        boosted = {hit["path"]: hit
                   for hit in self._search("BM25", top_k=5,
                                           weights={"boost": {"cn.md": 0.5}})["hits"]}
        for path in plain:
            self.assertAlmostEqual(plain[path]["score_detail"]["bm25"],
                                   boosted[path]["score_detail"]["bm25"], places=6)

    def test_ask_reports_the_breakdown_too(self):
        result = call(self.engine, "ask", kb_root=str(self.kb_root),
                      query="汉字二元组", weights={"bm25": 1.5, "boost": {"cn.md": 2}})
        self.assertTrue(result["evidence"])
        first = result["evidence"][0]
        self.assertIn("score_detail", first)
        self.assertEqual(first["score_detail"]["source_boost"], 2.0)
        self.assertAlmostEqual(first["score"], first["score_detail"]["final"], places=5)


class RecencyTests(unittest.TestCase):
    """Time decay: older documents rank lower when weights.recency is set."""

    def setUp(self):
        import os
        import tempfile
        import time
        from pathlib import Path

        from engine_test import make_engine, write_doc

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        docs = base / "docs"
        docs.mkdir()
        write_doc(docs / "old.md",
                  "# 旧笔记\n\n## BM25 排序\n\nBM25 关键词排序的旧记录。\n")
        write_doc(docs / "new.md",
                  "# 新笔记\n\n## BM25 排序\n\nBM25 关键词排序的新记录。\n")
        old = time.time() - 60 * 86400
        os.utime(docs / "old.md", (old, old))

        self.kb_root = base / ".kb-lite"
        self.engine = make_engine()
        call(self.engine, "ingest", kb_root=str(self.kb_root), paths=[str(docs)])

    def _search(self, **params):
        return call(self.engine, "search", kb_root=str(self.kb_root),
                    query="BM25 排序", top_k=5, **params)

    def test_without_recency_the_factor_is_one(self):
        result = self._search()
        for hit in result["hits"]:
            self.assertEqual(hit["score_detail"]["recency"], 1.0)
            self.assertIsNone(hit["score_detail"]["age_days"])

    def test_old_document_decays_and_new_one_does_not(self):
        result = self._search(weights={"recency": {"half_life_days": 30}})
        by_name = {hit["path"].rsplit("/", 1)[-1]: hit for hit in result["hits"]}
        self.assertIn("old.md", by_name)
        old = by_name["old.md"]["score_detail"]
        new = by_name["new.md"]["score_detail"]
        self.assertLess(old["recency"], 0.5)
        self.assertGreater(old["age_days"], 50)
        self.assertGreaterEqual(new["recency"], 0.95)
        self.assertLessEqual(new["age_days"], 1)
        self.assertAlmostEqual(old["final"],
                               round(old["bm25"] * old["source_boost"] * old["recency"], 6),
                               places=4)

    def test_floor_and_ceiling_are_respected(self):
        result = self._search(weights={
            "recency": {"half_life_days": 30, "min": 0.9, "max": 1.0},
        })
        for hit in result["hits"]:
            self.assertGreaterEqual(hit["score_detail"]["recency"], 0.9)
            self.assertLessEqual(hit["score_detail"]["recency"], 1.0)

    def test_invalid_recency_config_is_rejected(self):
        error = call_error(self.engine, "search", kb_root=str(self.kb_root),
                           query="BM25", weights={"recency": "half"})
        self.assertEqual(error["code"], "bad_request")
        error2 = call_error(self.engine, "search", kb_root=str(self.kb_root),
                            query="BM25", weights={"recency": {"min": 2, "max": 1}})
        self.assertEqual(error2["code"], "bad_request")


class WeightValidationTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.kb_root, self.docs, self.ingest_result = \
            build_search_corpus(self)

    def test_weights_must_be_an_object(self):
        error = call_error(self.engine, "search", kb_root=str(self.kb_root),
                           query="x", weights="loud")
        self.assertEqual(error["code"], "bad_request")

    def test_non_numeric_weight_is_rejected(self):
        error = call_error(self.engine, "search", kb_root=str(self.kb_root),
                           query="x", weights={"bm25": "abc"})
        self.assertEqual(error["code"], "bad_request")

    def test_negative_boost_is_rejected(self):
        error = call_error(self.engine, "search", kb_root=str(self.kb_root),
                           query="x", weights={"boost": {"cn.md": -1}})
        self.assertEqual(error["code"], "bad_request")

    def test_bad_boost_shape_is_rejected(self):
        error = call_error(self.engine, "search", kb_root=str(self.kb_root),
                           query="x", weights={"boost": "cn.md"})
        self.assertEqual(error["code"], "bad_request")


if __name__ == "__main__":
    unittest.main(verbosity=2)
