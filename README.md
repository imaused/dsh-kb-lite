# dsh-kb-lite

Local knowledge base for DeepSeek Harness: ingest Markdown, text and PDF files, retrieve passages with BM25 or optional dense search, and hand the model citations it can point at — file, heading, line or page.

English | [简体中文](README.zh.md)

MIT. Host-only DSH plugin, five tools, no browser UI, and the engine opens no sockets. BM25 needs nothing beyond the Python standard library.

## Why another one

`dsh-kb-rag` is the closest existing plugin, and it cannot be installed on DSH 0.2.0-rc.2. It declares a `dsh.client.inject` entry for `@deepseek-ai/dsh-client-runtime`, and that package is not part of the 0.2.0-rc.2 client baseline. It also pins `@deepseek-ai/dsh-tools` to `^0.1.0-rc.6`, which is the range DSH's own version gate rejects when the host is 0.2.0-rc.2. Every published version — 22 of them at the time of writing — carries the same peer range, so there is no older release to fall back to.

This plugin takes the other side of that trade: host-only, no client bundle, no client injection, and peer ranges written with an explicit prerelease branch so `0.2.0-rc.x` actually matches. Nothing is wrong with the idea of `dsh-kb-rag` — the blocker is the client dependency and the peer range, not the feature set. If a release appears that targets 0.2.x without injecting `dsh-client-runtime`, it will cover much of the same ground and there is no reason to prefer this one.

The scope is deliberately smaller than a full RAG stack: five tools, one local index directory, and a BM25 path that works on a machine where you have never installed torch.

## Features

- Five tools: `kb_ingest`, `kb_search`, `kb_ask`, `kb_stats`, `kb_clear`.
- No browser UI and no client bundle. `package.json` deliberately has no `dsh.client` field: the plugin only adds host tools, so a DSH release that changes client APIs cannot break an installed copy. That matters because a broken client half takes the whole web UI down with it — DSH starts all-or-nothing.
- Input: `.md`, `.markdown`, `.txt`, `.pdf`. PDF needs `pymupdf`; without it each PDF shows up as a per-file error and the rest of the batch still indexes. Other extensions are not read: a directory walk skips them, and a file you name explicitly is reported as `unsupported_extension`.
- Recursive directory ingest — hidden directories and `.git`, `node_modules`, `.venv`, `__pycache__` and similar are skipped. Incremental by `sha256`: unchanged files are skipped, changed files are re-indexed, `force: true` re-parses regardless.
- `max_files` caps one call; anything over the cap is returned as `pending` so the next call can pick it up.
- BM25 with no third-party dependency, including CJK bigram tokenisation.
- Optional dense retrieval through `sentence-transformers` and `BAAI/bge-small-zh-v1.5`, and `hybrid` mode that fuses both with reciprocal rank fusion (RRF).
- Citations: `path`, `title`, `heading`, `start_line`/`end_line` for Markdown and text, `page` for PDF, plus `matched_by` showing which retriever found the chunk.
- `kb_ask` returns verbatim source text inside a character budget — it does not paraphrase, summarise or rewrite.
- One directory per knowledge base. No database server, no daemon, no port.

## Install

```sh
dsh plugin --profile web add dsh-kb-lite
```

Or straight from GitHub:

```sh
dsh plugin --profile web add github:imaused/dsh-kb-lite
```

Replace `imaused` with the GitHub account that publishes the repository. Use your profile name if it is not `web` (`--profile desktop`, for example).

After it installs, restart DSH and open a **new** session. Tools are bound when a session is created; a session that was already open will not see them.

## Tools

| Tool | What it does | Parameters |
| --- | --- | --- |
| `kb_ingest` | Index files or directories | `paths`, `recursive`, `force`, `max_files`, `with_embeddings`, `kb_root` |
| `kb_search` | Return scored chunks with snippets | `query`, `top_k`, `mode` (`auto`, `bm25`, `dense`, `hybrid`), `kb_root` |
| `kb_ask` | Return verbatim passages for the model to cite, within a character budget | `query`, `top_k`, `max_chars`, `kb_root` |
| `kb_stats` | Document and chunk counts, `kb_root`, size, `updated_at`, up to 50 indexed paths | `kb_root` |
| `kb_clear` | Remove the index | `confirm: true`, `kb_root` |

`kb_root` is accepted by every tool and applies to that call only. Paths passed to `kb_ingest` must be absolute.

Defaults: `recursive: true`, `force: false`, `max_files: 50` (clamped to 1–500), `with_embeddings: "auto"`, `top_k: 8` for `kb_search` and `5` for `kb_ask` (both clamped to 1–50), `max_chars: 6000`. `with_embeddings` accepts `"auto"` (embed when the dense stack is available), `"on"` and `"off"`; `"off"` skips vector work for that ingest when you only want BM25.

## Quick start

These are chat prompts. Each one names the tool and the parameters explicitly, which is the most reliable way to get a deterministic call.

1. Index a documentation folder:

   ```
   Ingest D:/docs into the knowledge base with kb_ingest: paths ["D:/docs"], recursive: true, max_files: 50.
   ```

   Expect `added`/`updated`/`skipped` counts, any per-file `errors`, and `pending` if the folder held more than 50 supported files. The path must be absolute: a relative path is not resolved against a working directory, only tried against your home directory so the error can tell you exactly what was attempted.

2. Ask a question and get citable passages:

   ```
   Answer from the knowledge base with kb_ask: query "how does the retry policy back off", top_k: 5, max_chars: 6000. Quote the passages and cite path and line numbers.
   ```

3. Search with BM25 only — useful for comparing modes:

   ```
   Use kb_search with query "BM25 k1 b", top_k: 8, mode: "bm25".
   ```

4. Check what is actually indexed:

   ```
   Run kb_stats and tell me the kb_root, document count, chunk count, whether dense is available, and the indexed paths.
   ```

5. Remove a scratch index:

   ```
   Use kb_clear with kb_root "D:/kb/scratch" and confirm: true.
   ```

   This deletes the index files under that `kb_root` — `manifest.json`, `chunks.jsonl` and any vectors. Your source documents are not touched; `engine.log` stays.

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `KB_LITE_PYTHON` | not set — the plugin tries `python`, then `python3`, then `py` | Interpreter used for the engine process. Set it when the interpreter it finds is not the one that has `pymupdf` or `sentence-transformers`, or when none of those names resolves. |
| `KB_LITE_ROOT` | `~/.kb-lite` | Default `kb_root` when a call does not pass one. `~` is the user's home directory (`os.homedir()`), for example `C:/Users/you/.kb-lite` on Windows. |
| `KB_LITE_MODEL` | `BAAI/bge-small-zh-v1.5` | Embedding model used when dense retrieval is available. |
| `kb_root` (per call) | — | Overrides `KB_LITE_ROOT` for that one call. |

Precedence: per-call `kb_root` > `KB_LITE_ROOT` > `~/.kb-lite`.

Set the environment variables before starting DSH, not from inside a session.

## How retrieval works

**BM25.** A pure-Python implementation, no libraries. Tokenisation is Latin words (lowercased, `[a-z0-9_]+`) plus CJK bigrams (adjacent pairs of Chinese characters) plus a single-character fallback, with `k1 = 1.2` and `b = 0.75`. This is the mode that always works: if nothing else is installed, BM25 is still a complete keyword search with ranking.

**Dense.** When `numpy` is importable and `sentence-transformers` loads, the engine embeds chunks with `BAAI/bge-small-zh-v1.5` (configurable through `KB_LITE_MODEL`) and prepends the retrieval instruction `为这个句子生成表示以用于检索相关文章：` to queries, which is the convention that model was trained with. Dense retrieval finds paraphrases that share no words with the source. Vectors are produced during ingest, so an index built before you installed the extras has none — install them and re-ingest (with `force: true`, or after `kb_clear`) to get dense results on it.

The engine **never downloads a model as a side effect of a search**: it runs with `HF_HUB_OFFLINE=1` unless you set `KB_LITE_ALLOW_DOWNLOAD=1`. Point `KB_LITE_MODEL` at a local directory (a ModelScope download works — see `tools/fetch-model-modelscope.mjs`) and dense retrieval works offline.

**Hybrid.** Reciprocal rank fusion, `k = 60`: each retriever contributes its top `top_k * 4` candidates and the fused score decides the order. `matched_by` on each hit records whether BM25, dense, or both found it.

`mode: "auto"` uses hybrid when dense is available and BM25 otherwise, and every result reports `mode_used`, so you never have to guess which one ran. The explicit modes degrade the same way: `dense` or `hybrid` on a machine without the stack, or on an index with no vectors, falls back to BM25 rather than failing, and says so in `mode_used`. `kb_ask` has no `mode` parameter; it picks the same way and reports `mode_used`.

For a few hundred files, BM25 on its own is usually enough. Dense retrieval earns its install when your questions are paraphrases ("why does the upload keep dying?") rather than words that appear in the text ("upload timeout"), and when the corpus mixes Chinese and English. Pulling in the torch-based stack to search 40 Markdown files is not a good trade. To decide for your own corpus, run the same query twice — `mode: "bm25"` and `mode: "hybrid"` — and compare the hits.

**Chunking** matters for citations. Markdown is split at ATX headings, and a section longer than 1200 characters is split further at blank lines or sentence ends with 120 characters of overlap. Plain text is split at blank lines and merged greedily up to 1200 characters with the same overlap. PDF text is taken page by page and split the same way, with `page` counted from 1. Every chunk records `start_line` and `end_line` — line numbers in the original file, or within the page for a PDF — which is what makes `kb_ask` output quotable.

## Ranking weights (0.2.0)

Every hit carries a `score_detail` object, so a ranking is never a black box:

```json
{"bm25": 23.4, "dense": 0.81, "rrf": 0.032, "source_boost": 1.4,
 "boost_rule": "memory/", "recency": 0.87, "age_days": 6.2, "final": 28.5}
```

`bm25` and `dense` are the raw per-retriever scores, `rrf` is the fused rank score, `source_boost` is the path multiplier that matched, `recency` is the time decay, and `final` is what the list is sorted by (`score` equals `final`).

You can supply all four through the optional `weights` parameter on `kb_search` and `kb_ask`:

```json
{"bm25": 1.0, "dense": 0.6, "rrf_k": 60,
 "boost": {"memory/": 1.4, "_templates/": 0.2, "kb/README.md": 1.5},
 "recency": {"half_life_days": 30, "min": 0.25, "max": 1.0}}
```

- `bm25` / `dense` weight each retriever. In hybrid mode they scale the RRF contributions, which is how you say "trust keywords more than embeddings".
- `boost` is a path-substring multiplier; the **longest matching pattern wins**, so a file rule beats a directory rule.
- `recency` decays a document by age: `0.5 ** (age_days / half_life_days)`, clamped to `[min, max]`. Use it when newer notes should outrank older ones — the intended setting for a memory/knowledge folder that keeps growing.

Leave `weights` out and nothing changes; the older ranking is exactly what you get.

## Storage layout

Everything lives in one directory, `kb_root` (default `~/.kb-lite`, i.e. under your home directory rather than the current working directory — a desktop launch has no dependable cwd):

```
kb_root/
  manifest.json    # {version, docs: {<doc_id>: {path, sha256, mtime, size, title, kind, pages, chunks}}, updated_at}
  chunks.jsonl     # one JSON object per line: {chunk_id, doc_id, ordinal, heading, start_line, end_line, page, text}
  vectors.npy      # optional; row order matches chunks.jsonl; float32
  vectors.json     # fallback when numpy is absent; one of the two, never both
  engine.log       # engine log; the engine's stderr is mirrored here
```

`doc_id` is the first 16 hex characters of the sha1 of the normalised path; `chunk_id` is `<doc_id>:<ordinal>`. `manifest.json` and `chunks.jsonl` are plain text — you can read them, diff them, or check what was actually indexed.

The index is derived data. Delete `kb_root` and re-ingest rather than editing these files; `kb_clear` does the delete for you. If you change `KB_LITE_MODEL`, previously stored vectors are from the old model and should be rebuilt (`kb_clear`, then ingest again).

## Requirements

- **Node.js 18 or newer** — the host side of the plugin.
- **Python 3.9 or newer** — for the engine.
- **Nothing else** for BM25.
- Optional, for PDF: `pymupdf` (`pip install pymupdf`).
- Optional, for dense retrieval: `numpy` and `sentence-transformers` (`pip install numpy sentence-transformers`), in the interpreter the plugin launches (see `KB_LITE_PYTHON`).
- No database, no service, no API key.

## Limitations

These are known and not hidden behind configuration:

- **No reranker.** Hybrid fuses BM25 and dense ranks; nothing re-scores the fused list afterwards.
- **No DOI or citation-link handling.** A DOI in a PDF is text like any other. `kb_ask` gives you path, line and page, not a clickable reference.
- **No Zotero (or any bibliography) import.** Point `kb_ingest` at files on disk; that is the only entry point.
- **No OCR.** A scanned PDF has no text layer, so it yields nothing. Run OCR yourself first and ingest the text.
- **No incremental vector-store update.** Every ingest rewrites the whole vector file to match the new chunk list; unchanged chunks reuse their cached embeddings, but there is no append and no background compaction.
- **The BM25 index is rebuilt per query, not cached.** That is fine for the intended size (hundreds of files, thousands of chunks) and it keeps the on-disk format trivial; it is not the design to pick for a hundred-thousand-chunk corpus. Measured on this machine: 25 chunks answer a query in milliseconds; the cost grows with the corpus, not with the query.
- **Chunk size is fixed.** 1200 characters with 120 of overlap. There is no per-call or per-file override.
- **Four extensions only.** `.md`, `.markdown`, `.txt`, `.pdf`. DOCX, HTML and EPUB are not read.
- **The engine is a per-plugin-instance process, not a shared daemon.** Each DSH instance that loads the plugin starts its own `python engine/kb_engine.py`. Two profiles mean two processes, each loading its own copy of the embedding model if dense retrieval is on — not one shared service.

## Privacy

- The engine opens **no sockets** and makes no network requests. There is no telemetry, no update check, no remote index.
- The only long-lived process it spawns is one local `python engine/kb_engine.py` per plugin instance. Before that it runs a one-shot interpreter check, described in `SECURITY.md`.
- What it reads: the files you pass to `kb_ingest`, and its own `kb_root`.
- What it writes: only inside `kb_root`.
- No credentials are read or stored. There is no key, token or account.

One caveat, because it is easy to get wrong: if you install the optional dense stack, `sentence-transformers` (not this plugin) downloads `BAAI/bge-small-zh-v1.5` from Hugging Face the first time it runs, unless the model is already in your local cache. Pre-populate the cache, or skip the dense extras, on a machine that must not egress.

`SECURITY.md` has the exact argv, the full read/write boundary, and commands you can run to check all of the above yourself.

## Status

Version 0.1.0. Tested on Windows 11 with DSH 0.2.0-rc.2, Node 24 and Python 3.14. Linux and macOS are untested.

How this plugin is submitted to the plugin list is written up in [docs/submission.md](docs/submission.md).

## Troubleshooting

**A tool call fails with `python_not_found`, `python_start_failed` or `engine_dead`.** `python_not_found` means no candidate interpreter answered; `python_start_failed` means one was found but exited non-zero. Before the engine is launched, the plugin checks the candidate by running `python -c "import sys; sys.stdout.write('ok')"`, so an interpreter that exists but is broken fails here. Check the interpreter first:

```sh
python --version
```

Then point the plugin at a known-good interpreter with `KB_LITE_PYTHON` and restart DSH. On Windows, if `python` opens the Microsoft Store instead of running Python, use the real path:

```sh
KB_LITE_PYTHON=C:/Users/you/AppData/Local/Programs/Python/Python314/python.exe
```

To see whether the engine works at all, run it by hand and send one request:

```sh
echo '{"id":"1","op":"ping","params":{}}' | python engine/kb_engine.py
```

You should get a `ready` line and then `{"id":"1","ok":true,"result":{"pong":true,...}}`. If that works, the problem is the path the plugin is using, not the engine. If it does not, read `engine.log` in `kb_root`.

**PDFs are ignored or reported as errors.** PDF support needs `pymupdf` in the same interpreter:

```sh
python -c "import fitz; print(fitz.__doc__)"
```

A missing module shows up as a per-file entry in `errors[]` with code `pdf_unavailable`; the rest of the batch still indexes. A PDF that imports fine but produces zero chunks is usually a scan — there is no text layer and no OCR.

**Dense retrieval is unavailable.** `kb_stats` reports `dense: false`, and `mode: "auto"` stays on BM25. Install the extras into the interpreter named by `KB_LITE_PYTHON`:

```sh
python -m pip install numpy sentence-transformers
```

Then restart DSH. First use of the model may need the download described under Privacy. Without the stack, `mode: "dense"` and `mode: "hybrid"` silently fall back to BM25 and report `mode_used: "bm25"` — nothing breaks, but nothing dense happens either.

**The index is not where I expected.** `kb_stats` returns the `kb_root` in use. Default is `~/.kb-lite`; `KB_LITE_ROOT` or a per-call `kb_root` changes it. Pass an absolute path when in doubt.

**Chinese text comes back as mojibake.** On a Chinese-locale Windows console, Python's stdout defaults to GBK, not UTF-8:

```sh
python -c "import sys; print(sys.stdout.encoding)"
```

That printing `gbk` is normal and is exactly why the engine does not trust the console: it reconfigures its own stdin/stdout/stderr to UTF-8 at startup, and the plugin spawns it with `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1`. Garbled text therefore means you are not talking to the engine the plugin started — check `engine.log`, and check that `KB_LITE_PYTHON` names the interpreter you think it does.

**An ingest was cut short.** `max_files` stopped it. The response's `pending` count is what is left; call `kb_ingest` again on the same paths (already-indexed files are skipped by sha256, so this is cheap).

## License

MIT. See [LICENSE](LICENSE).
