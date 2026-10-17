#!/usr/bin/env python3
"""Fake kb-lite engine used by the Node test-suite as a protocol fixture.

This is NOT the real engine. It implements just enough of PROTOCOL.md to let
transport / plugin tests (or a manual smoke test) run without numpy,
pymupdf or sentence-transformers:

    {"event":"ready","version":"0.1.0","dense":false,"numpy":false}

It keeps state in memory per kb_root, reads .md/.markdown/.txt files, performs
a trivial lexical search, and never exits on a bad request. stdout is strictly
NDJSON; anything diagnostic goes to stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from typing import Any

VERSION = "0.1.0"
SUPPORTED_EXTENSIONS = (".md", ".markdown", ".txt")
CHUNK_CHARS = 1200

STATE: dict[str, dict[str, Any]] = {}


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def respond_ok(request_id: Any, result: dict[str, Any]) -> None:
    emit({"id": request_id, "ok": True, "result": result})


def respond_error(request_id: Any, code: str, message: str) -> None:
    emit({"id": request_id, "ok": False, "error": {"code": code, "message": message}})


def kb_state(kb_root: str) -> dict[str, Any]:
    if kb_root not in STATE:
        STATE[kb_root] = {"docs": {}, "chunks": [], "updated_at": None}
    return STATE[kb_root]


def doc_id_for(path: str) -> str:
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:16]


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def chunk_text(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines) and size < CHUNK_CHARS:
            size += len(lines[end]) + 1
            end += 1
        chunks.append(
            {
                "ordinal": len(chunks),
                "heading": "",
                "start_line": start + 1,
                "end_line": end,
                "page": None,
                "text": "\n".join(lines[start:end]),
            }
        )
        start = end
    return chunks


def iter_files(paths: list[str], recursive: bool) -> tuple[list[str], list[dict[str, str]]]:
    files: list[str] = []
    errors: list[dict[str, str]] = []
    for raw_path in paths:
        path = os.path.abspath(os.path.expanduser(raw_path))
        if not os.path.exists(path):
            errors.append({"path": path, "code": "path_missing", "message": "path does not exist"})
            continue
        if os.path.isfile(path):
            if path.lower().endswith(SUPPORTED_EXTENSIONS):
                files.append(path)
            else:
                errors.append({"path": path, "code": "unsupported_type", "message": "unsupported file type"})
            continue
        if os.path.isdir(path):
            if recursive:
                for directory, _subdirs, names in os.walk(path):
                    for name in sorted(names):
                        candidate = os.path.join(directory, name)
                        if candidate.lower().endswith(SUPPORTED_EXTENSIONS):
                            files.append(candidate)
            else:
                for name in sorted(os.listdir(path)):
                    candidate = os.path.join(path, name)
                    if os.path.isfile(candidate) and candidate.lower().endswith(SUPPORTED_EXTENSIONS):
                        files.append(candidate)
            continue
        errors.append({"path": path, "code": "unsupported_path", "message": "not a file or directory"})
    return files, errors


def stats(kb_root: str) -> dict[str, Any]:
    state = kb_state(kb_root)
    paths = [doc["path"] for doc in state["docs"].values()]
    files = []
    total_bytes = 0
    for path in paths:
        try:
            total_bytes += os.path.getsize(path)
        except OSError:
            pass
    root = os.path.abspath(kb_root)
    if os.path.isdir(root):
        for directory, _subdirs, names in os.walk(root):
            for name in names:
                files.append(os.path.join(directory, name))
    return {
        "docs": len(state["docs"]),
        "chunks": len(state["chunks"]),
        "dense": False,
        "dense_model": None,
        "kb_root": kb_root,
        "bytes": total_bytes,
        "updated_at": state["updated_at"],
        "paths": paths[:50],
        "files": len(files),
    }


def ingest(params: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    kb_root = params["kb_root"]
    paths = params["paths"]
    recursive = params.get("recursive", True)
    force = bool(params.get("force", False))
    max_files = int(params.get("max_files", 50) or 50)
    state = kb_state(kb_root)

    all_files, errors = iter_files(paths, recursive)
    unique_files = []
    seen = set()
    for path in all_files:
        if path not in seen:
            seen.add(path)
            unique_files.append(path)

    pending = max(0, len(unique_files) - max_files)
    selected = unique_files[:max_files]

    added = updated = skipped = 0
    docs = []
    for path in selected:
        try:
            text = read_text(path)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            doc_id = doc_id_for(path)
            previous = state["docs"].get(doc_id)
            if previous and previous.get("sha256") == digest and not force:
                skipped += 1
                docs.append({"path": path, "chunks": previous.get("chunks", 0)})
                continue
            chunks = chunk_text(text)
            state["chunks"] = [c for c in state["chunks"] if c["doc_id"] != doc_id]
            for chunk in chunks:
                chunk.update({"chunk_id": f"{doc_id}:{chunk['ordinal']}", "doc_id": doc_id, "path": path})
                state["chunks"].append(chunk)
            state["docs"][doc_id] = {
                "path": path,
                "sha256": digest,
                "title": os.path.splitext(os.path.basename(path))[0],
                "chunks": len(chunks),
            }
            if previous:
                updated += 1
            else:
                added += 1
            docs.append({"path": path, "chunks": len(chunks)})
        except Exception as exc:  # per-file errors never fail the whole ingest
            errors.append({"path": path, "code": "read_failed", "message": str(exc)})

    state["updated_at"] = time.time()
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "pending": pending,
        "docs": docs,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def score_chunk(query: str, text: str) -> float:
    lowered = text.lower()
    terms = [term for term in query.lower().split() if term]
    if not terms:
        return 0.0
    score = 0.0
    for term in terms:
        score += lowered.count(term) / max(1, len(lowered) ** 0.5)
    return score


def search(params: dict[str, Any]) -> dict[str, Any]:
    kb_root = params["kb_root"]
    query = params["query"]
    top_k = int(params.get("top_k", 8) or 8)
    state = kb_state(kb_root)
    scored = []
    for chunk in state["chunks"]:
        score = score_chunk(query, chunk["text"])
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    hits = []
    for score, chunk in scored[:top_k]:
        hits.append(
            {
                "chunk_id": chunk["chunk_id"],
                "doc_id": chunk["doc_id"],
                "path": chunk["path"],
                "title": os.path.splitext(os.path.basename(chunk["path"]))[0],
                "heading": chunk["heading"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "page": chunk["page"],
                "score": round(score, 6),
                "matched_by": ["bm25"],
                "snippet": chunk["text"][:300],
            }
        )
    return {"mode_used": "bm25", "hits": hits}


def ask(params: dict[str, Any]) -> dict[str, Any]:
    kb_root = params["kb_root"]
    query = params["query"]
    top_k = int(params.get("top_k", 5) or 5)
    max_chars = int(params.get("max_chars", 6000) or 6000)
    state = kb_state(kb_root)
    scored = []
    for chunk in state["chunks"]:
        score = score_chunk(query, chunk["text"])
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    evidence = []
    total = 0
    for score, chunk in scored[:top_k]:
        text = chunk["text"][: max(0, max_chars - total)]
        if not text:
            break
        evidence.append(
            {
                "path": chunk["path"],
                "title": os.path.splitext(os.path.basename(chunk["path"]))[0],
                "heading": chunk["heading"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "page": chunk["page"],
                "score": round(score, 6),
                "text": text,
            }
        )
        total += len(text)
    return {"evidence": evidence, "total_chars": total, "mode_used": "bm25"}


def handle_request(request: Any) -> None:
    request_id = None
    try:
        if not isinstance(request, dict):
            respond_error(None, "bad_request", "request must be a JSON object")
            return
        request_id = request.get("id")
        op = request.get("op")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            respond_error(request_id, "bad_request", "params must be a JSON object")
            return

        if op == "ping":
            respond_ok(request_id, {"pong": True, "version": VERSION, "dense": False, "numpy": False})
            return

        if op not in {"stats", "ingest", "search", "ask", "clear"}:
            respond_error(request_id, "unknown_op", f"unknown op: {op!r}")
            return

        if op != "ping" and not isinstance(params.get("kb_root"), str):
            respond_error(request_id, "bad_request", "kb_root must be a string")
            return

        if op == "stats":
            respond_ok(request_id, stats(params["kb_root"]))
        elif op == "ingest":
            paths = params.get("paths")
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                respond_error(request_id, "bad_request", "paths must be an array of strings")
                return
            respond_ok(request_id, ingest(params))
        elif op == "search":
            if not isinstance(params.get("query"), str) or not params["query"]:
                respond_error(request_id, "bad_request", "query must be a non-empty string")
                return
            respond_ok(request_id, search(params))
        elif op == "ask":
            if not isinstance(params.get("query"), str) or not params["query"]:
                respond_error(request_id, "bad_request", "query must be a non-empty string")
                return
            respond_ok(request_id, ask(params))
        elif op == "clear":
            if params.get("confirm") is not True:
                respond_error(request_id, "confirm_required", "clear requires confirm: true")
                return
            state = kb_state(params["kb_root"])
            removed = len(state["docs"])
            STATE[params["kb_root"]] = {"docs": {}, "chunks": [], "updated_at": time.time()}
            respond_ok(request_id, {"cleared": True, "removed_docs": removed})
    except Exception as exc:  # never crash, never exit
        respond_error(request_id, "internal_error", str(exc))


def main() -> int:
    # Keep the wire UTF-8 even if the parent process/console is cp936/GBK.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    emit({"event": "ready", "version": VERSION, "dense": False, "numpy": False})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            respond_error(None, "bad_request", f"invalid JSON: {exc}")
            continue
        handle_request(request)
    return 0


if __name__ == "__main__":
    sys.exit(main())
