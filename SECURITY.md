# Security model

This describes version 0.1.0 of `dsh-kb-lite`: one host-only DSH plugin, five tools, one short-lived Python probe, one long-lived Python process. Every claim below is written so that you can check it against the files in this repository, and the commands to do that are at the end.

Nothing here is a request for trust. If a line in this document and a line in the code disagree, the code is what runs.

## Execution model

`dsh-kb-lite` is loaded by DSH as a host plugin. It has no browser UI, no client bundle, and no `dsh.client` field in `package.json` — so it contributes nothing to the DSH web or desktop client and cannot interfere with it. There is no HTTP server, no listening socket, no timer, and no watcher.

When the plugin is applied it registers five tools and starts nothing. On the first tool call it resolves an interpreter, checks it with a short probe process, and starts one engine process. That engine lives for the life of the plugin instance. Two DSH instances that both load the plugin have two engines, one each; there is no daemon and no shared service.

## The engine process

Its spawn call is in `lib/rpc.js`:

```js
const child = spawnFn(argv[0], argv.slice(1), {
  cwd,
  env: {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    ...(env && typeof env === 'object' ? env : {}),
  },
  stdio: ['pipe', 'pipe', 'pipe'],
  windowsHide: true,
})
```

`spawnFn` is `node:child_process.spawn`, and `argv` is built in `lib/plugin.js` as `[cachedPython, enginePath]`. What each part means:

| Part | Value |
| --- | --- |
| `argv[0]` | `cachedPython`: `KB_LITE_PYTHON` if set, otherwise the first candidate that passed the probe. |
| `argv[1]` | `enginePath`: the absolute path to `engine/kb_engine.py` inside the installed package. |
| Further arguments | None. No file paths, no queries, no flags. |
| Shell | None. `spawn` is called with an argument vector and without `shell: true`; nothing is interpolated into a command line. |
| `cwd` | The plugin's `cwd` option, otherwise the DSH process's working directory. Document paths are always absolute, so the child's cwd is not used to resolve them. |
| `stdio` | All three piped. Requests in on stdin, responses out on stdout, logs on stderr. |
| `env` | The parent environment, then `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1`, then any environment the plugin was configured with. |
| `windowsHide` | No console window is created on Windows. |

The two environment variables matter for correctness, not tidiness. On a Chinese-locale Windows console Python's stdout defaults to GBK (`sys.stdout.encoding == 'gbk'`), which would corrupt UTF-8 JSON on the wire. The spawn environment forces UTF-8, and the engine calls `sys.stdin.reconfigure`, `sys.stdout.reconfigure` and `sys.stderr.reconfigure` at startup as well, so neither side depends on the console's code page.

The transport is NDJSON over the pipes: one JSON object per line, UTF-8, LF, no BOM. The engine answers every request with the `id` echoed back, and an unhandled exception becomes an `ok: false` line rather than a crash.

## The interpreter probe

Before the engine is started, the candidate interpreter is checked with a one-shot process, in `lib/rpc.js`:

```js
const result = spawnSync(
  candidate,
  ['-c', 'import sys; sys.stdout.write("ok")'],
  { windowsHide: true, timeout: 10_000, encoding: 'utf8' },
)
```

- The argument is a **fixed string**. Nothing from your documents, your query or your configuration is placed in it.
- It runs as your user, with the default spawn environment.
- It exits immediately; its output is captured to decide whether to use that interpreter.
- Candidates are `python` (or `KB_LITE_PYTHON` if set), then `python3`, then `py`. Probing stops at the first success, or at the first failure that is not "command not found". In a normal install that means **one** probe.
- If every candidate fails, the tool call fails with `python_not_found` or `python_start_failed` and no engine is started.

## Lifetime and restarts

- The engine is started lazily, on the first tool call.
- If it exits unexpectedly, the plugin rejects in-flight requests with `engine_restarted` and starts a replacement, up to 3 times with a 150–500 ms backoff. After that it gives up: the state is `engine_dead` and tool calls fail until DSH restarts.
- At most **one** engine process per plugin instance is alive at any moment. A replacement is started only after the previous one has exited.
- When the plugin is disposed, the plugin calls `kill()`: stdin is closed, then `SIGTERM`, then `SIGKILL` if the process is still alive one second later. A `process.on('exit')` hook repeats the best-effort kill if DSH exits first.

## What it reads

- **The files you pass to `kb_ingest`.** Paths must be absolute. A file is read only if its extension is `.md`, `.markdown`, `.txt` or `.pdf`. A directory is walked to find those extensions; hidden directories and `.git`, `.hg`, `.svn`, `node_modules`, `__pycache__`, `.venv`, `venv`, `.tox`, `.idea`, `.vscode` and similar are skipped, and the contents of unsupported files are not read.
- **Its own `kb_root`.** `manifest.json`, `chunks.jsonl`, and `vectors.npy` / `vectors.json` if present, plus `engine.log`.
- **Its own package files.** `engine/kb_engine.py` is what Python loads.

Nothing else. It does not read DSH configuration, credentials, browser data, shell history, or your home directory generally. The `paths` argument is the entire input surface, and it is checked for absoluteness before anything is opened.

## What it writes

Only inside `kb_root`:

| File | Contents |
| --- | --- |
| `manifest.json` | Per-document metadata: path, `sha256`, mtime, size, title, kind, page count, chunk count. |
| `chunks.jsonl` | One JSON object per chunk, including the chunk's **full text**. |
| `vectors.npy` or `vectors.json` | Optional embeddings, when the dense stack is available. |
| `engine.log` | Engine log lines; the engine's stderr is mirrored here. |

Writes are atomic: the engine writes a temporary `<name>.tmp.<pid>` file inside `kb_root` and renames it into place, so a crash mid-write cannot leave a half-written index. Every one of these paths is produced by `_child(root, name)`, which resolves the real path and raises `path_escape` if it would land outside `kb_root` — a fixed set of names, joined to the root and checked, not a path assembled from user input.

`kb_root` is created if it does not exist. `kb_clear` deletes the index files there (`manifest.json`, `chunks.jsonl`, both vector files); your source documents are never touched, and `engine.log` is left in place. The plugin writes nothing outside `kb_root`.

Two consequences worth stating plainly:

- **The index contains your documents' text in plain text, unencrypted.** `chunks.jsonl` is a readable copy of everything you ingested. Protect `kb_root` the way you protect the sources. It inherits your filesystem permissions and gets no additional protection.
- **It runs with your privileges.** There is no sandbox, no privilege drop, no separate service account. If your user can read a path and you pass it to `kb_ingest`, the engine can read it too.

## What it downloads

**Nothing.** There is no installer download, no model fetch in this repository's code, no update check, no telemetry endpoint, no remote index.

One honest qualification: if you install the optional dense stack, `sentence-transformers` — a third-party library you installed yourself — downloads `BAAI/bge-small-zh-v1.5` from Hugging Face the first time it loads the model, unless the model is already in your local cache. That connection is made by that library, not by `dsh-kb-lite`, and it does not happen on a BM25-only install. On a machine that must not egress, pre-populate the Hugging Face cache or skip `sentence-transformers`.

## Network

The engine opens no sockets. It imports no `socket`, `http`, `urllib`, `requests` or equivalent, and it neither listens on a port nor connects to one. Its file descriptors are stdin, stdout, stderr, the index files under `kb_root`, and the document paths you named.

## Credentials

None. There is no API key, token, cookie, account or login. No environment variable carrying a secret is read. The plugin stores nothing that could authenticate anywhere, because it never authenticates anywhere.

It does inherit your environment (`...process.env`), as any child process does. That is an inheritance, not a use: kb-lite does not read credential variables.

## Install-time behaviour

`package.json` declares no `preinstall`, `install` or `postinstall` script, and the package has no `dependencies` — only `peerDependencies` on `@deepseek-ai/cordis` and `@deepseek-ai/dsh-tools`, which DSH itself provides. Installing the plugin therefore does not execute plugin code and does not pull third-party npm code into the profile. The plugin's code runs when DSH applies it, not when it is installed.

The Python side has no `pip install` step and no package of its own. The engine is one script, and on the BM25 path it imports only the standard library.

## Verify it yourself

Run these from the repository root, or from the installed package directory. `grep` examples are for bash, macOS, Linux or Git Bash; PowerShell equivalents are given where they differ. The spawn calls live in `lib/rpc.js`, not `lib/index.js`.

**1. List every process-creation site in the host half.**

```sh
node -e "require('fs').readFileSync('lib/rpc.js','utf8').split('\n').forEach((l,i)=>{if(/spawn/i.test(l))console.log((i+1)+': '+l)})"
```

Expect the `node:child_process` import, the probe at `spawnSync(...)`, the engine start at `spawnFn(argv[0], argv.slice(1), ...)`, and comments mentioning spawn. There should be no `shell: true` anywhere.

**2. Confirm there are no network primitives in the engine.**

```sh
grep -nE "socket|urllib|requests|http\.client|urlopen|socketserver" engine/kb_engine.py
```

PowerShell:

```powershell
Select-String -Path engine\kb_engine.py -Pattern 'socket|urllib|requests|http\.client|urlopen|socketserver'
```

Expect no output. A match inside a comment or a docstring is not a network call, but there should be no match at all.

**3. Confirm there is no network code in the host half.**

```sh
grep -nE "node:http|node:https|node:net|require\('http'\)|fetch\(" lib/index.js lib/plugin.js lib/rpc.js
```

PowerShell:

```powershell
Select-String -Path lib\index.js,lib\plugin.js,lib\rpc.js -Pattern 'node:http|node:https|node:net|fetch\('
```

Expect no output.

**4. List every import in the engine.**

```sh
grep -nE "^\s*(import|from) " engine/kb_engine.py
```

Every module named should be from the Python standard library, or one of the three optional third-party ones: `fitz` (pymupdf, for PDF), `numpy`, `sentence_transformers`. Anything else is worth asking about.

**5. Confirm the engine really is a local stdin/stdout program.** Send it one request by hand:

```sh
echo '{"id":"1","op":"ping","params":{}}' | python engine/kb_engine.py
```

Expect a `ready` line, then `{"id":"1","ok":true,"result":{"pong":true,"version":"0.1.0",...}}`. No arguments, no configuration, no network.

**6. Confirm the write guard exists.**

```sh
grep -n "def _child" -A 8 engine/kb_engine.py
```

PowerShell:

```powershell
Select-String -Path engine\kb_engine.py -Pattern 'def _child' -Context 0,8
```

Expect a function that joins a fixed file name onto the root, resolves the real path, and raises `path_escape` when the result would not be a direct child of `kb_root`.

**7. Watch the processes at runtime.** With DSH running and the plugin loaded, after any `kb_stats` call:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*kb_engine.py*' } |
  Select-Object ProcessId, ParentProcessId, CommandLine | Format-List
```

On macOS or Linux:

```sh
ps -eo pid,ppid,args | grep -F kb_engine.py | grep -v grep
```

Expect **one** long-lived row per DSH instance that loaded the plugin. `CommandLine` should read `"<python>" "<...>/engine/kb_engine.py"` and nothing else — in particular, no paths to the documents you ingested. A second, brief row containing `-c "import sys; sys.stdout.write('ok')"` is the startup probe and is gone in milliseconds.

**8. Watch its sockets.** Take the `ProcessId` of the engine from step 7:

```powershell
Get-NetTCPConnection -OwningProcess <ProcessId> -ErrorAction SilentlyContinue
```

On macOS or Linux:

```sh
ss -tp | grep <pid>
```

Expect no rows while queries and ingests are running. Do this with the dense model already cached: during the one-time model download the connection belongs to `sentence-transformers`, as described above.

**9. Confirm the write boundary.** `kb_stats` returns the `kb_root` in use; list it:

```powershell
Get-ChildItem -Force (Join-Path $env:USERPROFILE '.kb-lite')
```

Expect `manifest.json`, `chunks.jsonl`, `engine.log`, and `vectors.npy` only if dense is enabled. Nothing else should appear, and nothing outside that directory should change.

**10. Confirm there is no install script and no runtime dependency.**

```sh
node -e "const p=require('./package.json');console.log(JSON.stringify({scripts:p.scripts,dependencies:p.dependencies||{},files:p.files},null,2))"
```

Expect `scripts` to contain only `test`, and `dependencies` to be `{}`.

## Reporting

If you find behavior that contradicts this document, that is a security-relevant bug: open an issue with the version, the platform, and the command you ran to observe it. Do not include document contents or paths you consider private.
