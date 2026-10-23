# -*- coding: utf-8 -*-
"""Stdlib-only unittest suite for ``engine/kb_engine.py``.

Run from the project root:

    python test/engine_test.py -v

The tests deliberately use only the standard library: this environment has
neither numpy nor sentence-transformers, so the BM25 path is what must work.
The dense/hybrid path is exercised through the built-in deterministic
``KB_LITE_EMBED=fake`` backend, driven in subprocesses so the environment
never leaks into the other tests.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = PROJECT_ROOT / "engine" / "kb_engine.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Environment variables the child engine must not depend on.
_DROP_ENV = ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO")


def _load_engine():
    spec = importlib.util.spec_from_file_location("kb_engine_under_test",
                                                  str(ENGINE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KB = _load_engine()


class NoDense:
    """Explicit 'no embedding backend' so in-process tests ignore the env."""

    backend_kind = "none"
    capable = False
    model_name = None

    def ensure_loaded(self) -> bool:
        return False

    def encode(self, texts, is_query=False):
        raise RuntimeError("dense backend disabled for this test")


def make_engine(dense=None):
    return KB.KBEngine(dense_backend=NoDense() if dense is None else dense)


def write_doc(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def call(engine, op, **params):
    response = engine.handle_request({"id": "test", "op": op, "params": params})
    if not response.get("ok"):
        raise AssertionError("%s failed: %r" % (op, response))
    return response["result"]


def call_error(engine, op, **params):
    response = engine.handle_request({"id": "test", "op": op, "params": params})
    if response.get("ok"):
        raise AssertionError("%s unexpectedly succeeded: %r" % (op, response))
    return response["error"]


def _clean_env(extra=None):
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("KB_LITE_") and key not in _DROP_ENV
    }
    env.update(extra or {})
    return env


def run_engine(lines, env_extra=None, timeout=180):
    """Spawn the engine as a subprocess, feed NDJSON lines, collect replies."""
    proc = subprocess.Popen(
        [sys.executable, str(ENGINE_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_clean_env(env_extra),
        cwd=str(PROJECT_ROOT),
    )
    payload = "".join(line + "\n" for line in lines)
    try:
        stdout, stderr = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    parsed = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return parsed, stdout, stderr, proc.returncode


def build_search_corpus(test_case):
    """Create a tiny Chinese/English corpus and ingest it."""
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    base = Path(directory.name)
    docs = base / "docs"
    docs.mkdir()
    write_doc(docs / "cn.md",
              "# 检索笔记\n\n## 中文检索\n\n本地知识库使用汉字二元组分词，"
              "BM25 负责排序。\n")
    write_doc(docs / "en.md",
              "# Ops\n\n## Ranking\n\nBM25 ranks documents by term "
              "frequency and inverse document frequency.\n")
    write_doc(docs / "cook.md",
              "# Cooking\n\n## Pasta\n\nBoil water, add salt, cook "
              "spaghetti for nine minutes.\n")
    kb_root = base / ".kb-lite"
    engine = make_engine()
    result = call(engine, "ingest", kb_root=str(kb_root), paths=[str(docs)])
    return engine, kb_root, docs, result


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------
class ChunkerTests(unittest.TestCase):
    def test_markdown_heading_split_and_line_ranges(self):
        text = (FIXTURES / "sample.md").read_text(encoding="utf-8")
        source_lines = text.split("\n")
        title, chunks = KB.chunk_markdown(text, "sample")
        self.assertEqual(title, "Sample Handbook")
        self.assertTrue(chunks)
        headings = [chunk["heading"] for chunk in chunks]
        self.assertEqual(headings[0], "# Sample Handbook")
        self.assertIn("## Installation", headings)
        self.assertIn("## Usage", headings)
        self.assertIn("## 附录", headings)

        for chunk in chunks:
            self.assertGreaterEqual(chunk["start_line"], 1)
            self.assertGreaterEqual(chunk["end_line"], chunk["start_line"])
            self.assertLessEqual(chunk["end_line"], len(source_lines))
            self.assertIsNone(chunk["page"])

        install_line = next(
            number for number, line in enumerate(source_lines, 1)
            if line.strip() == "## Installation"
        )
        install_chunk = next(chunk for chunk in chunks
                             if chunk["heading"] == "## Installation")
        self.assertEqual(install_chunk["start_line"], install_line)
        self.assertIn("standard library only", install_chunk["text"])

        usage_chunk = next(chunk for chunk in chunks
                           if chunk["heading"] == "## Usage")
        self.assertIn("知识库使用说明：本工具把本地文档切分成块，用 BM25 排序后返回片段。",
                      usage_chunk["text"])

    def test_markdown_long_section_splits_under_1200_with_overlap(self):
        paragraph = "第%02d段：%s"
        body = "\n\n".join(
            paragraph % (index, "本地知识库检索。" * 40)
            for index in range(1, 7)
        )
        markdown = "# 长文\n\n## 大节\n\n" + body + "\n"
        title, chunks = KB.chunk_markdown(markdown, "long")
        self.assertEqual(title, "长文")
        section_chunks = [chunk for chunk in chunks
                          if chunk["heading"] == "## 大节"]
        self.assertGreaterEqual(len(section_chunks), 2)
        for chunk in section_chunks:
            self.assertLessEqual(len(chunk["text"]), 1200)
            self.assertTrue(chunk["text"])
        first, second = section_chunks[0], section_chunks[1]
        self.assertIn(first["text"][-60:], second["text"])
        self.assertLessEqual(second["start_line"], first["end_line"] + 1)
        self.assertTrue(any("本地知识库检索。" * 40 in chunk["text"]
                            for chunk in section_chunks))

    def test_split_offsets_cap_and_overlap(self):
        text = "abcdefghij" * 500  # 5000 chars, no natural break points
        spans = KB.split_offsets(text, 1200, 120)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(text))
        for start, end in spans:
            self.assertLessEqual(end - start, 1200)
            self.assertLess(start, end)
        for index in range(len(spans) - 1):
            self.assertEqual(spans[index + 1][0], spans[index][1] - 120)

    def test_cjk_text_is_not_broken(self):
        sentence = "中文分块必须保持汉字完整，不能出现乱码或半个字符。"
        body = "\n\n".join([sentence] * 80)
        self.assertGreater(len(body), 1200)
        _title, chunks = KB.chunk_text_plain(body, "cjk")
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertIn(chunk["text"], body)
            self.assertNotIn("\ufffd", chunk["text"])
        self.assertTrue(any(sentence in chunk["text"] for chunk in chunks))

    def test_plain_text_paragraphs_and_ranges(self):
        text = (FIXTURES / "notes.txt").read_text(encoding="utf-8")
        source_lines = text.split("\n")
        title, chunks = KB.chunk_text_plain(text, "notes")
        self.assertEqual(title, "Notes for the knowledge base engine.")
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertGreaterEqual(chunk["start_line"], 1)
            self.assertLessEqual(chunk["end_line"], len(source_lines))
            self.assertIsNone(chunk["page"])
            self.assertLessEqual(len(chunk["text"]), 1200)
        self.assertTrue(any("中文备注" in chunk["text"] for chunk in chunks))

    def test_tokenizer_latin_and_cjk(self):
        tokens = KB.tokenize("网络共享 BM25 Ranking")
        for expected in ("bm25", "ranking", "网络", "络共", "共享",
                         "网", "络", "共", "享"):
            self.assertIn(expected, tokens)
        self.assertEqual(KB.tokenize(""), [])


# --------------------------------------------------------------------------
# BM25 + search
# --------------------------------------------------------------------------
class SearchTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.kb_root, self.docs, self.ingest_result = \
            build_search_corpus(self)

    def _search(self, query, **params):
        return call(self.engine, "search", kb_root=str(self.kb_root),
                    query=query, **params)

    def test_chinese_query_hits_the_chinese_chunk(self):
        result = self._search("汉字二元组")
        self.assertEqual(result["mode_used"], "bm25")
        self.assertTrue(result["hits"])
        top = result["hits"][0]
        self.assertTrue(top["path"].endswith("cn.md"))
        self.assertIn("汉字二元组", top["snippet"])
        self.assertGreater(top["score"], 0.0)

    def test_english_query_hits_the_english_chunk(self):
        result = self._search("inverse document frequency")
        self.assertTrue(result["hits"])
        top = result["hits"][0]
        self.assertTrue(top["path"].endswith("en.md"))
        self.assertIn("inverse document frequency", top["snippet"])

    def test_unrelated_query_scores_low(self):
        result = self._search("xylophone zzzz")
        self.assertEqual(result["hits"], [])

    def test_hit_shape_and_snippet_limit(self):
        long_text = "# 长文\n\n" + ("知识库检索片段。" * 80) + "\n"
        write_doc(self.docs / "long.md", long_text)
        call(self.engine, "ingest", kb_root=str(self.kb_root),
             paths=[str(self.docs / "long.md")])
        result = self._search("知识库检索")
        self.assertTrue(result["hits"])
        for hit in result["hits"]:
            for key in ("chunk_id", "doc_id", "path", "title", "heading",
                        "start_line", "end_line", "page", "score",
                        "matched_by", "snippet"):
                self.assertIn(key, hit)
            self.assertLessEqual(len(hit["snippet"]), 300)
            self.assertIsInstance(hit["start_line"], int)
            self.assertIsInstance(hit["end_line"], int)
            self.assertGreaterEqual(hit["end_line"], hit["start_line"])
            self.assertEqual(hit["matched_by"], ["bm25"])
        scores = [hit["score"] for hit in result["hits"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(any(len(hit["snippet"]) >= 100 for hit in result["hits"]))

    def test_dense_modes_fall_back_to_bm25(self):
        for mode in ("auto", "hybrid", "dense"):
            result = self._search("汉字二元组", mode=mode)
            self.assertEqual(result["mode_used"], "bm25", mode)
            self.assertTrue(result["hits"])

    def test_top_k_clamped(self):
        plenty = self._search("知识库", top_k=999)
        self.assertLessEqual(len(plenty["hits"]), 50)
        single = self._search("知识库", top_k=0)
        self.assertEqual(len(single["hits"]), 1)

    def test_invalid_mode_is_bad_request(self):
        error = call_error(self.engine, "search",
                           kb_root=str(self.kb_root), query="x", mode="magic")
        self.assertEqual(error["code"], "bad_request")

    def test_warmup_reports_unavailable_backend(self):
        result = call(self.engine, "warmup", kb_root=str(self.kb_root))
        self.assertFalse(result["ok"])
        self.assertFalse(result["dense"])
        self.assertFalse(result["loaded"])
        self.assertEqual(result["elapsed_ms"], 0)


# --------------------------------------------------------------------------
# ingest / stats / clear
# --------------------------------------------------------------------------
class IngestTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.docs = self.tmp / "docs"
        (self.docs / "sub").mkdir(parents=True)
        shutil.copy(FIXTURES / "sample.md", self.docs / "sample.md")
        shutil.copy(FIXTURES / "notes.txt", self.docs / "notes.txt")
        shutil.copy(FIXTURES / "sub" / "nested.md", self.docs / "sub" / "nested.md")
        self.kb_root = self.tmp / ".kb-lite"
        self.engine = make_engine()

    def _ingest(self, **overrides):
        params = {"kb_root": str(self.kb_root), "paths": [str(self.docs)]}
        params.update(overrides)
        return call(self.engine, "ingest", **params)

    def test_incremental_add_skip_update(self):
        first = self._ingest()
        self.assertEqual(first["added"], 3)
        self.assertEqual(first["updated"], 0)
        self.assertEqual(first["skipped"], 0)
        self.assertEqual(first["pending"], 0)
        self.assertEqual(first["errors"], [])
        self.assertEqual(len(first["docs"]), 3)
        self.assertIsInstance(first["elapsed_ms"], int)

        second = self._ingest()
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["skipped"], 3)
        self.assertEqual(second["pending"], 0)

        target = self.docs / "notes.txt"
        target.write_text(
            target.read_text(encoding="utf-8")
            + "\n\n新增段落：增量更新测试。\n",
            encoding="utf-8",
        )
        os.utime(target, (time.time() + 5, time.time() + 5))

        third = self._ingest()
        self.assertEqual(third["added"], 0)
        self.assertEqual(third["updated"], 1)
        self.assertEqual(third["skipped"], 2)
        self.assertEqual(len(third["docs"]), 1)
        self.assertTrue(third["docs"][0]["path"].endswith("notes.txt"))

    def test_force_reparses_everything(self):
        self._ingest()
        forced = self._ingest(force=True)
        self.assertEqual(forced["added"], 0)
        self.assertEqual(forced["updated"], 3)
        self.assertEqual(forced["skipped"], 0)

    def test_prune_removes_only_missing_files(self):
        self._ingest()
        victim = self.docs / "sub" / "nested.md"
        victim.unlink()

        # Without prune the vanished document stays searchable (old behaviour).
        kept = self._ingest()
        self.assertEqual(kept["removed"], 0)
        self.assertEqual(call(self.engine, "stats",
                              kb_root=str(self.kb_root))["docs"], 3)

        pruned = self._ingest(prune=True)
        self.assertEqual(pruned["removed"], 1)
        self.assertEqual(pruned["updated"], 0)
        self.assertEqual(pruned["skipped"], 2)
        self.assertTrue(pruned["removed_paths"][0].endswith("nested.md"))
        stats = call(self.engine, "stats", kb_root=str(self.kb_root))
        self.assertEqual(stats["docs"], 2)
        self.assertEqual(len(stats["paths"]), 2)

        # Documents outside the scanned paths are never pruned.
        outside = write_doc(self.tmp / "outside.md", "# 外部\n\n不应被删。\n")
        call(self.engine, "ingest", kb_root=str(self.kb_root),
             paths=[str(outside)])
        outside.unlink()
        scoped = self._ingest(prune=True)
        self.assertEqual(scoped["removed"], 0)
        self.assertEqual(call(self.engine, "stats",
                              kb_root=str(self.kb_root))["docs"], 3)

    def test_max_files_and_pending(self):
        first = self._ingest(max_files=2)
        self.assertEqual(first["added"], 2)
        self.assertEqual(first["pending"], 1)
        second = self._ingest(max_files=2)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["skipped"], 2)
        self.assertEqual(second["pending"], 1)

    def test_errors_are_per_file(self):
        missing = self._ingest(paths=[str(self.tmp / "does-not-exist")])
        self.assertEqual(missing["added"], 0)
        self.assertEqual(len(missing["errors"]), 1)
        self.assertEqual(missing["errors"][0]["code"], "ingest_path_missing")

        binary = self.tmp / "data.bin"
        binary.write_bytes(b"binary")
        unsupported = self._ingest(paths=[str(binary)])
        self.assertEqual(unsupported["errors"][0]["code"],
                         "unsupported_extension")

    def test_oversize_file_is_skipped(self):
        big = self.docs / "big.md"
        big.write_text("x" * 500, encoding="utf-8")
        original = KB.MAX_FILE_BYTES
        KB.MAX_FILE_BYTES = 100
        try:
            result = self._ingest(paths=[str(big)])
        finally:
            KB.MAX_FILE_BYTES = original
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["code"], "file_too_large")

    def test_pdf_without_pymupdf_reports_error_not_crash(self):
        pdf = self.docs / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4\nthis is not a real pdf\n")
        result = self._ingest(paths=[str(pdf)])
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(result["errors"][0]["code"].startswith("pdf"),
                        result["errors"])

    def test_stats_counts_and_manifest_layout(self):
        ingested = self._ingest()
        stats = call(self.engine, "stats", kb_root=str(self.kb_root))
        self.assertEqual(stats["docs"], 3)
        self.assertEqual(stats["chunks"],
                         sum(doc["chunks"] for doc in ingested["docs"]))
        self.assertEqual(len(stats["paths"]), 3)
        self.assertTrue(all(isinstance(path, str) for path in stats["paths"]))
        self.assertGreater(stats["bytes"], 0)
        self.assertIsNotNone(stats["updated_at"])
        self.assertFalse(stats["dense"])
        self.assertIsNone(stats["dense_model"])
        self.assertEqual(stats["backend"], "none")
        self.assertFalse(stats["dense_loaded"])
        self.assertFalse(stats["dense_loading"])
        self.assertIsNone(stats["vectors"])
        self.assertEqual(Path(stats["kb_root"]).resolve(), self.kb_root.resolve())

        with open(self.kb_root / "chunks.jsonl", encoding="utf-8") as handle:
            stored = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(stored), stats["chunks"])
        for chunk in stored:
            for key in ("chunk_id", "doc_id", "ordinal", "heading",
                        "start_line", "end_line", "page", "text"):
                self.assertIn(key, chunk)
            self.assertEqual(chunk["chunk_id"],
                             "%s:%s" % (chunk["doc_id"], chunk["ordinal"]))

        manifest = json.loads(
            (self.kb_root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(len(manifest["docs"]), 3)
        for entry in manifest["docs"].values():
            for key in ("path", "sha256", "mtime", "size", "title", "kind",
                        "pages", "chunks"):
                self.assertIn(key, entry)

        log_path = self.kb_root / "engine.log"
        self.assertTrue(log_path.exists())
        self.assertIn("ingest finished", log_path.read_text(encoding="utf-8"))

    def test_storage_writes_are_confined_to_kb_root(self):
        for name in ("../escaped.json", "..\\escaped.json"):
            with self.assertRaises(KB.KBError):
                KB._child(str(self.kb_root), name)

    def test_paths_capped_at_fifty(self):
        many = self.tmp / "many"
        many.mkdir()
        for index in range(55):
            write_doc(many / ("doc%02d.md" % index),
                      "# 文档 %02d\n\n内容 %02d。\n" % (index, index))
        result = self._ingest(paths=[str(many)], max_files=100)
        self.assertEqual(result["added"], 55)
        stats = call(self.engine, "stats", kb_root=str(self.kb_root))
        self.assertEqual(stats["docs"], 55)
        self.assertEqual(len(stats["paths"]), 50)

    def test_clear_requires_confirm_and_removes_chunks(self):
        self._ingest()
        error = call_error(self.engine, "clear", kb_root=str(self.kb_root))
        self.assertEqual(error["code"], "confirm_required")
        error = call_error(self.engine, "clear", kb_root=str(self.kb_root),
                           confirm="true")
        self.assertEqual(error["code"], "confirm_required")
        self.assertTrue((self.kb_root / "chunks.jsonl").exists())

        cleared = call(self.engine, "clear", kb_root=str(self.kb_root),
                       confirm=True)
        self.assertTrue(cleared["cleared"])
        self.assertEqual(cleared["removed_docs"], 3)
        self.assertFalse((self.kb_root / "chunks.jsonl").exists())
        self.assertFalse((self.kb_root / "manifest.json").exists())
        stats = call(self.engine, "stats", kb_root=str(self.kb_root))
        self.assertEqual(stats["chunks"], 0)
        self.assertEqual(stats["docs"], 0)


# --------------------------------------------------------------------------
# ask
# --------------------------------------------------------------------------
class AskTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.kb_root, self.docs, self.ingest_result = \
            build_search_corpus(self)
        write_doc(self.docs / "long.md",
                  "# 长文\n\n" + ("知识库" * 800) + "\n")
        call(self.engine, "ingest", kb_root=str(self.kb_root),
             paths=[str(self.docs / "long.md")])
        self.stored_chunks = [
            json.loads(line) for line in
            (self.kb_root / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_ask_returns_evidence_within_budget(self):
        budget = 600
        result = call(self.engine, "ask", kb_root=str(self.kb_root),
                      query="知识库", max_chars=budget)
        self.assertTrue(result["evidence"])
        self.assertLessEqual(result["total_chars"], budget)
        self.assertEqual(result["mode_used"], "bm25")
        computed = sum(len(item["text"]) for item in result["evidence"])
        self.assertEqual(computed, result["total_chars"])
        for item in result["evidence"]:
            self.assertTrue(item["text"])
            for key in ("path", "title", "heading", "start_line", "end_line",
                        "page", "score", "text"):
                self.assertIn(key, item)

    def test_ask_evidence_is_verbatim_source_text(self):
        result = call(self.engine, "ask", kb_root=str(self.kb_root),
                      query="知识库", max_chars=6000)
        self.assertTrue(result["evidence"])
        for item in result["evidence"]:
            self.assertTrue(
                any(item["text"] in chunk["text"]
                    for chunk in self.stored_chunks),
                "evidence text is not a verbatim slice of a stored chunk",
            )

    def test_ask_budget_is_clamped(self):
        result = call(self.engine, "ask", kb_root=str(self.kb_root),
                      query="知识库", max_chars=100)
        self.assertLessEqual(result["total_chars"], 500)
        self.assertGreater(result["total_chars"], 0)
        self.assertLessEqual(len(result["evidence"][0]["text"]), 500)

    def test_ask_without_matches_returns_empty_evidence(self):
        result = call(self.engine, "ask", kb_root=str(self.kb_root),
                      query="zzz qqq")
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["total_chars"], 0)
        self.assertEqual(result["mode_used"], "bm25")


# --------------------------------------------------------------------------
# NDJSON protocol (subprocess)
# --------------------------------------------------------------------------
class ProtocolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.kb_root = self.tmp / ".kb-lite"

    def test_ready_malformed_json_and_unknown_op(self):
        lines = [
            json.dumps({"id": "1", "op": "ping", "params": {}}),
            "{this is definitely not json",
            json.dumps({"id": "2", "op": "ping", "params": {}}),
            json.dumps({"id": "3", "op": "nope", "params": {}}),
            json.dumps({"id": "4", "op": "stats",
                        "params": {"kb_root": str(self.kb_root)}}),
            json.dumps({"id": "5", "op": "clear",
                        "params": {"kb_root": str(self.kb_root)}}),
        ]
        parsed, stdout, _stderr, returncode = run_engine(lines)
        self.assertEqual(returncode, 0)
        self.assertEqual(len(parsed), len(lines) + 1)
        self.assertFalse(stdout.startswith("\ufeff"))

        ready = parsed[0]
        self.assertEqual(ready["event"], "ready")
        self.assertEqual(ready["version"], "0.1.0")
        self.assertIn("dense", ready)
        self.assertIn("numpy", ready)
        self.assertIn(ready["backend"], ("none", "sentence-transformers", "fake"))

        first_ping = parsed[1]
        self.assertEqual(first_ping["id"], "1")
        self.assertTrue(first_ping["ok"])
        self.assertTrue(first_ping["result"]["pong"])
        self.assertEqual(set(first_ping["result"]),
                         {"pong", "version", "dense", "dense_loaded", "numpy"})

        malformed = parsed[2]
        self.assertIsNone(malformed["id"])
        self.assertFalse(malformed["ok"])
        self.assertEqual(malformed["error"]["code"], "bad_request")

        second_ping = parsed[3]
        self.assertEqual(second_ping["id"], "2")
        self.assertTrue(second_ping["ok"])

        unknown = parsed[4]
        self.assertEqual(unknown["id"], "3")
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["error"]["code"], "unknown_op")

        stats = parsed[5]
        self.assertEqual(stats["id"], "4")
        self.assertTrue(stats["ok"])
        self.assertEqual(stats["result"]["docs"], 0)

        clear = parsed[6]
        self.assertEqual(clear["id"], "5")
        self.assertFalse(clear["ok"])
        self.assertEqual(clear["error"]["code"], "confirm_required")

    def test_non_object_request_is_bad_request(self):
        lines = [
            json.dumps({"id": "1", "op": "ping", "params": {}}),
            "[]",
            json.dumps({"id": "2", "op": "ping", "params": {}}),
        ]
        parsed, _stdout, _stderr, returncode = run_engine(lines)
        self.assertEqual(returncode, 0)
        self.assertEqual(parsed[2]["error"]["code"], "bad_request")
        self.assertTrue(parsed[3]["ok"])

    def test_two_roots_in_one_process_are_independent(self):
        docs_a = self.tmp / "docs-a"
        docs_b = self.tmp / "docs-b"
        write_doc(docs_a / "only_a.md", "# A\n\nalpha bravo content.\n")
        write_doc(docs_b / "only_b.md", "# B\n\ncharlie delta content.\n")
        root_a = self.tmp / "kb-a"
        root_b = self.tmp / "kb-b"
        requests = [
            {"id": "1", "op": "ingest",
             "params": {"kb_root": str(root_a), "paths": [str(docs_a)]}},
            {"id": "2", "op": "ingest",
             "params": {"kb_root": str(root_b), "paths": [str(docs_b)]}},
            {"id": "3", "op": "stats", "params": {"kb_root": str(root_a)}},
            {"id": "4", "op": "stats", "params": {"kb_root": str(root_b)}},
            {"id": "5", "op": "search",
             "params": {"kb_root": str(root_a), "query": "alpha"}},
            {"id": "6", "op": "search",
             "params": {"kb_root": str(root_b), "query": "alpha"}},
            {"id": "7", "op": "clear",
             "params": {"kb_root": str(root_a), "confirm": True}},
            {"id": "8", "op": "stats", "params": {"kb_root": str(root_a)}},
            {"id": "9", "op": "stats", "params": {"kb_root": str(root_b)}},
        ]
        lines = [json.dumps(request, ensure_ascii=False) for request in requests]
        parsed, _stdout, stderr, returncode = run_engine(lines)
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(parsed[3]["result"]["docs"], 1)
        self.assertEqual(parsed[4]["result"]["docs"], 1)
        self.assertEqual(len(parsed[5]["result"]["hits"]), 1)
        self.assertTrue(parsed[5]["result"]["hits"][0]["path"].endswith("only_a.md"))
        self.assertEqual(parsed[6]["result"]["hits"], [])
        self.assertTrue(parsed[7]["result"]["cleared"])
        self.assertEqual(parsed[8]["result"]["docs"], 0)
        self.assertEqual(parsed[9]["result"]["docs"], 1)


# --------------------------------------------------------------------------
# fake embedding backend / hybrid retrieval (subprocess, env isolated)
# --------------------------------------------------------------------------
class FakeEmbedderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.docs = self.tmp / "docs"
        self.docs.mkdir()
        write_doc(self.docs / "a.md",
                  "# 检索\n\n本地知识库使用汉字二元组分词，BM25 负责排序。\n")
        write_doc(self.docs / "b.md",
                  "# 烹饪\n\n烧水，加盐，煮面九分钟。\n")
        write_doc(self.docs / "c.md",
                  "# Ranking\n\nBM25 ranks documents by term frequency.\n")
        self.kb_root = self.tmp / ".kb-lite"

    def test_fake_backend_enables_hybrid(self):
        requests = [
            {"id": "1", "op": "ingest",
             "params": {"kb_root": str(self.kb_root), "paths": [str(self.docs)]}},
            {"id": "2", "op": "stats", "params": {"kb_root": str(self.kb_root)}},
            {"id": "3", "op": "search",
             "params": {"kb_root": str(self.kb_root), "query": "BM25 排序",
                        "mode": "hybrid"}},
            {"id": "4", "op": "search",
             "params": {"kb_root": str(self.kb_root), "query": "BM25 排序",
                        "mode": "bm25"}},
            {"id": "5", "op": "search",
             "params": {"kb_root": str(self.kb_root), "query": "BM25 排序",
                        "mode": "dense"}},
        ]
        lines = [json.dumps(request, ensure_ascii=False) for request in requests]
        parsed, _stdout, stderr, returncode = run_engine(
            lines, env_extra={"KB_LITE_EMBED": "fake"})
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(len(parsed), len(lines) + 1)

        ready = parsed[0]
        self.assertEqual(ready["backend"], "fake")
        self.assertTrue(ready["dense"])

        ingest = parsed[1]["result"]
        self.assertEqual(ingest["added"], 3)

        stats = parsed[2]["result"]
        self.assertTrue(stats["dense"])
        self.assertEqual(stats["backend"], "fake")
        self.assertEqual(stats["dense_model"], "fake-hash-64")
        vectors = self.kb_root / "vectors.json"
        if not vectors.exists():
            vectors = self.kb_root / "vectors.npy"
        self.assertTrue(vectors.exists(), "no vector store was written")

        hybrid = parsed[3]["result"]
        self.assertEqual(hybrid["mode_used"], "hybrid")
        self.assertTrue(hybrid["hits"])
        self.assertTrue(
            any("bm25" in hit["matched_by"] and "dense" in hit["matched_by"]
                for hit in hybrid["hits"]),
            hybrid["hits"],
        )

        bm25 = parsed[4]["result"]
        self.assertEqual(bm25["mode_used"], "bm25")
        self.assertTrue(bm25["hits"])

        dense = parsed[5]["result"]
        self.assertEqual(dense["mode_used"], "dense")
        self.assertTrue(dense["hits"])

    def test_fake_backend_dimension_override(self):
        lines = [json.dumps({"id": "1", "op": "stats",
                             "params": {"kb_root": str(self.kb_root)}})]
        parsed, _stdout, stderr, returncode = run_engine(
            lines, env_extra={"KB_LITE_EMBED": "fake", "KB_LITE_EMBED_DIM": "32"})
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(parsed[0]["backend"], "fake")
        self.assertEqual(parsed[0]["dense"], True)
        stats = parsed[1]["result"]
        self.assertEqual(stats["dense_model"], "fake-hash-32")
        self.assertEqual(stats["backend"], "fake")

    def test_auto_uses_hybrid_when_backend_is_eager(self):
        engine = make_engine(KB.FakeHashBackend(8))
        call(engine, "ingest", kb_root=str(self.kb_root), paths=[str(self.docs)],
             with_embeddings="on")
        result = call(engine, "search", kb_root=str(self.kb_root),
                      query="BM25 排序", mode="auto")
        self.assertEqual(result["mode_used"], "hybrid")

    def test_lazy_auto_defers_dense_load(self):
        class LazyFake(KB.FakeHashBackend):
            lazy = True

            def __init__(self):
                super().__init__(8)
                self.warm_started = False

            @property
            def is_loaded(self):
                return False

            def start_warmup(self):
                self.warm_started = True
                return True

        engine = make_engine(LazyFake())
        call(engine, "ingest", kb_root=str(self.kb_root), paths=[str(self.docs)],
             with_embeddings="on")
        result = call(engine, "search", kb_root=str(self.kb_root),
                      query="BM25 排序", mode="auto")
        self.assertEqual(result["mode_used"], "bm25")
        self.assertTrue(engine.dense.warm_started)
        forced = call(engine, "search", kb_root=str(self.kb_root),
                      query="BM25 排序", mode="hybrid")
        self.assertEqual(forced["mode_used"], "hybrid")

    def test_vector_metadata_roundtrip_and_model_mismatch(self):
        engine = make_engine(KB.FakeHashBackend(8))
        call(engine, "ingest", kb_root=str(self.kb_root), paths=[str(self.docs)],
             with_embeddings="on")
        meta_path = self.kb_root / "vectors.meta.json"
        self.assertTrue(meta_path.exists())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["dim"], 8)
        self.assertEqual(meta["model"], "fake-hash-8")
        stats = call(engine, "stats", kb_root=str(self.kb_root))
        self.assertEqual(stats["vectors"]["dim"], 8)
        self.assertEqual(stats["vectors"]["model"], "fake-hash-8")
        self.assertIsNotNone(engine._load_vectors(str(self.kb_root)))
        meta["model"] = "another-model"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        self.assertIsNone(engine._load_vectors(str(self.kb_root)))

    def test_dense_disabled_env_forces_bm25(self):
        requests = [
            {"id": "1", "op": "ingest",
             "params": {"kb_root": str(self.kb_root), "paths": [str(self.docs)]}},
            {"id": "2", "op": "stats", "params": {"kb_root": str(self.kb_root)}},
            {"id": "3", "op": "search",
             "params": {"kb_root": str(self.kb_root), "query": "BM25 排序",
                        "mode": "hybrid"}},
        ]
        lines = [json.dumps(request, ensure_ascii=False) for request in requests]
        parsed, _stdout, stderr, returncode = run_engine(
            lines, env_extra={"KB_LITE_EMBED": "fake", "KB_LITE_DENSE": "0"})
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(parsed[0]["backend"], "none")
        self.assertFalse(parsed[0]["dense"])
        stats = parsed[2]["result"]
        self.assertFalse(stats["dense"])
        self.assertEqual(stats["backend"], "none")
        self.assertIsNone(stats["dense_model"])
        hybrid = parsed[3]["result"]
        self.assertEqual(hybrid["mode_used"], "bm25")
        self.assertTrue(hybrid["hits"])


# --------------------------------------------------------------------------
# UTF-8 / CP936 encoding regression (subprocess without PYTHONIOENCODING)
# --------------------------------------------------------------------------
class EncodingTests(unittest.TestCase):
    def test_chinese_survives_cp936_console_default(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        docs = base / "知识库"
        docs.mkdir()
        article = (
            "# 网络共享指南\n\n"
            "## 概述\n\n"
            "网络共享用于在多台电脑之间共享文件，本指南介绍配置步骤。\n\n"
            "## 配置步骤\n\n"
            "1. 打开控制面板。\n"
            "2. 启用网络发现和文件共享。\n\n"
            "## 故障排查\n\n"
            "如果无法访问网络共享，请检查防火墙设置和权限。\n"
        )
        source = docs / "网络共享.md"
        source.write_text(article, encoding="utf-8")
        kb_root = base / ".kb-lite"

        requests = [
            {"id": "1", "op": "ingest",
             "params": {"kb_root": str(kb_root), "paths": [str(docs)]}},
            {"id": "2", "op": "search",
             "params": {"kb_root": str(kb_root), "query": "网络共享"}},
            {"id": "3", "op": "ask",
             "params": {"kb_root": str(kb_root), "query": "网络共享",
                        "max_chars": 6000}},
        ]
        lines = [json.dumps(request, ensure_ascii=False) for request in requests]
        # Deliberately empty: the engine must force UTF-8 on its own.
        parsed, stdout, stderr, returncode = run_engine(
            lines, env_extra={"PYTHONIOENCODING": "", "KB_LITE_DENSE": "0"})
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(len(parsed), len(lines) + 1)
        self.assertEqual(parsed[0]["backend"], "none")
        self.assertNotIn("\ufffd", stdout)
        self.assertFalse(stdout.startswith("\ufeff"))

        ingest = parsed[1]["result"]
        self.assertEqual(ingest["added"], 1)
        self.assertIn("知识库", ingest["docs"][0]["path"])
        self.assertIn("网络共享.md", ingest["docs"][0]["path"])

        search = parsed[2]["result"]
        self.assertTrue(search["hits"])
        self.assertIn("网络共享", search["hits"][0]["snippet"])
        self.assertIn("知识库", search["hits"][0]["path"])

        ask = parsed[3]["result"]
        self.assertTrue(ask["evidence"])
        joined = "\n".join(item["text"] for item in ask["evidence"])
        self.assertIn("网络共享", joined)
        self.assertIn("网络共享.md", ask["evidence"][0]["path"])


if __name__ == "__main__":
    # Keep the harness readable on a cp936 console when output is captured
    # (the engine itself independently forces UTF-8 in serve()).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    unittest.main(verbosity=2)
