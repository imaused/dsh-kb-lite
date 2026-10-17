#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NDJSON knowledge-base engine for the dsh-kb-lite DeepSeek Harness plugin.

The wire contract lives in ``PROTOCOL.md`` next to this file.  In short:

* stdin  -- one JSON request per line (NDJSON, UTF-8, no BOM)
* stdout -- exactly one JSON response line per request, plus one ``ready``
  event at startup.  Nothing else may ever be written to stdout.
* stderr -- human readable logs (also mirrored to ``<kb_root>/engine.log``).

The engine is deliberately dependency free: chunking, BM25 retrieval and the
NDJSON loop only use the Python standard library.  Dense retrieval is an
optional best-effort layer on top of ``numpy`` + ``sentence-transformers``;
it is imported lazily and the engine silently degrades to BM25 when either
package (or the model) is unavailable.

Python 3.9+ / single file / no third-party imports required.
"""

from __future__ import annotations

import bisect
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
import traceback
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ENGINE_VERSION = "0.1.0"
MANIFEST_VERSION = 1

# --- safety / tuning caps -------------------------------------------------
MAX_FILE_BYTES = 20 * 1024 * 1024          # skip files above 20 MB
MAX_CHUNK_CHARS = 1200                     # hard upper bound for a chunk
CHUNK_OVERLAP = 120                        # characters shared by neighbours
SNIPPET_CHARS = 300                        # search snippet budget
TOP_K_MAX = 50                             # top_k clamp: 1..50
TOP_K_DEFAULT = 8
ASK_TOP_K_DEFAULT = 5
MAX_CHARS_MIN = 500                        # ask budget clamp: 500..30000
MAX_CHARS_MAX = 30000
MAX_CHARS_DEFAULT = 6000
RRF_K = 60                                 # reciprocal rank fusion constant

# --- retrieval constants --------------------------------------------------
BM25_K1 = 1.2
BM25_B = 0.75
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
DENSE_MODEL = os.environ.get("KB_LITE_MODEL") or "BAAI/bge-small-zh-v1.5"

# --- on-disk layout -------------------------------------------------------
MANIFEST_NAME = "manifest.json"
CHUNKS_NAME = "chunks.jsonl"
VECTORS_NPY = "vectors.npy"
VECTORS_JSON = "vectors.json"
LOG_NAME = "engine.log"

SUPPORTED_EXTS = (".md", ".markdown", ".txt", ".pdf")
MARKDOWN_EXTS = (".md", ".markdown")

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
}


# --------------------------------------------------------------------------
# errors and logging
# --------------------------------------------------------------------------
class KBError(Exception):
    """An error that maps onto the protocol's error object."""

    def __init__(self, code: str, message: str) -> None:
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def _stderr(level: str, msg: str, **extra: Any) -> None:
    """Write a diagnostic line to stderr; never raise."""
    try:
        line = "[kb-lite %s] %s" % (level, msg)
        if extra:
            try:
                line += " " + json.dumps(extra, ensure_ascii=False, default=str)
            except Exception:
                pass
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def log_event(root: Optional[str], level: str, msg: str, **extra: Any) -> None:
    """Mirror a log line to stderr and to ``<kb_root>/engine.log``.

    Logging must never break a request, so every failure is swallowed.
    """
    _stderr(level, msg, **extra)
    if not root:
        return
    try:
        path = os.path.join(os.path.abspath(root), LOG_NAME)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        suffix = ""
        if extra:
            try:
                suffix = " " + json.dumps(extra, ensure_ascii=False, default=str)
            except Exception:
                suffix = ""
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("%s %s %s%s\n" % (stamp, level.upper(), msg, suffix))
    except Exception:
        pass


# --------------------------------------------------------------------------
# small helpers: time, modules, paths, ids, io
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _module_present(name: str) -> bool:
    """Cheap availability probe: locate a module without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _display_path(path: str) -> str:
    """Absolute path with forward slashes (stable across the wire)."""
    try:
        return os.path.abspath(path).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def _norm_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/")


def _doc_id_for(path: str) -> str:
    return hashlib.sha1(_norm_key(path).encode("utf-8")).hexdigest()[:16]


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _real_root(root: str) -> str:
    return os.path.realpath(os.path.abspath(root))


def _child(root: str, name: str) -> str:
    """Join a fixed storage file name onto kb_root, refusing escapes."""
    real_root = _real_root(root)
    path = os.path.join(real_root, name)
    parent = os.path.dirname(os.path.realpath(path))
    if parent != real_root:
        raise KBError("path_escape", "refusing to write outside kb_root: %r" % (name,))
    return path


def _atomic_write(path: str, data: bytes) -> None:
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _read_text_file(path: str) -> str:
    """Read a text file, tolerating UTF-8 (with/without BOM) and GB18030.

    CRLF/CR line endings are normalised to LF so chunk text and snippets are
    clean; line numbers are unaffected (one break stays one line).
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _mtime_same(stored: Any, current: float) -> bool:
    try:
        return abs(float(stored) - float(current)) <= 1e-5
    except (TypeError, ValueError):
        return False


def _get_bool(params: Dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    if isinstance(value, (int, float)):
        return bool(value)
    raise KBError("bad_request", "params.%s must be a boolean" % key)


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    if value is None:
        number = default
    elif isinstance(value, bool):
        raise KBError("bad_request", "expected an integer, got a boolean")
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except (TypeError, ValueError):
            raise KBError("bad_request", "expected an integer, got %r" % (value,))
    else:
        raise KBError("bad_request", "expected an integer, got %r" % (value,))
    return max(low, min(high, number))


def _require_query(params: Dict[str, Any]) -> str:
    query = params.get("query")
    if not isinstance(query, str) or not query.strip():
        raise KBError("bad_request", "params.query must be a non-empty string")
    return query


def _kb_root(params: Dict[str, Any]) -> str:
    root = params.get("kb_root")
    if not isinstance(root, str) or not root.strip():
        raise KBError("bad_request", "params.kb_root must be a non-empty string")
    path = os.path.abspath(os.path.expanduser(root.strip()))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise KBError("kb_root_error", "cannot create kb_root: %s" % exc)
    if not os.path.isdir(path):
        raise KBError("kb_root_error", "kb_root is not a directory: %s" % path)
    return path


def _walk_files(root: str, recursive: bool) -> Iterable[str]:
    """Yield supported files under *root* deterministically."""
    if recursive:
        visited = set()
        for dirpath, dirnames, filenames in os.walk(root):
            real_dir = os.path.realpath(dirpath)
            if real_dir in visited:
                dirnames[:] = []
                continue
            visited.add(real_dir)
            dirnames[:] = sorted(
                name for name in dirnames
                if not name.startswith(".") and name not in _SKIP_DIRS
            )
            for name in sorted(filenames):
                if _ext(name) in SUPPORTED_EXTS:
                    yield os.path.join(dirpath, name)
    else:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            return
        for name in entries:
            candidate = os.path.join(root, name)
            if os.path.isfile(candidate) and _ext(candidate) in SUPPORTED_EXTS:
                yield candidate


def _wants_embeddings(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "never", "none")
    return bool(value)


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------
def _line_numbers(text: str) -> List[int]:
    return [index for index, char in enumerate(text) if char == "\n"]


def _offset_line(newlines: Sequence[int], offset: int) -> int:
    return bisect.bisect_left(newlines, offset)


# Ordered by preference: paragraph breaks first, then sentence/clause ends,
# then any whitespace, then a hard character cut.
_BREAK_GROUPS: Tuple[Tuple[Tuple[str, ...], bool], ...] = (
    (("\n\n",), False),
    (("。", "！", "？", "；", "\n", ". ", "! ", "? ", "; "), True),
    (("，", ", ", " ", "\t"), True),
)


def _choose_break(text: str, start: int, hard_end: int) -> int:
    """Pick the nicest break point in the second half of [start, hard_end)."""
    low = start + max(1, (hard_end - start) // 3)
    for seps, cut_after in _BREAK_GROUPS:
        best_index = -1
        best_len = 0
        for sep in seps:
            index = text.rfind(sep, low, hard_end)
            if index > best_index:
                best_index = index
                best_len = len(sep)
        if best_index >= 0:
            end = best_index + (best_len if cut_after else 0)
            if end > start:
                return min(end, hard_end)
    return hard_end


def split_offsets(text: str, max_chars: int = MAX_CHUNK_CHARS,
                  overlap: int = CHUNK_OVERLAP) -> List[Tuple[int, int]]:
    """Split ``text`` into overlapping ``(start, end)`` spans, each <= max_chars."""
    total = len(text)
    if total <= 0:
        return []
    if total <= max_chars:
        return [(0, total)]
    spans: List[Tuple[int, int]] = []
    start = 0
    while start < total:
        hard_end = min(start + max_chars, total)
        if hard_end >= total:
            spans.append((start, total))
            break
        end = _choose_break(text, start, hard_end)
        if end <= start:
            end = hard_end
        spans.append((start, end))
        nxt = end - overlap
        if nxt <= start:
            nxt = end
        start = nxt
    return spans


def _span_chunk(text: str, newlines: Sequence[int], base_line: int,
                start: int, end: int) -> Optional[Tuple[str, int, int]]:
    """Materialise a span as (text, start_line, end_line) with whitespace stripped."""
    raw = text[start:end]
    leading = len(raw) - len(raw.lstrip())
    inner_start = start + leading
    inner_end = start + len(raw.rstrip())
    if inner_end <= inner_start:
        return None
    body = text[inner_start:inner_end]
    if not body:
        return None
    start_line = base_line + _offset_line(newlines, inner_start)
    end_line = base_line + _offset_line(newlines, inner_end - 1)
    return body, start_line, end_line


_ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)$")
_TRAILING_HASHES_RE = re.compile(r"[ \t]+#+[ \t]*$")


def _parse_atx(line: str) -> Optional[Tuple[int, str]]:
    match = _ATX_RE.match(line)
    if not match:
        return None
    level = len(match.group(1))
    body = _TRAILING_HASHES_RE.sub("", match.group(2)).strip()
    if not body:
        return None
    return level, body


def _new_section(heading: Optional[str], level: int, lineno: int) -> Dict[str, Any]:
    return {"heading": heading, "level": level, "start": lineno, "lines": []}


def _markdown_sections(text: str) -> List[Dict[str, Any]]:
    """Split Markdown into sections at ATX headings (fenced code respected)."""
    lines = text.split("\n")
    sections: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    fence: Optional[str] = None

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            if current is None:
                current = _new_section(None, 0, lineno)
            current["lines"].append((lineno, line))
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = "```" if stripped.startswith("```") else "~~~"
            if current is None:
                current = _new_section(None, 0, lineno)
            current["lines"].append((lineno, line))
            continue
        atx = _parse_atx(line)
        if atx is not None:
            if current is not None:
                sections.append(current)
            level, body = atx
            current = _new_section("#" * level + " " + body, level, lineno)
            current["lines"].append((lineno, line))
        else:
            if current is None:
                current = _new_section(None, 0, lineno)
            current["lines"].append((lineno, line))

    if current is not None:
        sections.append(current)
    return sections


def _heading_text(heading: Optional[str]) -> str:
    if not heading:
        return ""
    return heading.lstrip("#").strip()


def chunk_markdown(text: str,
                   fallback_title: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """Chunk Markdown by ATX heading, splitting oversized sections at 1200 chars."""
    sections = _markdown_sections(text or "")
    title = None
    for section in sections:
        if section["heading"] and section["level"] == 1:
            title = _heading_text(section["heading"])
            break
    if not title:
        for section in sections:
            if section["heading"]:
                title = _heading_text(section["heading"])
                break
    if not title:
        title = fallback_title or "Untitled"

    chunks: List[Dict[str, Any]] = []
    for section in sections:
        body = "\n".join(line for _, line in section["lines"])
        if not body.strip():
            continue
        heading = section["heading"] or title
        newlines = _line_numbers(body)
        for start, end in split_offsets(body):
            piece = _span_chunk(body, newlines, section["start"], start, end)
            if piece is None:
                continue
            piece_text, start_line, end_line = piece
            chunks.append({
                "heading": heading,
                "start_line": start_line,
                "end_line": end_line,
                "page": None,
                "text": piece_text,
            })
    return title, chunks


def chunk_text_plain(text: str,
                     fallback_title: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """Chunk plain text: paragraphs merged greedily to <=1200 chars, 120 overlap."""
    body = text or ""
    title = None
    for line in body.split("\n"):
        if line.strip():
            title = line.strip()[:120]
            break
    if not title:
        title = fallback_title or "Untitled"

    chunks: List[Dict[str, Any]] = []
    if body.strip():
        newlines = _line_numbers(body)
        for start, end in split_offsets(body):
            piece = _span_chunk(body, newlines, 1, start, end)
            if piece is None:
                continue
            piece_text, start_line, end_line = piece
            chunks.append({
                "heading": title,
                "start_line": start_line,
                "end_line": end_line,
                "page": None,
                "text": piece_text,
            })
    return title, chunks


def chunk_pdf(path: str) -> Tuple[str, Optional[int], List[Dict[str, Any]]]:
    """Chunk a PDF page by page; 1-based ``page`` numbers."""
    try:
        import fitz  # type: ignore  # PyMuPDF
    except Exception:
        raise KBError("pdf_unavailable",
                      "pymupdf is not installed; PDF ingestion is unavailable")
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise KBError("pdf_open_failed", "cannot open PDF: %s" % exc)

    title = _stem(path)
    try:
        try:
            metadata = document.metadata or {}
        except Exception:
            metadata = {}
        if isinstance(metadata, dict):
            candidate = metadata.get("title")
            if candidate and str(candidate).strip():
                title = str(candidate).strip()
        pages = int(getattr(document, "page_count", 0) or 0)
        chunks: List[Dict[str, Any]] = []
        for index in range(pages):
            try:
                page = document.load_page(index)
                page_text = page.get_text("text") or ""
            except Exception as exc:
                log_event(None, "warn", "pdf page %d unreadable: %s" % (index + 1, exc))
                continue
            if not page_text.strip():
                continue
            newlines = _line_numbers(page_text)
            for start, end in split_offsets(page_text):
                piece = _span_chunk(page_text, newlines, 1, start, end)
                if piece is None:
                    continue
                piece_text, start_line, end_line = piece
                chunks.append({
                    "heading": title,
                    "start_line": start_line,
                    "end_line": end_line,
                    "page": index + 1,
                    "text": piece_text,
                })
        return title, pages, chunks
    finally:
        try:
            document.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# tokenisation + BM25 (pure standard library)
# --------------------------------------------------------------------------
_LATIN_RE = re.compile(r"[a-z0-9_]+")
_HAN_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                         r"\U00020000-\U0002ebef]+")


def tokenize(text: str) -> List[str]:
    """Lowercase latin words + CJK bigrams + single han characters as fallback."""
    if not text:
        return []
    lowered = text.lower()
    tokens = _LATIN_RE.findall(lowered)
    for run in _HAN_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
        tokens.extend(run)  # single-character fallback
    return tokens


class BM25Index:
    """Okapi BM25 (k1=1.2, b=0.75) over pre-tokenised documents."""

    def __init__(self, documents: Sequence[Sequence[str]]) -> None:
        self.k1 = BM25_K1
        self.b = BM25_B
        self.n = len(documents)
        self.doc_tf = [Counter(document) for document in documents]
        self.doc_len = [sum(counts.values()) for counts in self.doc_tf]
        self.avgdl = (sum(self.doc_len) / float(self.n)) if self.n else 0.0
        self.postings: Dict[str, List[int]] = {}
        for index, counts in enumerate(self.doc_tf):
            for term in counts:
                self.postings.setdefault(term, []).append(index)
        self._idf: Dict[str, float] = {}

    def idf(self, term: str) -> float:
        cached = self._idf.get(term)
        if cached is not None:
            return cached
        df = len(self.postings.get(term, ()))
        if not self.n:
            value = 0.0
        else:
            value = math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))
        self._idf[term] = value
        return value

    def scores(self, query_tokens: Sequence[str]) -> List[float]:
        scores = [0.0] * self.n
        if not self.n or not query_tokens:
            return scores
        query_tf = Counter(query_tokens)
        for term, query_count in query_tf.items():
            docs = self.postings.get(term)
            if not docs:
                continue
            idf = self.idf(term)
            if idf <= 0.0:
                continue
            for index in docs:
                tf = self.doc_tf[index].get(term, 0)
                if not tf:
                    continue
                doc_len = self.doc_len[index] or 1
                denominator = tf + self.k1 * (
                    1.0 - self.b + self.b * doc_len / (self.avgdl or 1.0)
                )
                if denominator <= 0.0:
                    continue
                scores[index] += idf * (tf * (self.k1 + 1.0) / denominator) * query_count
        return scores


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if len(collapsed) <= limit:
        return collapsed
    if limit <= 1:
        return collapsed[:limit]
    return collapsed[:limit - 1] + "\u2026"


# --------------------------------------------------------------------------
# optional dense backend
# --------------------------------------------------------------------------
class DenseBackend:
    """Lazy sentence-transformers backend; ``capable`` is an import probe only."""

    backend_kind = "sentence-transformers"

    def __init__(self, model_name: Optional[str] = None) -> None:
        self.model_name = model_name or os.environ.get("KB_LITE_MODEL") or DENSE_MODEL
        self._model = None
        self._numpy = None
        self._load_attempted = False
        self._load_error: Optional[str] = None

    @property
    def capable(self) -> bool:
        return _module_present("numpy") and _module_present("sentence_transformers")

    def ensure_loaded(self) -> bool:
        if self._model is not None:
            return True
        if self._load_attempted:
            return False
        self._load_attempted = True
        if not self.capable:
            self._load_error = "numpy and sentence-transformers are required"
            return False
        try:
            import numpy as np  # noqa: F401  (imported for its side effect)
            from sentence_transformers import SentenceTransformer  # type: ignore
        except Exception as exc:
            self._load_error = "%s: %s" % (type(exc).__name__, exc)
            _stderr("warn", "dense backend unavailable: %s" % self._load_error)
            return False
        try:
            self._model = SentenceTransformer(self.model_name)
            self._numpy = np
            return True
        except Exception as exc:
            self._load_error = "%s: %s" % (type(exc).__name__, exc)
            _stderr("warn", "cannot load embedding model %r: %s"
                    % (self.model_name, self._load_error))
            return False

    def encode(self, texts: Sequence[str], is_query: bool = False) -> List[List[float]]:
        del is_query  # the caller applies the bge query prefix
        if not self.ensure_loaded() or self._model is None:
            raise KBError("dense_unavailable",
                          "dense backend unavailable: %s"
                          % (self._load_error or "unknown error"))
        payload = list(texts)
        if not payload:
            return []
        try:
            vectors = self._model.encode(
                payload, normalize_embeddings=True, convert_to_numpy=True,
                show_progress_bar=False,
            )
        except TypeError:
            vectors = self._model.encode(
                payload, convert_to_numpy=True, show_progress_bar=False,
            )
            vectors = self._normalise(vectors)
        return [[float(value) for value in row] for row in vectors]

    def _normalise(self, vectors: Any) -> Any:
        try:
            import numpy as np
            array = np.asarray(vectors, dtype="float32")
            if array.ndim == 1:
                array = array.reshape(1, -1)
            norms = np.linalg.norm(array, axis=1, keepdims=True)
            norms[norms == 0.0] = 1.0
            return array / norms
        except Exception:
            return vectors


class FakeHashBackend:
    """Deterministic hashed bag-of-words embedder, enabled with KB_LITE_EMBED=fake.

    It exists so the dense/hybrid pipeline and vector persistence can be
    tested end to end without numpy, network access or a real model.  Tokens
    come from the same tokenizer BM25 uses, each token is hashed into
    ``[0, dim)`` and the resulting vector is L2-normalised.
    """

    backend_kind = "fake"

    def __init__(self, dim: int = 64) -> None:
        try:
            dim = int(dim)
        except (TypeError, ValueError):
            dim = 64
        self.dim = max(1, min(4096, dim))
        self.model_name = "fake-hash-%d" % self.dim

    @property
    def capable(self) -> bool:
        return True

    def ensure_loaded(self) -> bool:
        return True

    def encode(self, texts: Sequence[str], is_query: bool = False) -> List[List[float]]:
        vectors: List[List[float]] = []
        for raw in texts:
            text = raw or ""
            if is_query and text.startswith(BGE_QUERY_PREFIX):
                text = text[len(BGE_QUERY_PREFIX):]
            vector = [0.0] * self.dim
            for token in tokenize(text):
                digest = hashlib.sha1(token.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:8], "big") % self.dim] += 1.0
            norm = math.sqrt(sum(value * value for value in vector))
            if norm > 0.0:
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return vectors


def _dense_disabled() -> bool:
    value = (os.environ.get("KB_LITE_DENSE") or "").strip().lower()
    return value in ("0", "false", "no", "off", "none", "disabled")


def _fake_dim() -> int:
    raw = os.environ.get("KB_LITE_EMBED_DIM")
    if raw:
        try:
            return max(1, min(4096, int(str(raw).strip())))
        except (TypeError, ValueError):
            pass
    return 64


def make_dense_backend() -> Optional[Any]:
    """Pick the embedding backend from the environment.

    ``KB_LITE_DENSE=0`` disables dense retrieval entirely.  ``KB_LITE_EMBED``
    may be ``fake`` (deterministic test embedder), ``none``, or left at its
    default / ``auto`` / ``sentence-transformers`` for the real backend.
    """
    if _dense_disabled():
        return None
    choice = (os.environ.get("KB_LITE_EMBED") or "").strip().lower()
    if choice in ("fake", "hash", "hashed", "stub"):
        return FakeHashBackend(_fake_dim())
    if choice in ("none", "off", "disabled"):
        return None
    backend = DenseBackend()
    return backend if backend.capable else None


def _dense_dot(matrix: Any, query_vector: Sequence[float]) -> Optional[List[float]]:
    """Cosine scores (vectors are normalised) or None when dimensions mismatch."""
    if matrix is None or query_vector is None:
        return None
    if len(matrix) != 0:
        try:
            first = matrix[0]
            if len(first) != len(query_vector):
                return None
        except Exception:
            return None
    try:
        import numpy as np
        array = np.asarray(matrix, dtype="float32")
        query = np.asarray(list(query_vector), dtype="float32")
        if array.ndim != 2 or array.shape[1] != query.shape[0]:
            return None
        return [float(value) for value in array.dot(query)]
    except Exception:
        pass
    scores: List[float] = []
    for row in matrix:
        try:
            if len(row) != len(query_vector):
                return None
            scores.append(float(sum(float(a) * float(b)
                                    for a, b in zip(row, query_vector))))
        except Exception:
            return None
    return scores


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
class KBEngine:
    """Stateless-with-respect-to-roots request handler.

    Every operation reloads its root's manifest/chunks, so one process can
    safely serve several ``kb_root`` values without caching staleness.
    """

    def __init__(self, dense_backend: Optional[Any] = None) -> None:
        if dense_backend is None:
            dense_backend = make_dense_backend()
        self.dense = dense_backend

    # -- backend reporting -------------------------------------------------
    def _backend_report(self) -> Tuple[str, Optional[str], bool]:
        """Return (backend_name, dense_model, dense_available)."""
        backend = self.dense
        if backend is None:
            return "none", None, False
        capable = bool(getattr(backend, "capable", False))
        if not capable:
            return "none", None, False
        kind = getattr(backend, "backend_kind", None)
        if not kind:
            kind = "sentence-transformers" if isinstance(backend, DenseBackend) else "fake"
        return kind, getattr(backend, "model_name", None), True

    # -- protocol plumbing -------------------------------------------------
    def ready_event(self) -> Dict[str, Any]:
        backend, _model, dense = self._backend_report()
        return {
            "event": "ready",
            "version": ENGINE_VERSION,
            "dense": dense,
            "numpy": _module_present("numpy"),
            "backend": backend,
        }

    def handle_request(self, request: Any) -> Dict[str, Any]:
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict):
                raise KBError("bad_request", "request must be a JSON object")
            op = request.get("op")
            if not isinstance(op, str) or not op:
                raise KBError("bad_request", "request.op must be a non-empty string")
            params = request.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise KBError("bad_request", "request.params must be an object")
            handler = {
                "ping": self.op_ping,
                "stats": self.op_stats,
                "ingest": self.op_ingest,
                "search": self.op_search,
                "ask": self.op_ask,
                "clear": self.op_clear,
            }.get(op)
            if handler is None:
                raise KBError("unknown_op", "unknown op: %s" % op)
            result = handler(params)
            return {"id": request_id, "ok": True, "result": result}
        except KBError as exc:
            return {"id": request_id, "ok": False,
                    "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            _stderr("error", "internal_error: %s\n%s"
                    % (exc, traceback.format_exc()))
            return {"id": request_id, "ok": False,
                    "error": {"code": "internal_error",
                              "message": "%s: %s" % (type(exc).__name__, exc)}}

    # -- storage -----------------------------------------------------------
    def _load_manifest(self, root: str) -> Dict[str, Any]:
        path = _child(root, MANIFEST_NAME)
        empty = {"version": MANIFEST_VERSION, "docs": {}, "updated_at": None}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return empty
        except (OSError, ValueError) as exc:
            log_event(root, "warn", "manifest unreadable, starting empty: %s" % exc)
            return empty
        if not isinstance(data, dict):
            log_event(root, "warn", "manifest is not an object, starting empty")
            return empty
        docs = data.get("docs")
        if not isinstance(docs, dict):
            docs = {}
        clean: Dict[str, Any] = {}
        for doc_id, entry in docs.items():
            if isinstance(doc_id, str) and isinstance(entry, dict):
                clean[doc_id] = entry
        return {
            "version": MANIFEST_VERSION,
            "docs": clean,
            "updated_at": data.get("updated_at"),
        }

    def _save_manifest(self, root: str, manifest: Dict[str, Any]) -> None:
        path = _child(root, MANIFEST_NAME)
        payload = json.dumps(manifest, ensure_ascii=False, indent=2)
        _atomic_write(path, (payload + "\n").encode("utf-8"))

    def _load_chunks(self, root: str) -> List[Dict[str, Any]]:
        path = _child(root, CHUNKS_NAME)
        chunks: List[Dict[str, Any]] = []
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return chunks
        except OSError as exc:
            log_event(root, "warn", "chunks.jsonl unreadable: %s" % exc)
            return chunks
        with handle:
            for lineno, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    log_event(root, "warn", "chunks.jsonl line %d is not JSON" % lineno)
                    continue
                if not isinstance(record, dict):
                    continue
                chunk_id = record.get("chunk_id")
                doc_id = record.get("doc_id")
                text = record.get("text")
                if not isinstance(chunk_id, str) or not isinstance(doc_id, str) \
                        or not isinstance(text, str):
                    continue
                try:
                    ordinal = int(record.get("ordinal") or 0)
                except (TypeError, ValueError):
                    ordinal = 0
                chunks.append({
                    "chunk_id": chunk_id,
                    "doc_id": doc_id,
                    "ordinal": ordinal,
                    "heading": record.get("heading"),
                    "start_line": record.get("start_line"),
                    "end_line": record.get("end_line"),
                    "page": record.get("page"),
                    "text": text,
                })
        return chunks

    def _save_chunks(self, root: str, chunks: Sequence[Dict[str, Any]]) -> None:
        path = _child(root, CHUNKS_NAME)
        lines = []
        for chunk in chunks:
            lines.append(json.dumps({
                "chunk_id": chunk.get("chunk_id"),
                "doc_id": chunk.get("doc_id"),
                "ordinal": chunk.get("ordinal"),
                "heading": chunk.get("heading"),
                "start_line": chunk.get("start_line"),
                "end_line": chunk.get("end_line"),
                "page": chunk.get("page"),
                "text": chunk.get("text", ""),
            }, ensure_ascii=False))
        payload = "\n".join(lines)
        if payload:
            payload += "\n"
        _atomic_write(path, payload.encode("utf-8"))

    def _load_vectors(self, root: str) -> Optional[Any]:
        npy_path = _child(root, VECTORS_NPY)
        json_path = _child(root, VECTORS_JSON)
        if os.path.isfile(npy_path):
            try:
                import numpy as np
                array = np.load(npy_path, allow_pickle=False)
                if getattr(array, "ndim", 0) == 2:
                    return array.astype("float32")
                log_event(root, "warn", "vectors.npy has an unexpected shape")
                return None
            except Exception as exc:
                log_event(root, "warn", "cannot load vectors.npy: %s" % exc)
                return None
        if os.path.isfile(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError) as exc:
                log_event(root, "warn", "cannot load vectors.json: %s" % exc)
                return None
            if isinstance(data, list) and (not data or isinstance(data[0], list)):
                return data
            log_event(root, "warn", "vectors.json has an unexpected shape")
        return None

    def _write_vectors(self, root: str, vectors: Sequence[Sequence[float]]) -> None:
        npy_path = _child(root, VECTORS_NPY)
        json_path = _child(root, VECTORS_JSON)
        try:
            import numpy as np
        except Exception:
            np = None  # type: ignore[assignment]
        if np is not None:
            array = np.asarray(vectors, dtype="float32")
            tmp = "%s.tmp.%d" % (npy_path, os.getpid())
            try:
                with open(tmp, "wb") as handle:
                    np.save(handle, array, allow_pickle=False)
                os.replace(tmp, npy_path)
            finally:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
            try:
                if os.path.isfile(json_path):
                    os.remove(json_path)
            except OSError:
                pass
            return
        tmp = "%s.tmp.%d" % (json_path, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump([[float(value) for value in row] for row in vectors], handle)
            os.replace(tmp, json_path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        try:
            if os.path.isfile(npy_path):
                os.remove(npy_path)
        except OSError:
            pass

    @staticmethod
    def _vector_key(chunk: Dict[str, Any]) -> str:
        text = chunk.get("text") or ""
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        return "%s:%s:%s" % (chunk.get("doc_id"), chunk.get("ordinal"), digest)

    def _sync_vectors(self, root: str, old_chunks: Sequence[Dict[str, Any]],
                      new_chunks: Sequence[Dict[str, Any]],
                      with_embeddings: Any) -> None:
        """Keep vectors.npy / vectors.json row-aligned with chunks.jsonl."""
        npy_path = _child(root, VECTORS_NPY)
        json_path = _child(root, VECTORS_JSON)

        def drop_vectors() -> None:
            for path in (npy_path, json_path):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass

        cache: Dict[str, List[float]] = {}
        if old_chunks and new_chunks:
            old_vectors = self._load_vectors(root)
            if old_vectors is not None and len(old_vectors) == len(old_chunks):
                for chunk, vector in zip(old_chunks, old_vectors):
                    try:
                        cache[self._vector_key(chunk)] = [float(value) for value in vector]
                    except (TypeError, ValueError):
                        continue

        prepared: List[Optional[List[float]]] = []
        missing: List[int] = []
        for index, chunk in enumerate(new_chunks):
            vector = cache.get(self._vector_key(chunk))
            prepared.append(vector)
            if vector is None:
                missing.append(index)

        want = _wants_embeddings(with_embeddings)
        capable = bool(self.dense is not None and getattr(self.dense, "capable", False))
        if missing and want and capable:
            try:
                texts = [new_chunks[index]["text"] for index in missing]
                produced = self.dense.encode(texts, is_query=False)
                if len(produced) != len(texts):
                    raise KBError("dense_error", "embedding count mismatch")
                for index, vector in zip(missing, produced):
                    row = [float(value) for value in vector]
                    if not row:
                        raise KBError("dense_error", "empty embedding returned")
                    prepared[index] = row
                missing = []
            except Exception as exc:
                log_event(root, "warn", "embedding failed, dropping vectors: %s" % exc)
                missing = [index for index, vector in enumerate(prepared)
                           if vector is None]

        if not new_chunks:
            drop_vectors()
            return
        if missing or any(vector is None for vector in prepared):
            drop_vectors()
            return
        try:
            self._write_vectors(root, [vector for vector in prepared if vector is not None])
        except Exception as exc:
            log_event(root, "warn", "cannot write vectors: %s" % exc)
            drop_vectors()

    # -- ops ---------------------------------------------------------------
    def op_ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        del params
        _backend, _model, dense = self._backend_report()
        return {
            "pong": True,
            "version": ENGINE_VERSION,
            "dense": dense,
            "numpy": _module_present("numpy"),
        }

    def op_stats(self, params: Dict[str, Any]) -> Dict[str, Any]:
        root = _kb_root(params)
        manifest = self._load_manifest(root)
        chunks = self._load_chunks(root)
        docs = manifest.get("docs") or {}
        total_bytes = 0
        for entry in docs.values():
            try:
                total_bytes += int(entry.get("size") or 0)
            except (TypeError, ValueError):
                continue
        if total_bytes == 0 and chunks:
            total_bytes = sum(len((chunk.get("text") or "").encode("utf-8"))
                              for chunk in chunks)
        paths = sorted(
            entry.get("path") for entry in docs.values()
            if isinstance(entry, dict) and entry.get("path")
        )
        backend, dense_model, capable = self._backend_report()
        return {
            "docs": len(docs),
            "chunks": len(chunks),
            "dense": capable,
            "dense_model": dense_model,
            "backend": backend,
            "kb_root": _display_path(root),
            "bytes": total_bytes,
            "updated_at": manifest.get("updated_at"),
            "paths": paths[:50],
        }

    # -- ingest ------------------------------------------------------------
    def _parse_file(self, path: str, kind: str) -> Dict[str, Any]:
        if kind == "pdf":
            title, pages, chunks = chunk_pdf(path)
        elif kind in ("md", "markdown"):
            title, chunks = chunk_markdown(_read_text_file(path), _stem(path))
            pages = None
        elif kind == "txt":
            title, chunks = chunk_text_plain(_read_text_file(path), _stem(path))
            pages = None
        else:
            raise KBError("unsupported_extension",
                          "unsupported file extension: .%s" % kind)
        return {"title": title, "kind": kind, "pages": pages, "chunks": chunks}

    def op_ingest(self, params: Dict[str, Any]) -> Dict[str, Any]:
        started = time.time()
        root = _kb_root(params)
        raw_paths = params.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise KBError("bad_request",
                          "params.paths must be a non-empty array of strings")
        for item in raw_paths:
            if not isinstance(item, str):
                raise KBError("bad_request", "params.paths must contain only strings")
        recursive = _get_bool(params, "recursive", True)
        force = _get_bool(params, "force", False)
        max_files = _clamp_int(params.get("max_files"), 50, 1, 100000)
        with_embeddings = params.get("with_embeddings", "auto")

        manifest = self._load_manifest(root)
        docs_meta: Dict[str, Any] = manifest["docs"]
        old_chunks = self._load_chunks(root)
        by_doc: Dict[str, List[Dict[str, Any]]] = {}
        for chunk in old_chunks:
            by_doc.setdefault(chunk["doc_id"], []).append(chunk)

        errors: List[Dict[str, Any]] = []
        candidates: List[str] = []
        seen = set()
        for raw in raw_paths:
            path = os.path.abspath(os.path.expanduser(raw))
            if os.path.isdir(path):
                for found in _walk_files(path, recursive):
                    key = os.path.normcase(os.path.abspath(found))
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(found)
            elif os.path.isfile(path):
                if _ext(path) not in SUPPORTED_EXTS:
                    errors.append({
                        "path": _display_path(path),
                        "code": "unsupported_extension",
                        "message": "unsupported file type: %s" % (_ext(path) or "(none)"),
                    })
                    continue
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(path)
            else:
                errors.append({
                    "path": _display_path(path),
                    "code": "ingest_path_missing",
                    "message": "path does not exist: %s" % raw,
                })

        candidates.sort(key=lambda item: item.lower())
        pending = max(0, len(candidates) - max_files)
        batch = candidates[:max_files]

        added = 0
        updated = 0
        skipped = 0
        changed = False
        docs_out: List[Dict[str, Any]] = []

        for path in batch:
            display = _display_path(path)
            try:
                kind = _ext(path).lstrip(".")
                try:
                    stat = os.stat(path)
                except OSError as exc:
                    raise KBError("ingest_path_missing",
                                  "cannot stat file: %s" % exc)
                if stat.st_size > MAX_FILE_BYTES:
                    raise KBError("file_too_large",
                                  "file is larger than the 20 MB limit")
                digest = _sha256_file(path)
                doc_id = _doc_id_for(path)
                previous = docs_meta.get(doc_id)
                mtime = float(stat.st_mtime)
                if (previous is not None and not force
                        and previous.get("sha256") == digest
                        and int(previous.get("size") or -1) == int(stat.st_size)
                        and _mtime_same(previous.get("mtime"), mtime)):
                    skipped += 1
                    continue

                parsed = self._parse_file(path, kind)
                doc_chunks: List[Dict[str, Any]] = []
                for ordinal, chunk in enumerate(parsed["chunks"]):
                    doc_chunks.append({
                        "chunk_id": "%s:%d" % (doc_id, ordinal),
                        "doc_id": doc_id,
                        "ordinal": ordinal,
                        "heading": chunk.get("heading"),
                        "start_line": chunk.get("start_line"),
                        "end_line": chunk.get("end_line"),
                        "page": chunk.get("page"),
                        "text": chunk.get("text", ""),
                    })
                by_doc[doc_id] = doc_chunks
                docs_meta[doc_id] = {
                    "path": display,
                    "sha256": digest,
                    "mtime": round(mtime, 6),
                    "size": int(stat.st_size),
                    "title": parsed["title"],
                    "kind": kind,
                    "pages": parsed["pages"],
                    "chunks": len(doc_chunks),
                }
                if previous is not None:
                    updated += 1
                else:
                    added += 1
                changed = True
                docs_out.append({"path": display, "chunks": len(doc_chunks)})
            except KBError as exc:
                errors.append({"path": display, "code": exc.code,
                               "message": exc.message})
                log_event(root, "warn", "ingest error", path=display,
                          code=exc.code, message=exc.message)
            except Exception as exc:
                errors.append({
                    "path": display,
                    "code": "ingest_failed",
                    "message": "%s: %s" % (type(exc).__name__, exc),
                })
                log_event(root, "warn", "ingest failed", path=display,
                          message=str(exc))

        if changed:
            ordered_ids = sorted(
                by_doc.keys(),
                key=lambda doc_id: (
                    (docs_meta.get(doc_id) or {}).get("path") or "", doc_id
                ),
            )
            new_chunks: List[Dict[str, Any]] = []
            for doc_id in ordered_ids:
                new_chunks.extend(by_doc[doc_id])
            self._sync_vectors(root, old_chunks, new_chunks, with_embeddings)
            self._save_chunks(root, new_chunks)
            self._save_manifest(root, {
                "version": MANIFEST_VERSION,
                "docs": docs_meta,
                "updated_at": _now_iso(),
            })

        elapsed_ms = int((time.time() - started) * 1000)
        log_event(root, "info", "ingest finished", added=added, updated=updated,
                  skipped=skipped, errors=len(errors), pending=pending,
                  elapsed_ms=elapsed_ms)
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "pending": pending,
            "docs": docs_out,
            "elapsed_ms": elapsed_ms,
        }

    # -- retrieval ---------------------------------------------------------
    def _bm25_scores(self, chunks: Sequence[Dict[str, Any]], query: str) -> List[float]:
        index = BM25Index([tokenize(chunk.get("text") or "") for chunk in chunks])
        return index.scores(tokenize(query))

    def _retrieve(self, root: str, chunks: Sequence[Dict[str, Any]],
                  manifest: Dict[str, Any], query: str, top_k: int,
                  mode: str) -> Tuple[List[Tuple[int, float, List[str]]], str]:
        bm25_scores = self._bm25_scores(chunks, query)
        candidate_limit = max(top_k * 4, top_k)
        order = sorted(range(len(chunks)), key=lambda index: (-bm25_scores[index], index))
        bm25_rank = [index for index in order if bm25_scores[index] > 0.0][:candidate_limit]

        dense_ready = False
        if mode in ("auto", "dense", "hybrid") and self.dense is not None \
                and getattr(self.dense, "capable", False):
            try:
                dense_ready = bool(self.dense.ensure_loaded())
            except Exception as exc:
                log_event(root, "warn", "dense backend failed to load: %s" % exc)
                dense_ready = False

        if mode == "auto":
            effective = "hybrid" if dense_ready else "bm25"
        elif mode == "dense":
            effective = "dense" if dense_ready else "bm25"
        elif mode == "hybrid":
            effective = "hybrid" if dense_ready else "bm25"
        else:
            effective = "bm25"

        vector_rows = None
        if effective in ("dense", "hybrid"):
            vector_rows = self._load_vectors(root)
            if vector_rows is None or len(vector_rows) != len(chunks):
                log_event(root, "warn",
                          "dense vectors missing or out of sync; using bm25")
                effective = "bm25"
                vector_rows = None

        dense_scores: Optional[List[float]] = None
        dense_rank: List[int] = []
        if effective in ("dense", "hybrid") and vector_rows is not None:
            try:
                query_vector = self.dense.encode(
                    [BGE_QUERY_PREFIX + query], is_query=True)[0]
                dense_scores = _dense_dot(vector_rows, query_vector)
            except Exception as exc:
                log_event(root, "warn",
                          "dense query failed (%s); using bm25" % exc)
                dense_scores = None
            if dense_scores is None:
                log_event(root, "warn",
                          "dense score computation failed; using bm25")
                effective = "bm25"
            else:
                dense_order = sorted(
                    range(len(chunks)),
                    key=lambda index: (-dense_scores[index], index),
                )
                dense_rank = dense_order[:candidate_limit]

        if effective == "hybrid":
            fused: Dict[int, float] = {}
            sources: Dict[int, List[str]] = {}
            for rank_position, index in enumerate(bm25_rank):
                fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank_position + 1)
                sources.setdefault(index, []).append("bm25")
            for rank_position, index in enumerate(dense_rank):
                fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank_position + 1)
                sources.setdefault(index, []).append("dense")
            fused_order = sorted(fused.keys(), key=lambda index: (-fused[index], index))
            ranked = [(index, fused[index], sources[index])
                      for index in fused_order[:top_k]]
            return ranked, "hybrid"

        if effective == "dense" and dense_scores is not None:
            ranked = [(index, dense_scores[index], ["dense"])
                      for index in dense_rank[:top_k]]
            return ranked, "dense"

        ranked = [(index, bm25_scores[index], ["bm25"])
                  for index in bm25_rank[:top_k]]
        return ranked, "bm25"

    @staticmethod
    def _doc_info(manifest: Dict[str, Any], doc_id: str) -> Tuple[str, str]:
        entry = (manifest.get("docs") or {}).get(doc_id) or {}
        path = entry.get("path") if isinstance(entry, dict) else ""
        title = entry.get("title") if isinstance(entry, dict) else ""
        return (path or ""), (title or "")

    def _hit(self, chunk: Dict[str, Any], manifest: Dict[str, Any],
             score: float, matched_by: Sequence[str]) -> Dict[str, Any]:
        path, title = self._doc_info(manifest, chunk.get("doc_id") or "")
        return {
            "chunk_id": chunk.get("chunk_id"),
            "doc_id": chunk.get("doc_id"),
            "path": path,
            "title": title,
            "heading": chunk.get("heading"),
            "start_line": chunk.get("start_line"),
            "end_line": chunk.get("end_line"),
            "page": chunk.get("page"),
            "score": round(float(score), 6),
            "matched_by": list(matched_by),
            "snippet": _snippet(chunk.get("text") or ""),
        }

    def op_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        root = _kb_root(params)
        query = _require_query(params)
        top_k = _clamp_int(params.get("top_k"), TOP_K_DEFAULT, 1, TOP_K_MAX)
        mode = params.get("mode", "auto")
        if mode is None:
            mode = "auto"
        if not isinstance(mode, str) or mode not in ("auto", "bm25", "dense", "hybrid"):
            raise KBError("bad_request",
                          "params.mode must be one of auto, bm25, dense, hybrid")
        chunks = self._load_chunks(root)
        manifest = self._load_manifest(root)
        if not chunks:
            return {"mode_used": "bm25", "hits": []}
        ranked, mode_used = self._retrieve(root, chunks, manifest, query, top_k, mode)
        hits = [self._hit(chunks[index], manifest, score, sources)
                for index, score, sources in ranked]
        log_event(root, "info", "search", query=query[:80], mode=mode_used,
                  hits=len(hits), top_k=top_k)
        return {"mode_used": mode_used, "hits": hits}

    def op_ask(self, params: Dict[str, Any]) -> Dict[str, Any]:
        root = _kb_root(params)
        query = _require_query(params)
        top_k = _clamp_int(params.get("top_k"), ASK_TOP_K_DEFAULT, 1, TOP_K_MAX)
        budget = _clamp_int(params.get("max_chars"), MAX_CHARS_DEFAULT,
                            MAX_CHARS_MIN, MAX_CHARS_MAX)
        mode = params.get("mode", "auto")
        if mode is None:
            mode = "auto"
        if not isinstance(mode, str) or mode not in ("auto", "bm25", "dense", "hybrid"):
            raise KBError("bad_request",
                          "params.mode must be one of auto, bm25, dense, hybrid")
        chunks = self._load_chunks(root)
        manifest = self._load_manifest(root)
        if not chunks:
            return {"evidence": [], "total_chars": 0, "mode_used": "bm25"}
        ranked, mode_used = self._retrieve(root, chunks, manifest, query, top_k, mode)
        evidence: List[Dict[str, Any]] = []
        used = 0
        for index, score, _sources in ranked:
            remaining = budget - used
            if remaining <= 0:
                break
            chunk = chunks[index]
            text = chunk.get("text") or ""
            if not text:
                continue
            if len(text) > remaining:
                text = text[:remaining]
                if not text.strip():
                    continue
            path, title = self._doc_info(manifest, chunk.get("doc_id") or "")
            evidence.append({
                "path": path,
                "title": title,
                "heading": chunk.get("heading"),
                "start_line": chunk.get("start_line"),
                "end_line": chunk.get("end_line"),
                "page": chunk.get("page"),
                "score": round(float(score), 6),
                "text": text,
            })
            used += len(text)
        log_event(root, "info", "ask", query=query[:80], mode=mode_used,
                  evidence=len(evidence), total_chars=used, budget=budget)
        return {"evidence": evidence, "total_chars": used, "mode_used": mode_used}

    def op_clear(self, params: Dict[str, Any]) -> Dict[str, Any]:
        root = _kb_root(params)
        if params.get("confirm") is not True:
            raise KBError("confirm_required",
                          "clear requires params.confirm === true")
        manifest = self._load_manifest(root)
        removed_docs = len(manifest.get("docs") or {})
        for name in (MANIFEST_NAME, CHUNKS_NAME, VECTORS_NPY, VECTORS_JSON):
            path = _child(root, name)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as exc:
                log_event(root, "warn", "cannot remove %s: %s" % (name, exc))
        log_event(root, "info", "clear finished", removed_docs=removed_docs,
                  kb_root=_display_path(root))
        return {"cleared": True, "removed_docs": removed_docs}


# --------------------------------------------------------------------------
# NDJSON server loop
# --------------------------------------------------------------------------
def _write_line(stream: Any, payload: Dict[str, Any]) -> None:
    try:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                          default=str)
    except (TypeError, ValueError):
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"),
                          default=str)
    stream.write(line + "\n")
    stream.flush()


def _handle_line(engine: KBEngine, line: str) -> Dict[str, Any]:
    try:
        request = json.loads(line)
    except ValueError as exc:
        _stderr("warn", "malformed JSON request: %s" % exc)
        return {"id": None, "ok": False,
                "error": {"code": "bad_request",
                          "message": "malformed JSON request: %s" % exc}}
    try:
        return engine.handle_request(request)
    except Exception as exc:  # defensive: handle_request should never raise
        _stderr("error", "unhandled request failure: %s" % exc)
        return {"id": None, "ok": False,
                "error": {"code": "internal_error",
                          "message": "%s: %s" % (type(exc).__name__, exc)}}


def _configure_stdio() -> None:
    """Force UTF-8 on stdin/stdout/stderr and LF-only stdout newlines."""
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def serve(stdin: Any = None, stdout: Any = None,
          engine: Optional[KBEngine] = None) -> int:
    """Run the NDJSON loop until stdin closes."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    engine = engine if engine is not None else KBEngine()
    _write_line(stdout, engine.ready_event())
    while True:
        try:
            line = stdin.readline()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            _stderr("error", "cannot read stdin: %s" % exc)
            break
        if line == "":
            break
        line = line.strip()
        if not line:
            continue
        if line.startswith("\ufeff"):
            line = line.lstrip("\ufeff")
        response = _handle_line(engine, line)
        try:
            _write_line(stdout, response)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            _stderr("error", "cannot write stdout: %s" % exc)
            break
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    del argv
    _configure_stdio()
    try:
        return serve()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _stderr("error", "engine crashed: %s\n%s" % (exc, traceback.format_exc()))
        return 1


if __name__ == "__main__":
    sys.exit(main())
