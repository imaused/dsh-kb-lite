# kb-lite wire protocol（Node ⇄ Python）

传输：**NDJSON**，一行一个 JSON，stdin/stdout，UTF-8，无 BOM。**stderr 只放日志**，绝不混入 stdout。

## 启动

Python 进程启动后立刻输出一行 ready（stdout），然后进入读循环：

```json
{"event":"ready","version":"0.1.0","dense":false,"numpy":false}
```

`dense` = 嵌入后端是否可用（有 numpy 且能 import sentence-transformers）。
`numpy` = numpy 是否可 import。二者都可为 false，**引擎在纯 BM25 下必须完全可用**。

## 请求 / 响应

请求：

```json
{"id":"1","op":"stats","params":{"kb_root":"C:/path/.kb-lite"}}
```

响应（成功）：

```json
{"id":"1","ok":true,"result":{...}}
```

响应（失败，必须回，不许崩）：

```json
{"id":"1","ok":false,"error":{"code":"ingest_path_missing","message":"..."}}
```

- `id` 原样回传；缺 `id` 视为 `null`。
- 未知 `op` → `ok:false, code:"unknown_op"`，进程继续活着。
- 任何未捕获异常 → 捕获、按 `internal_error` 回一行，**不许退出**。
- 参数非法 → `bad_request`。

## ops

### `ping`
params: 无
result: `{"pong":true,"version":"0.1.0","dense":bool,"numpy":bool}`

### `stats`
params: `{"kb_root":str}`
result:
```json
{"docs":0,"chunks":0,"dense":false,"dense_model":null,
 "kb_root":"...","bytes":0,"updated_at":null,"paths":[]}
```
`paths` 最多列 50 条文档路径（给 agent 看库里有啥）。

### `ingest`
params:
```json
{"kb_root":str,"paths":[str],"recursive":true,"force":false,
 "max_files":50,"with_embeddings":"auto"}
```
- `paths` 可含文件或目录（目录按 `recursive` 展开）。
- 支持扩展名：`.md .markdown .txt .pdf`（`.pdf` 需 pymupdf，没有则逐文件报错，不整体失败）。
- 去重：同 `sha256` 且 `mtime+size` 未变 → `skipped`；内容变了 → `updated`。
- `force:true` 忽略增量判断，重解析重索引。
- 单次最多处理 `max_files` 个文件，超出部分放进 `pending` 返回。
result:
```json
{"added":0,"updated":0,"skipped":0,"errors":[{"path":"...","code":"...","message":"..."}],
 "pending":0,"docs":[{"path":"...","chunks":3}],"elapsed_ms":12}
```

### `search`
params:
```json
{"kb_root":str,"query":str,"top_k":8,"mode":"auto"}
```
`mode`: `auto`（有 dense 就 hybrid，否则 bm25）| `bm25` | `dense` | `hybrid`。
result:
```json
{"mode_used":"bm25","hits":[
  {"chunk_id":"...","doc_id":"...","path":"...","title":"...",
   "heading":"## 小节","start_line":12,"end_line":28,"page":null,
   "score":0.83,"matched_by":["bm25"],"snippet":"前 300 字…"}]}
```
`score` 降序；`snippet` ≤ 300 字符。

### `ask`
params: `{"kb_root":str,"query":str,"top_k":5,"max_chars":6000}`
result:
```json
{"evidence":[{"path":"...","title":"...","heading":"...",
  "start_line":12,"end_line":28,"page":null,"score":0.83,"text":"完整段落（≤ max_chars 预算内）"}],
 "total_chars":1234,"mode_used":"hybrid"}
```
`text` 是给模型直接引用的原文，**不改写**。

### `clear`
params: `{"kb_root":str,"confirm":true}`
result: `{"cleared":true,"removed_docs":0}`；`confirm` 不为 `true` → `ok:false, code:"confirm_required"`。

## 磁盘布局（`kb_root` 下）

```
kb_root/
  manifest.json      # {version:1, docs:{<doc_id>:{path,sha256,mtime,size,title,kind,pages,chunks}}, updated_at}
  chunks.jsonl       # 每行 {chunk_id,doc_id,ordinal,heading,start_line,end_line,page,text}
  vectors.npy        # 可选；行序 == chunks.jsonl 行序；float32
  vectors.json       # 没有 numpy 时的降级存储（列表的列表），与 .npy 二选一
  engine.log         # 引擎日志（stderr 也镜像一份，便于排错）
```

`doc_id` = 规范化路径的 sha1 前 16 位；`chunk_id` = `<doc_id>:<ordinal>`。

## 分块规则

- Markdown：按 ATX 标题切节；首节标题用文档标题。单节超 1200 字符再按空行/句号贪心切，重叠 120 字符。
- 纯文本：按空行分段，贪心合并到 ≤1200 字符，重叠 120。
- PDF：按页取文本，每页再按 1200 切；`page` 从 1 开始。
- 每块记录 `start_line`/`end_line`（md/txt 用原文行号；pdf 用该页内行号，`page` 非空）。

## 检索规则

- **BM25**：纯 Python 实现，无需第三方库。分词 = 拉丁词（小写，`[a-z0-9_]+`）+ CJK 二元组（连续汉字取相邻两字）+ 单字兜底。`k1=1.2 b=0.75`。
- **dense**：`sentence-transformers` + `BAAI/bge-small-zh-v1.5`（配置可改）；查询侧加指令前缀 `为这个句子生成表示以用于检索相关文章：`。
- **hybrid**：RRF，`k=60`，两路各取 `top_k*4` 候选后融合；`matched_by` 记录命中来源。
