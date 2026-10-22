// kb-lite DSH host plugin.
//
// All tool logic lives here. This module deliberately imports no
// @deepseek-ai/* packages: `applyPlugin(ctx, defineTool, opts)` receives the
// host's defineTool function from lib/index.js, which keeps everything here
// directly testable with a fake ctx/defineTool.

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  EngineError,
  createChildProcessTransport,
  createEngineClient,
  resolvePythonPath,
} from './rpc.js'

const DEFAULT_TIMEOUT_MS = 30_000
const DEFAULT_INGEST_TIMEOUT_MS = 300_000

const MAX_TOP_K = 50
const DEFAULT_TOP_K_SEARCH = 8
const DEFAULT_TOP_K_ASK = 5
const DEFAULT_MAX_FILES = 50
const MAX_MAX_FILES = 500
const DEFAULT_MAX_CHARS = 6000

const SEARCH_MODES = new Set(['auto', 'bm25', 'dense', 'hybrid'])

/** Shared output renderer: never throws, always returns one text block. */
function renderJsonResult(_args, value) {
  let text
  try {
    text = JSON.stringify(value, null, 2)
  } catch {
    text = undefined
  }
  if (typeof text !== 'string') {
    try {
      text = String(value)
    } catch {
      text = '{"ok":false,"error":{"code":"render_failed","message":"result could not be rendered"}}'
    }
  }
  return [{ type: 'text', text }]
}

const TOOL_OUTPUT = {
  schema: { type: 'json' },
  render: renderJsonResult,
}

function clampInt(value, fallback, min, max) {
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) return fallback
  return Math.min(max, Math.max(min, Math.trunc(number)))
}

function publicError(error) {
  const code =
    error && typeof error === 'object' && typeof error.code === 'string' && error.code
      ? error.code
      : 'internal_error'
  let message = 'unknown error'
  try {
    message = String(error && error.message ? error.message : error)
  } catch {
    message = 'unknown error'
  }
  return { code, message }
}

function resolveEnginePath(projectRoot, requested) {
  if (typeof requested === 'string' && requested.trim() !== '') {
    return path.isAbsolute(requested)
      ? path.normalize(requested)
      : path.resolve(projectRoot, requested)
  }
  // The engine lives under <package>/engine/. Accept the conventional names so
  // the Node and Python halves can evolve independently; kb_engine.py is the
  // canonical entry point.
  const candidates = [
    'kb_engine.py',
    'engine.py',
    'main.py',
    'server.py',
    'kb-lite.py',
    'kb_lite.py',
  ]
  for (const name of candidates) {
    const candidate = path.join(projectRoot, 'engine', name)
    if (fs.existsSync(candidate)) return candidate
  }
  return path.join(projectRoot, 'engine', 'kb_engine.py')
}

function packageRootFromModule() {
  try {
    return path.resolve(fileURLToPath(new URL('..', import.meta.url)))
  } catch {
    return process.cwd()
  }
}

/**
 * Register the five kb-lite tools on a DSH context.
 *
 * @param {object} ctx              DSH context (uses ctx.tools.register, ctx.effect)
 * @param {Function} defineTool     @deepseek-ai/dsh-tools defineTool
 * @param {object} [opts]
 * @param {string} [opts.kbRoot]    Default knowledge-base directory.
 * @param {string} [opts.projectRoot] Package root used to locate engine/kb_engine.py.
 * @param {string} [opts.enginePath] Explicit engine entry point.
 * @param {string} [opts.python]    Python executable (else KB_LITE_PYTHON, else python).
 * @param {string} [opts.cwd]       Engine child-process cwd. Default process.cwd().
 * @param {object} [opts.transport] Pre-built engine transport (tests/integration).
 * @param {Function} [opts.restart] Transport factory used for (re)starts.
 * @param {number} [opts.timeoutMs] Default request timeout (30 s).
 * @param {number} [opts.ingestTimeoutMs] Ingest timeout (300 s).
 * @returns {Function} dispose() - closes the engine; safe to call more than once.
 */
export function applyPlugin(ctx, defineTool, opts = {}) {
  if (typeof defineTool !== 'function') {
    throw new TypeError('applyPlugin requires defineTool from @deepseek-ai/dsh-tools')
  }
  if (!ctx || !ctx.tools || typeof ctx.tools.register !== 'function') {
    throw new TypeError('applyPlugin requires ctx.tools.register')
  }

  const options = opts && typeof opts === 'object' ? opts : {}
  const projectRoot = path.resolve(options.projectRoot ?? packageRootFromModule())
  const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : DEFAULT_TIMEOUT_MS
  const ingestTimeoutMs = Number.isFinite(options.ingestTimeoutMs)
    ? options.ingestTimeoutMs
    : DEFAULT_INGEST_TIMEOUT_MS

  let client = null
  let disposed = false
  let cachedPython = null

  function defaultKbRoot() {
    if (typeof options.kbRoot === 'string' && options.kbRoot.trim() !== '') {
      return path.resolve(options.kbRoot)
    }
    const fromEnv = process.env.KB_LITE_ROOT
    if (typeof fromEnv === 'string' && fromEnv.trim() !== '') {
      return path.resolve(fromEnv)
    }
    return path.join(os.homedir(), '.kb-lite')
  }

  function resolveKbRoot(value) {
    if (typeof value === 'string' && value.trim() !== '') return path.resolve(value)
    return defaultKbRoot()
  }

  function makeSpawnTransport() {
    const enginePath = resolveEnginePath(projectRoot, options.enginePath)
    if (!fs.existsSync(enginePath)) {
      throw new EngineError(
        'engine_not_found',
        `Python engine not found at ${enginePath}. Set opts.enginePath or install the engine/ directory.`,
      )
    }
    if (!cachedPython) {
      cachedPython = resolvePythonPath({
        python: options.python,
        env: options.env ?? process.env,
        probe: options.probePython,
      }).python
    }
    return createChildProcessTransport({
      argv: [cachedPython, enginePath],
      cwd: options.cwd ?? process.cwd(),
      env: options.env,
      spawnFn: options.spawnFn,
    })
  }

  function getClient() {
    if (disposed) throw new EngineError('engine_closed', 'kb-lite plugin has been disposed')
    if (client) return client
    const restart =
      typeof options.restart === 'function'
        ? options.restart
        : typeof options.createTransport === 'function'
          ? options.createTransport
          : options.transport
            ? undefined
            : makeSpawnTransport
    client = createEngineClient({
      transport: options.transport,
      restart,
      timeoutMs,
      backoffMs: options.backoffMs,
      restartLimit: options.restartLimit,
      killOnAbort: options.killOnAbort,
    })
    return client
  }

  function badRequest(message) {
    return new EngineError('bad_request', message)
  }

  function buildIngestParams(args) {
    const kbRoot = resolveKbRoot(args.kb_root)

    let rawPaths = args.paths
    if (typeof rawPaths === 'string') rawPaths = [rawPaths]
    if (!Array.isArray(rawPaths) || rawPaths.length === 0) {
      throw badRequest('kb_ingest requires a non-empty paths array of absolute paths')
    }

    const paths = []
    for (const rawPath of rawPaths) {
      if (typeof rawPath !== 'string' || rawPath.trim() === '') {
        throw badRequest('kb_ingest paths entries must be non-empty strings')
      }
      const trimmed = rawPath.trim()
      if (path.isAbsolute(trimmed)) {
        paths.push(trimmed)
        continue
      }
      // Contract: paths must be absolute. We still try to be helpful: resolve a
      // relative path against the home directory, and if that does not exist,
      // say exactly what happened in the error text.
      const againstHome = path.resolve(os.homedir(), trimmed)
      if (fs.existsSync(againstHome)) {
        paths.push(againstHome)
        continue
      }
      throw new EngineError(
        'path_not_absolute',
        `kb_ingest paths must be absolute; '${trimmed}' was resolved against ` +
          `${os.homedir()} as '${againstHome}' and that path does not exist`,
      )
    }

    return {
      kb_root: kbRoot,
      paths,
      recursive: args.recursive !== false,
      force: args.force === true,
      max_files: clampInt(args.max_files, DEFAULT_MAX_FILES, 1, MAX_MAX_FILES),
      with_embeddings:
        typeof args.with_embeddings === 'string' && args.with_embeddings.trim() !== ''
          ? args.with_embeddings.trim()
          : 'auto',
    }
  }

  function normalizeWeights(raw) {
    if (raw === undefined || raw === null) return undefined
    if (typeof raw !== 'object' || Array.isArray(raw)) {
      throw badRequest(
        'weights must be an object like { "bm25": 1.0, "dense": 0.6, "boost": { "memory/": 1.3 } }',
      )
    }
    const out = {}
    for (const key of ['bm25', 'dense', 'rrf_k']) {
      if (raw[key] === undefined || raw[key] === null) continue
      const n = Number(raw[key])
      if (!Number.isFinite(n) || n < 0) {
        throw badRequest(`weights.${key} must be a non-negative number`)
      }
      out[key] = n
    }
    if (raw.boost !== undefined && raw.boost !== null) out.boost = raw.boost
    if (raw.recency !== undefined && raw.recency !== null) {
      if (typeof raw.recency !== 'object' || Array.isArray(raw.recency)) {
        throw badRequest('weights.recency must be an object like { "half_life_days": 30, "min": 0.25 }')
      }
      out.recency = raw.recency
    }
    return Object.keys(out).length > 0 ? out : undefined
  }

  function requireQuery(args, toolName) {
    if (typeof args.query !== 'string' || args.query.trim() === '') {
      throw badRequest(`${toolName} requires a non-empty query string`)
    }
    return args.query
  }

  function buildSearchParams(args) {
    const query = requireQuery(args, 'kb_search')
    let mode = typeof args.mode === 'string' ? args.mode.trim().toLowerCase() : 'auto'
    if (mode === '') mode = 'auto'
    if (!SEARCH_MODES.has(mode)) {
      throw badRequest(`kb_search mode must be one of: ${[...SEARCH_MODES].join(', ')}`)
    }
    const weights = normalizeWeights(args.weights)
    return {
      kb_root: resolveKbRoot(args.kb_root),
      query,
      top_k: clampInt(args.top_k, DEFAULT_TOP_K_SEARCH, 1, MAX_TOP_K),
      mode,
      ...(weights ? { weights } : {}),
    }
  }

  function buildAskParams(args) {
    const weights = normalizeWeights(args.weights)
    return {
      kb_root: resolveKbRoot(args.kb_root),
      query: requireQuery(args, 'kb_ask'),
      top_k: clampInt(args.top_k, DEFAULT_TOP_K_ASK, 1, MAX_TOP_K),
      max_chars: clampInt(args.max_chars, DEFAULT_MAX_CHARS, 1, 200_000),
      ...(weights ? { weights } : {}),
    }
  }

  function buildStatsParams(args) {
    return { kb_root: resolveKbRoot(args.kb_root) }
  }

  function buildClearParams(args) {
    if (args.confirm !== true) {
      throw new EngineError(
        'confirm_required',
        'kb_clear requires confirm: true because it deletes the whole knowledge base',
      )
    }
    return { kb_root: resolveKbRoot(args.kb_root), confirm: true }
  }

  const builders = {
    ingest: buildIngestParams,
    search: buildSearchParams,
    ask: buildAskParams,
    stats: buildStatsParams,
    clear: buildClearParams,
  }

  async function runTool(op, rawArgs, exec) {
    try {
      const args = rawArgs && typeof rawArgs === 'object' ? rawArgs : {}
      const params = builders[op](args)
      const engine = getClient()
      const result = await engine.request(op, params, {
        signal: exec && exec.signal ? exec.signal : undefined,
        timeoutMs: op === 'ingest' ? ingestTimeoutMs : timeoutMs,
      })
      const body =
        result && typeof result === 'object' && !Array.isArray(result) ? result : { result }
      return { ok: true, ...body }
    } catch (error) {
      return { ok: false, error: publicError(error) }
    }
  }

  const toolDefinitions = [
    {
      name: 'kb_ingest',
      description:
        'Ingest local documents (Markdown, TXT, PDF) into the kb-lite knowledge base. ' +
        'paths must be absolute; directories are expanded recursively by default. ' +
        'Returns counts for added/updated/skipped files plus per-file errors and pending work.',
      output: TOOL_OUTPUT,
      parameters: {
        paths: {
          type: 'array',
          items: { type: 'string' },
          required: true,
          description:
            'Absolute file or directory paths to ingest. Relative paths are rejected (they are only resolved against the home directory to report a useful error).',
        },
        kb_root: {
          type: 'string',
          description:
            'Absolute knowledge-base directory. Defaults to KB_LITE_ROOT or ~/.kb-lite.',
        },
        recursive: {
          type: 'boolean',
          description: 'Recursively expand directory paths (default true).',
        },
        force: {
          type: 'boolean',
          description: 'Re-parse and re-index unchanged files (default false).',
        },
        max_files: {
          type: 'integer',
          description:
            'Maximum files processed in one call, clamped to 1..500 (default 50). Extra files are reported as pending.',
        },
        with_embeddings: {
          type: 'string',
          description: 'Embedding policy: "auto" (default), "on", or "off".',
        },
      },
      async execute(args, exec) {
        return runTool('ingest', args, exec)
      },
    },
    {
      name: 'kb_search',
      description:
        'Search the kb-lite knowledge base and return ranked chunks with path, heading, line range, score and snippet. ' +
        'Uses BM25 by default and hybrid retrieval when embeddings are available.',
      output: TOOL_OUTPUT,
      parameters: {
        query: {
          type: 'string',
          required: true,
          description: 'Search query (Chinese or English).',
        },
        kb_root: {
          type: 'string',
          description:
            'Absolute knowledge-base directory. Defaults to KB_LITE_ROOT or ~/.kb-lite.',
        },
        top_k: {
          type: 'integer',
          description: 'Number of hits to return, clamped to 1..50 (default 8).',
        },
        mode: {
          type: 'string',
          description: 'Retrieval mode: auto (default), bm25, dense, or hybrid.',
        },
        weights: {
          type: 'json',
          description:
            'Optional ranking weights: { "bm25": 1.0, "dense": 0.6, "rrf_k": 60, ' +
            '"boost": { "<path substring>": multiplier }, ' +
            '"recency": { "half_life_days": 30, "min": 0.25, "max": 1.0 } }. ' +
            'boost 按最长匹配生效；recency 让旧文档按半衰期衰减。每个命中都带 score_detail 分解。',
        },
      },
      async execute(args, exec) {
        return runTool('search', args, exec)
      },
    },
    {
      name: 'kb_ask',
      description:
        'Retrieve the best evidence passages for a question, returned verbatim with source path, heading, line range and page. ' +
        'Use this before answering questions about documents in the knowledge base.',
      output: TOOL_OUTPUT,
      parameters: {
        query: {
          type: 'string',
          required: true,
          description: 'Question or query to retrieve evidence for.',
        },
        kb_root: {
          type: 'string',
          description:
            'Absolute knowledge-base directory. Defaults to KB_LITE_ROOT or ~/.kb-lite.',
        },
        top_k: {
          type: 'integer',
          description: 'Number of evidence passages, clamped to 1..50 (default 5).',
        },
        max_chars: {
          type: 'integer',
          description: 'Total character budget for evidence text (default 6000).',
        },
        weights: {
          type: 'json',
          description:
            'Optional ranking weights, same shape as kb_search: { "bm25", "dense", "rrf_k", ' +
            '"boost": { "<path substring>": multiplier }, "recency": { "half_life_days", "min", "max" } }.',
        },
      },
      async execute(args, exec) {
        return runTool('ask', args, exec)
      },
    },
    {
      name: 'kb_stats',
      description:
        'Report knowledge-base statistics: document/chunk counts, dense retrieval availability, size, update time and up to 50 indexed paths.',
      output: TOOL_OUTPUT,
      parameters: {
        kb_root: {
          type: 'string',
          description:
            'Absolute knowledge-base directory. Defaults to KB_LITE_ROOT or ~/.kb-lite.',
        },
      },
      async execute(args, exec) {
        return runTool('stats', args, exec)
      },
    },
    {
      name: 'kb_clear',
      description:
        'Delete every document and index file in the kb-lite knowledge base. Requires confirm: true.',
      output: TOOL_OUTPUT,
      parameters: {
        confirm: {
          type: 'boolean',
          description: 'Must be true; otherwise the tool refuses to clear the knowledge base.',
        },
        kb_root: {
          type: 'string',
          description:
            'Absolute knowledge-base directory. Defaults to KB_LITE_ROOT or ~/.kb-lite.',
        },
      },
      async execute(args, exec) {
        return runTool('clear', args, exec)
      },
    },
  ]

  for (const definition of toolDefinitions) {
    ctx.tools.register(defineTool(definition))
  }

  function dispose() {
    if (disposed) return
    disposed = true
    if (!client) return
    const closing = client
    client = null
    return closing.close()
  }

  if (typeof ctx.effect === 'function') {
    ctx.effect(() => () => {
      const pending = dispose()
      if (pending && typeof pending.then === 'function') pending.catch(() => {})
    })
  }

  return dispose
}
