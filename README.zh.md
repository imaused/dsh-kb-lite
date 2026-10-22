# dsh-kb-lite

DeepSeek Harness 的本地知识库：把 Markdown、纯文本和 PDF 入库，用 BM25 或可选的稠密检索取回原文段落，并把模型能直接引用的出处交给它——文件、标题、行号或页码。

[English](README.md) | 简体中文

MIT。宿主侧插件，五个工具，没有浏览器界面，引擎不打开任何 socket。BM25 只依赖 Python 标准库。

## 为什么还要另做一个

`dsh-kb-rag` 是现有插件里最接近的一个，它在 DSH 0.2.0-rc.2 上装不上。它声明了指向 `@deepseek-ai/dsh-client-runtime` 的 `dsh.client.inject`，而 0.2.0-rc.2 的客户端基线里没有这个包。同时它把 `@deepseek-ai/dsh-tools` 钉在 `^0.1.0-rc.6`，宿主是 0.2.0-rc.2 时 DSH 自己的版本闸门拒绝的正是这个范围。迄今发布的 22 个版本都带同一个 peer 范围，没有可以退回去的旧版本。

本插件站在这条取舍的另一边：只有宿主侧，不带客户端包，不做客户端注入，peer 范围写成带显式预发布分支的形式，让 `0.2.0-rc.x` 真的能匹配上。`dsh-kb-rag` 的思路本身没有问题——卡住它的是客户端依赖和 peer 范围，不是功能。如果将来出现面向 0.2.x、且不再注入 `dsh-client-runtime` 的版本，它覆盖的范围和这个插件大部分重叠，没有理由优先选这个。

范围是有意做小的：五个工具、一个本地索引目录、一条在从没装过 torch 的机器上也能跑的 BM25。

## 特性

- 五个工具：`kb_ingest`、`kb_search`、`kb_ask`、`kb_stats`、`kb_clear`。
- 没有浏览器界面，也没有客户端包。`package.json` 里刻意不写 `dsh.client` 字段：插件只加宿主侧工具，所以 DSH 改客户端 API 时弄不坏已经装好的副本。这一点重要，因为客户端那一半一旦坏了会把整个 Web 界面一起带下去——DSH 的启动是全或无。
- 输入：`.md`、`.markdown`、`.txt`、`.pdf`。PDF 需要 `pymupdf`；没有时每个 PDF 作为逐文件错误返回，批次其余文件照常入库。其它扩展名不读：目录遍历会跳过，显式点名的文件会以 `unsupported_extension` 报错。
- 支持递归目录——隐藏目录以及 `.git`、`node_modules`、`.venv`、`__pycache__` 一类的目录会跳过。按 `sha256` 增量：未变化的文件跳过，内容变化的重新索引，`force: true` 无条件重解析。
- `max_files` 限制单次调用处理的文件数；超出的部分作为 `pending` 返回，下次调用接着做。
- BM25 不依赖任何第三方库，含汉字二元组分词。
- 可选的稠密检索走 `sentence-transformers` 和 `BAAI/bge-small-zh-v1.5`；`hybrid` 用倒数排名融合（RRF）合并两路结果。
- 引用信息：`path`、`title`、`heading`，Markdown 与纯文本有 `start_line`/`end_line`，PDF 有 `page`，`matched_by` 说明这一块是哪一路检回的。
- `kb_ask` 在字符预算内返回原文，不改写、不摘要、不润色。
- 一个知识库一个目录。没有数据库服务，没有守护进程，不占端口。

## 安装

```sh
dsh plugin --profile web add dsh-kb-lite
```

或者直接从 GitHub 装：

```sh
dsh plugin --profile web add github:imaused/dsh-kb-lite
```

把 `imaused` 换成发布这个仓库的 GitHub 账号。如果你的 profile 不叫 `web`，换成自己的名字（例如 `--profile desktop`）。

装完重启 DSH，并开一个**新**会话。工具是在会话创建时绑定的；已经开着的会话看不到它们。

## 工具

| 工具 | 做什么 | 参数 |
| --- | --- | --- |
| `kb_ingest` | 索引文件或目录 | `paths`、`recursive`、`force`、`max_files`、`with_embeddings`、`kb_root` |
| `kb_search` | 返回带片段的打分结果 | `query`、`top_k`、`mode`（`auto`、`bm25`、`dense`、`hybrid`）、`kb_root` |
| `kb_ask` | 在字符预算内返回可供模型引用的原文段落 | `query`、`top_k`、`max_chars`、`kb_root` |
| `kb_stats` | 文档数、块数、`kb_root`、占用大小、`updated_at`，最多 50 条已索引路径 | `kb_root` |
| `kb_clear` | 删除索引 | `confirm: true`、`kb_root` |

每个工具都接受可选的 `kb_root`，只对本次调用生效。传给 `kb_ingest` 的路径必须是绝对路径。

默认值：`recursive: true`、`force: false`、`max_files: 50`（限制在 1–500）、`with_embeddings: "auto"`，`kb_search` 的 `top_k` 默认 8、`kb_ask` 默认 5（都限制在 1–50），`max_chars` 默认 6000。`with_embeddings` 接受 `"auto"`（有稠密依赖时算向量）、`"on"` 和 `"off"`；只想用 BM25 时传 `"off"` 可以跳过这次入库的向量计算。

## 快速开始

下面都是对话里直接输入的提示词。每条都点名了工具和参数，这是拿到确定性调用最可靠的方式。

1. 索引一个文档目录：

   ```
   用 kb_ingest 把 D:/docs 入库：paths ["D:/docs"]，recursive: true，max_files: 50。
   ```

   返回里会有 `added`/`updated`/`skipped` 计数、逐文件的 `errors`；如果目录里的受支持文件超过 50 个，还会有 `pending`。路径必须是绝对路径：相对路径不会按工作目录解析，只会尝试拼到你的主目录上，好让报错说清楚到底试了哪个路径。

2. 提问并拿到可引用的段落：

   ```
   用 kb_ask 从知识库回答：query "重试策略是怎么退避的"，top_k: 5，max_chars: 6000。引用原文，并标出 path 和行号。
   ```

3. 只用 BM25 检索——对比两种模式时很有用：

   ```
   用 kb_search 检索：query "BM25 k1 b"，top_k: 8，mode: "bm25"。
   ```

4. 看看索引里到底有什么：

   ```
   运行 kb_stats，告诉我 kb_root、文档数、块数、dense 是否可用，以及已索引的路径。
   ```

5. 清掉一个临时索引：

   ```
   用 kb_clear 清空 kb_root "D:/kb/scratch"，confirm: true。
   ```

   这会删掉该 `kb_root` 下的索引文件——`manifest.json`、`chunks.jsonl` 以及可能存在的向量文件。不会动你的原始文档；`engine.log` 会留下。

## 配置

| 设置 | 默认值 | 作用 |
| --- | --- | --- |
| `KB_LITE_PYTHON` | 未设置——依次尝试 `python`、`python3`、`py` | 启动引擎进程用的解释器。找到的不是装了 `pymupdf` 或 `sentence-transformers` 的那个，或者这些名字都解析不到时，设它。 |
| `KB_LITE_ROOT` | `~/.kb-lite` | 调用没有传 `kb_root` 时使用的默认目录。`~` 是用户主目录（`os.homedir()`），Windows 上例如 `C:/Users/you/.kb-lite`。 |
| `KB_LITE_MODEL` | `BAAI/bge-small-zh-v1.5` | 稠密检索可用时使用的嵌入模型。 |
| `kb_root`（单次调用） | — | 覆盖 `KB_LITE_ROOT`，只对本次调用生效。 |

优先级：单次调用的 `kb_root` > `KB_LITE_ROOT` > `~/.kb-lite`。

环境变量要在启动 DSH 之前设好，在会话里设不生效。

## 检索是怎么工作的

**BM25。** 纯 Python 实现，不用任何库。分词是拉丁词（转小写，`[a-z0-9_]+`）加汉字二元组（相邻两字），再加单字兜底，参数为 `k1 = 1.2`、`b = 0.75`。这是永远可用的模式：什么都没装，BM25 仍然是一套完整的、带排序的关键词检索。

**稠密检索。** 当 `numpy` 可导入、`sentence-transformers` 能加载时，引擎用 `BAAI/bge-small-zh-v1.5`（可用 `KB_LITE_MODEL` 改）给块算向量，并在查询侧加上该模型训练时用的指令前缀 `为这个句子生成表示以用于检索相关文章：`。稠密检索能找到与原文不共用一个词的改写问法。向量是在入库时算的，所以在装这些可选依赖之前建的索引没有向量——装好之后重新入库（`force: true`，或者先 `kb_clear`）才会有稠密结果。

**混合检索。** 倒数排名融合，`k = 60`：两路各取 `top_k * 4` 个候选，按融合分排序。每条命中的 `matched_by` 记录它是被 BM25、稠密检索还是两者一起找到的。

`mode: "auto"` 在有稠密时走 hybrid，否则走 bm25，每条结果都带 `mode_used`，不用猜实际跑的是哪条路。显式模式也按同样方式降级：机器上没有那套依赖，或者索引里没有向量时，`dense` 或 `hybrid` 会退回 BM25 而不是报错，并在 `mode_used` 里说明。`kb_ask` 没有 `mode` 参数，选择方式相同，也会报 `mode_used`。

实测（2026-10-23，45 篇 / 334 块，18 条查询）：BM25 的 MRR 0.844、Recall@3 89%；稠密 0.861、**Recall@3 100%**；hybrid 0.872、94%。所以**即使只有 45 篇，稠密也值得装**——差距正好落在 BM25 看不见的换说法查询上（问「插件把界面搞坏」，原文只写「客户端注入」）。代价是延迟：CPU 上 BM25 约 70ms，稠密约 900ms（模型一次性加载之后）。同一个集合上**重排实测更差**（0.756）：cross-encoder 会把"哪儿都提到关键词"的索引类文档顶上来；`weights.rerank` 保留但默认关闭。要判断自己的语料，同一个查询跑两遍——`mode: "bm25"` 和 `mode: "hybrid"`——对比命中即可。

**分块**决定了引用能不能落地。Markdown 按 ATX 标题切节，单节超过 1200 字符再按空行或句末切，重叠 120 字符。纯文本按空行切分，贪心合并到不超过 1200 字符，重叠同样是 120。PDF 按页取文本，再用同样的规则切，`page` 从 1 开始。每块都记录 `start_line` 和 `end_line`——md/txt 是原文行号，PDF 是该页内的行号——`kb_ask` 的输出之所以能直接引用，靠的就是这两个字段。

## 排序权重（0.2.0）

每个命中都带一个 `score_detail`，排名不再是黑箱：

```json
{"bm25": 23.4, "dense": 0.81, "rrf": 0.032, "source_boost": 1.4,
 "boost_rule": "memory/", "recency": 0.87, "age_days": 6.2, "final": 28.5}
```

`bm25`/`dense` 是两路原始分，`rrf` 是融合后的名次分，`source_boost` 是命中的路径乘数，`recency` 是时间衰减，`final` 是真正用来排序的数（`score` 等于 `final`）。

`kb_search` 和 `kb_ask` 都接受可选参数 `weights`，四项都能调：

```json
{"bm25": 1.0, "dense": 0.6, "rrf_k": 60,
 "boost": {"memory/": 1.4, "_templates/": 0.2, "kb/README.md": 1.5},
 "recency": {"half_life_days": 30, "min": 0.25, "max": 1.0}}
```

- `bm25` / `dense`：两路各自的权重；hybrid 时按加权 RRF 融合，用来表达"更信关键词还是更信语义"。
- `boost`：路径子串 → 乘数，**最长匹配生效**，所以文件级规则能压过目录级规则。
- `recency`：按文档年龄衰减 `0.5 ** (age_days / half_life_days)`，夹在 `[min, max]`。适合"新结论该压过旧结论"的记忆/知识目录。

不传 `weights` 时行为与旧版完全一致。

## 存储布局

所有东西都在一个目录里，即 `kb_root`（默认 `~/.kb-lite`，也就是放在用户主目录下，而不是当前工作目录——以桌面方式启动时 cwd 并不可靠）：

```
kb_root/
  manifest.json    # {version, docs: {<doc_id>: {path, sha256, mtime, size, title, kind, pages, chunks}}, updated_at}
  chunks.jsonl     # 每行一个 JSON：{chunk_id, doc_id, ordinal, heading, start_line, end_line, page, text}
  vectors.npy      # 可选；行序与 chunks.jsonl 一致；float32
  vectors.json     # 没有 numpy 时的降级存储；与 .npy 二选一，不会同时存在
  engine.log       # 引擎日志；引擎的 stderr 也镜像到这里
```

`doc_id` = 规范化路径 sha1 的前 16 位；`chunk_id` = `<doc_id>:<ordinal>`。`manifest.json` 和 `chunks.jsonl` 都是纯文本——可以直接读、直接 diff，也可以拿来核对到底入库了什么。

索引是派生数据。要重置就删掉 `kb_root` 再重新入库，不要手改这些文件；`kb_clear` 就是替你删。改了 `KB_LITE_MODEL` 之后，已存的向量来自旧模型，应当重建（`kb_clear`，再入库）。

## 环境要求

- **Node.js 18 或更新**——插件的宿主侧。
- **Python 3.9 或更新**——运行引擎。
- 跑 BM25 **不需要别的东西**。
- 可选项，PDF 需要：`pymupdf`（`pip install pymupdf`）。
- 可选项，稠密检索需要：`numpy` 和 `sentence-transformers`（`pip install numpy sentence-transformers`），装进插件实际启动的那个解释器（见 `KB_LITE_PYTHON`）。
- 不需要数据库、不需要服务、不需要 API key。

## 已知限制

以下都是已知的，没有藏在配置项后面：

- **没有重排器。** hybrid 只是把 BM25 与稠密两路排序融合，融合之后不再重新打分。
- **不处理 DOI，也不生成可点击的引用。** PDF 里的 DOI 就是普通文本。`kb_ask` 给你 path、行号和页码，不是一个能点开的文献链接。
- **不能导入 Zotero（或任何文献管理库）。** 入口只有磁盘上的文件，把 `kb_ingest` 指过去。
- **没有 OCR。** 扫描版 PDF 没有文本层，抽不出任何东西。先自己做 OCR，再入库文本。
- **向量文件整体重写，不做增量更新。** 每次入库都会按新的块列表重写 `vectors.npy`/`vectors.json`；未变化的块复用已有向量，但没有追加写，也没有后台压缩。
- **BM25 索引是每次查询时重建的，不做缓存。** 目标规模（几百个文件、几千个块）下没问题，换来的是磁盘格式足够简单；它不是为十万级块设计的。本机实测：25 个块的语料查询在毫秒级返回，代价随语料增长，而不是随查询增长。
- **块大小固定。** 1200 字符、重叠 120，没有单次调用或单文件的覆盖开关。
- **只认四种扩展名。** `.md`、`.markdown`、`.txt`、`.pdf`。DOCX、HTML、EPUB 不读。
- **引擎是每个插件实例一个进程，不是共享守护进程。** 每个加载了插件的 DSH 实例都会起自己的 `python engine/kb_engine.py`。两个 profile 就是两个进程；开着稠密检索时，各自加载一份嵌入模型，不是一个共享服务。

## 隐私

- 引擎**不打开任何 socket**，不发网络请求。没有遥测，没有更新检查，没有远程索引。
- 它唯一常驻的进程是每个插件实例一个本地 `python engine/kb_engine.py`；启动前还会跑一次一次性的解释器探测，详见 `SECURITY.md`。
- 读什么：你传给 `kb_ingest` 的文件，以及它自己的 `kb_root`。
- 写什么：只写 `kb_root` 里面。
- 不读也不存任何凭据。没有 key、没有 token、没有账号。

有一个容易搞错的例外要讲清楚：如果装了可选的稠密依赖，第一次运行时从 Hugging Face 下载 `BAAI/bge-small-zh-v1.5` 的是 `sentence-transformers`（不是本插件），除非模型已经在本机缓存里。在不能出网的机器上，要么先把缓存准备好，要么不要装稠密那一套。

`SECURITY.md` 里有精确的 argv、完整的读写边界，以及你可以自己跑的核对命令。

## 状态

版本 0.1.0。在 Windows 11、DSH 0.2.0-rc.2、Node 24、Python 3.14 上测过。Linux 与 macOS 未测试。

这个插件怎么提交到插件列表，写在 [docs/submission.md](docs/submission.md) 里。

## 排错

**工具报 `python_not_found`、`python_start_failed` 或 `engine_dead`。** `python_not_found` 表示所有候选解释器都没应答；`python_start_failed` 表示找到了一个但退出码非零。启动引擎之前，插件会先跑 `python -c "import sys; sys.stdout.write('ok')"` 做一次探测，所以解释器存在但坏掉的情况会卡在这一步。先确认解释器：

```sh
python --version
```

然后用 `KB_LITE_PYTHON` 指到一个确定可用的解释器，重启 DSH。Windows 上如果 `python` 打开的是微软商店而不是解释器，写真实路径：

```sh
KB_LITE_PYTHON=C:/Users/you/AppData/Local/Programs/Python/Python314/python.exe
```

想知道引擎本身能不能跑，手工起一次并发一条请求：

```sh
echo '{"id":"1","op":"ping","params":{}}' | python engine/kb_engine.py
```

应该先看到一个 `ready` 行，然后是 `{"id":"1","ok":true,"result":{"pong":true,...}}`。这一步能过，问题就在插件使用的路径上，不在引擎；过不去，就看 `kb_root` 下的 `engine.log`。

**PDF 被忽略或报错。** PDF 支持需要同一个解释器里有 `pymupdf`：

```sh
python -c "import fitz; print(fitz.__doc__)"
```

缺模块会以逐文件错误出现在 `errors[]` 里，错误码 `pdf_unavailable`，批次其余文件照常入库。能导入、但切出 0 个块的 PDF，多半是扫描件——没有文本层，也没有 OCR。

**稠密检索不可用。** `kb_stats` 会报 `dense: false`，`mode: "auto"` 就停在 BM25。把依赖装进插件实际启动的那个解释器：

```sh
python -m pip install numpy sentence-transformers
```

然后重启 DSH。模型第一次使用可能需要下载，见上面「隐私」。没有这套依赖时，`mode: "dense"` 和 `mode: "hybrid"` 会静默退回 BM25，并在 `mode_used` 里报 `"bm25"`——不会出错，但也不会有稠密检索。

**索引不在我以为的地方。** `kb_stats` 会返回当前使用的 `kb_root`。默认是 `~/.kb-lite`；`KB_LITE_ROOT` 或单次调用的 `kb_root` 可以改。拿不准就传绝对路径。

**中文回来是乱码。** 在中文 Windows 控制台下，Python 的 stdout 默认是 GBK 而不是 UTF-8：

```sh
python -c "import sys; print(sys.stdout.encoding)"
```

输出 `gbk` 是正常的，这也正是引擎不信任控制台的原因：它在启动时把自己的 stdin/stdout/stderr 重设为 UTF-8，插件启动它时也带上了 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`。所以出现乱码说明你面对的不是插件启动的那个引擎——查 `engine.log`，并确认 `KB_LITE_PYTHON` 指的就是你以为的那个解释器。

**入库没做完就停了。** 是 `max_files` 截断的。返回里的 `pending` 是剩下的数量；对同样的路径再调一次 `kb_ingest`（已入库的文件会按 sha256 跳过，代价很小）。

## 许可证

MIT，见 [LICENSE](LICENSE)。
