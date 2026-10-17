// kb-lite engine client: process manager + NDJSON request/response client.
//
// This module intentionally has NO @deepseek-ai/* imports. The transport is
// injectable so the whole client can be unit-tested in memory.
//
// Transport contract (the object passed as `transport` to createEngineClient):
//
//   write(line)    Send one NDJSON record. `line` has NO trailing newline;
//                  the real child-process transport appends '\n' and writes it
//                  as UTF-8. Must throw synchronously if the pipe is unusable.
//   onLine(cb)     Register a callback for each complete stdout line (no '\n',
//                  '\r' already stripped). Returns an unsubscribe function.
//   onExit(cb)     Register a callback for process exit / spawn failure. The
//                  callback receives { code, signal, error }.
//   kill()         Best-effort termination of the engine process.
//   stderrTail()   Optional. Returns the tail of stderr (last 8 KB).
//   closed         Optional. Promise that settles when the process is gone.
//
// The real implementation is createChildProcessTransport({ argv, cwd, env }).
// `createChildProcessTransport` only uses node:child_process, so it can be
// tested by injecting a fake child via `spawnFn`.

import { spawn, spawnSync } from 'node:child_process'
import { StringDecoder } from 'node:string_decoder'

export const DEFAULT_TIMEOUT_MS = 30_000
export const DEFAULT_INGEST_TIMEOUT_MS = 300_000
export const DEFAULT_RESTART_LIMIT = 3
export const DEFAULT_STDERR_LIMIT = 8 * 1024

// How much of the stderr tail is embedded in an error message.
const STDERR_IN_MESSAGE = 2_000

/** Error shape used by the whole client; `code` is what tools surface. */
export class EngineError extends Error {
  constructor(code, message, extra = undefined) {
    super(message)
    this.name = 'EngineError'
    this.code = code
    if (extra && typeof extra === 'object') {
      if (extra.cause !== undefined) this.cause = extra.cause
      if (extra.detail !== undefined) this.detail = extra.detail
    }
  }
}

/**
 * Incremental NDJSON line splitter.
 *
 * push(chunk) returns every complete line contained in the buffer. Partial
 * trailing data is kept until the next push. A trailing '\r' (CRLF) is
 * stripped from returned lines. Empty lines are returned as '' so callers can
 * decide to ignore them.
 */
export function createLineSplitter() {
  let buffer = ''
  return {
    push(chunk) {
      buffer += chunk == null ? '' : String(chunk)
      const lines = []
      let index
      while ((index = buffer.indexOf('\n')) >= 0) {
        let line = buffer.slice(0, index)
        buffer = buffer.slice(index + 1)
        if (line.endsWith('\r')) line = line.slice(0, -1)
        lines.push(line)
      }
      return lines
    },
    flush() {
      if (buffer === '') return []
      let line = buffer
      buffer = ''
      if (line.endsWith('\r')) line = line.slice(0, -1)
      return [line]
    },
    get buffered() {
      return buffer
    },
  }
}

function appendStderr(current, chunk, limit) {
  const next = current + String(chunk == null ? '' : chunk)
  if (next.length <= limit) return next
  return next.slice(next.length - limit)
}

function resolveBackoff(backoffMs) {
  if (typeof backoffMs === 'function') {
    const value = Number(backoffMs())
    return Number.isFinite(value) && value >= 0 ? value : 0
  }
  if (typeof backoffMs === 'number' && Number.isFinite(backoffMs) && backoffMs >= 0) {
    return backoffMs
  }
  // Default: 150-500 ms as required by the protocol contract.
  return 150 + Math.floor(Math.random() * 351)
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function asEngineError(err, fallbackCode) {
  if (err instanceof EngineError) return err
  if (err && typeof err === 'object' && typeof err.code === 'string') {
    return new EngineError(err.code, String(err.message || err.code), { cause: err })
  }
  const message = err instanceof Error ? err.message : String(err)
  return new EngineError(fallbackCode, message)
}

/**
 * Build a child-process transport for the Python engine.
 *
 * @param {object} options
 * @param {string[]} options.argv      [pythonExecutable, engineScript, ...args]
 * @param {string}   [options.cwd]
 * @param {object}   [options.env]     Merged over process.env.
 * @param {Function} [options.spawnFn] Injectable spawn (tests).
 * @param {number}   [options.stderrLimit]
 */
export function createChildProcessTransport(options = {}) {
  const {
    argv,
    cwd,
    env,
    spawnFn = spawn,
    stderrLimit = DEFAULT_STDERR_LIMIT,
  } = options

  if (!Array.isArray(argv) || argv.length === 0 || typeof argv[0] !== 'string') {
    throw new Error('createChildProcessTransport requires argv = [pythonExecutable, engineScript, ...]')
  }

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

  const lineListeners = new Set()
  const exitListeners = new Set()
  const stdoutSplitter = createLineSplitter()
  const stdoutDecoder = new StringDecoder('utf8')
  const stderrDecoder = new StringDecoder('utf8')
  let stderr = ''
  let exited = false
  let resolveClosed
  const closed = new Promise((resolve) => {
    resolveClosed = resolve
  })

  const emitLine = (line) => {
    if (line === '') return
    for (const listener of [...lineListeners]) {
      try {
        listener(line)
      } catch {
        // Listener failures must never break the stream.
      }
    }
  }

  const emitExit = (info) => {
    if (exited) return
    exited = true
    let rest = ''
    try {
      rest = stdoutDecoder.end()
    } catch {
      rest = ''
    }
    if (rest) {
      for (const line of stdoutSplitter.push(rest)) emitLine(line)
    }
    for (const line of stdoutSplitter.flush()) emitLine(line)
    for (const listener of [...exitListeners]) {
      try {
        listener(info)
      } catch {
        // ignore listener failures
      }
    }
    resolveClosed(info)
  }

  if (child.stdout) {
    child.stdout.on('data', (chunk) => {
      let text
      try {
        text = typeof chunk === 'string' ? chunk : stdoutDecoder.write(chunk)
      } catch {
        text = String(chunk)
      }
      for (const line of stdoutSplitter.push(text)) emitLine(line)
    })
    child.stdout.on('error', (err) => {
      stderr = appendStderr(stderr, `[stdout error] ${err && err.message ? err.message : err}\n`, stderrLimit)
    })
  }

  if (child.stderr) {
    child.stderr.on('data', (chunk) => {
      let text
      try {
        text = typeof chunk === 'string' ? chunk : stderrDecoder.write(chunk)
      } catch {
        text = String(chunk)
      }
      stderr = appendStderr(stderr, text, stderrLimit)
    })
    child.stderr.on('error', () => {})
  }

  if (child.stdin) {
    child.stdin.on('error', (err) => {
      stderr = appendStderr(stderr, `[stdin error] ${err && err.message ? err.message : err}\n`, stderrLimit)
    })
  }

  child.on?.('error', (err) => {
    emitExit({ code: null, signal: null, error: err })
  })
  child.on?.('exit', (code, signal) => {
    emitExit({ code: code ?? null, signal: signal ?? null, error: null })
  })

  const transport = {
    argv,
    get pid() {
      return child.pid ?? null
    },
    get alive() {
      return !exited
    },
    closed,
    write(line) {
      if (exited) throw new Error('engine process has already exited')
      const stdin = child.stdin
      if (!stdin || stdin.destroyed || stdin.writableEnded) {
        throw new Error('engine stdin is not writable')
      }
      stdin.write(String(line) + '\n', 'utf8')
    },
    onLine(cb) {
      lineListeners.add(cb)
      return () => lineListeners.delete(cb)
    },
    onExit(cb) {
      exitListeners.add(cb)
      return () => exitListeners.delete(cb)
    },
    stderrTail() {
      return stderr
    },
    kill() {
      if (exited) return
      try {
        child.stdin?.end()
      } catch {
        // ignore
      }
      try {
        child.kill()
      } catch {
        // ignore
      }
      // Belt and braces: if SIGTERM is ignored, force-kill shortly after.
      const timer = setTimeout(() => {
        if (!exited) {
          try {
            child.kill('SIGKILL')
          } catch {
            // ignore
          }
        }
      }, 1000)
      if (typeof timer.unref === 'function') timer.unref()
    },
  }

  return transport
}

/** Default probe used by resolvePythonPath. */
function probePython(candidate) {
  const result = spawnSync(
    candidate,
    ['-c', 'import sys; sys.stdout.write("ok")'],
    { windowsHide: true, timeout: 10_000, encoding: 'utf8' },
  )
  if (result.error) {
    return {
      ok: false,
      error: result.error,
      status: result.status,
      stdout: result.stdout,
      stderr: result.stderr,
    }
  }
  return {
    ok: result.status === 0,
    status: result.status,
    stdout: result.stdout,
    stderr: result.stderr,
  }
}

/**
 * Resolve a usable Python executable.
 *
 * Order: opts.python -> env.KB_LITE_PYTHON -> 'python'. On ENOENT, retry with
 * 'python3', then 'py'. Throws EngineError('python_not_found') if none work.
 * `probe` is injectable for tests.
 */
export function resolvePythonPath(options = {}) {
  const env = options.env ?? process.env
  const base = options.python || (env && env.KB_LITE_PYTHON) || 'python'
  const candidates = []
  for (const candidate of [base, 'python3', 'py']) {
    if (typeof candidate === 'string' && candidate !== '' && !candidates.includes(candidate)) {
      candidates.push(candidate)
    }
  }
  const probe = typeof options.probe === 'function' ? options.probe : probePython

  const failures = []
  let sawNonEnoent = false

  for (const candidate of candidates) {
    let result
    try {
      result = probe(candidate)
    } catch (err) {
      result = { ok: false, error: err }
    }
    if (result && result.ok) {
      return { python: candidate, candidates }
    }
    const errorCode = result && result.error && result.error.code
    failures.push({
      candidate,
      code: errorCode || null,
      status: result ? result.status ?? null : null,
      message:
        (result && result.error && result.error.message) ||
        (result && result.stderr ? String(result.stderr).trim().slice(0, 300) : ''),
    })
    if (errorCode !== 'ENOENT') {
      sawNonEnoent = true
      break
    }
  }

  const tried = failures
    .map((failure) => {
      if (failure.code) return `${failure.candidate} (${failure.code})`
      if (failure.status !== null) return `${failure.candidate} (exit ${failure.status})`
      return failure.candidate
    })
    .join(', ')
  const lastMessage = failures
    .map((failure) => failure.message)
    .filter(Boolean)
    .slice(-1)[0]
  const code = sawNonEnoent ? 'python_start_failed' : 'python_not_found'
  const hint = 'Set opts.python or the KB_LITE_PYTHON environment variable to a working interpreter.'
  throw new EngineError(
    code,
    `No usable Python interpreter found. Tried: ${tried}. ${hint}` +
      (lastMessage ? ` Last error: ${lastMessage}` : ''),
  )
}

class EngineClient {
  constructor(options = {}) {
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
    this.restartLimit = Number.isFinite(options.restartLimit)
      ? Math.max(0, Math.trunc(options.restartLimit))
      : DEFAULT_RESTART_LIMIT
    this.backoffMs = options.backoffMs
    this.killOnAbort = options.killOnAbort !== false

    this._factory =
      options.restart ??
      options.createTransport ??
      options.start ??
      null

    this._transport = null
    this._attached = null
    this._pending = new Map()
    this._nextId = 1
    this._closed = false
    this._dead = false
    this._restartCount = 0
    this._starting = null
    this._lastError = null
    this._exitHook = null
    // Set when we kill the engine because a tool call was aborted. The exit
    // that follows is expected, so it must not consume a restart attempt.
    this._abortKill = false

    if (options.transport) this._attach(options.transport)
  }

  get isDead() {
    return this._dead
  }

  get restartCount() {
    return this._restartCount
  }

  get pendingCount() {
    return this._pending.size
  }

  get transport() {
    return this._transport
  }

  _attach(transport) {
    if (!transport || typeof transport.write !== 'function') {
      throw new EngineError('engine_unavailable', 'engine transport must implement write(line)')
    }
    this._detach()
    this._transport = transport
    const offLine =
      typeof transport.onLine === 'function'
        ? transport.onLine((line) => this._onLine(line))
        : null
    const offExit =
      typeof transport.onExit === 'function'
        ? transport.onExit((info) => this._onExit(info || {}))
        : null
    this._attached = { transport, offLine, offExit }

    if (!this._exitHook) {
      this._exitHook = () => {
        try {
          const current = this._transport
          if (current && typeof current.kill === 'function') current.kill()
        } catch {
          // best effort on process exit
        }
      }
      process.on('exit', this._exitHook)
    }
  }

  _detach() {
    const attached = this._attached
    if (!attached) return
    this._attached = null
    try {
      if (attached.offLine) attached.offLine()
    } catch {
      // ignore
    }
    try {
      if (attached.offExit) attached.offExit()
    } catch {
      // ignore
    }
  }

  _stderrTail(transport = this._transport) {
    try {
      if (transport && typeof transport.stderrTail === 'function') {
        return String(transport.stderrTail() || '')
      }
    } catch {
      // ignore
    }
    return ''
  }

  _withStderr(message, transport) {
    const tail = this._stderrTail(transport).trim()
    if (!tail) return message
    return `${message}\n--- engine stderr (tail) ---\n${tail.slice(-STDERR_IN_MESSAGE)}`
  }

  _settle(id) {
    const entry = this._pending.get(id)
    if (!entry) return null
    this._pending.delete(id)
    if (entry.timer) clearTimeout(entry.timer)
    if (entry.signal && entry.onAbort) {
      try {
        entry.signal.removeEventListener('abort', entry.onAbort)
      } catch {
        // ignore
      }
    }
    return entry
  }

  _rejectAll(error) {
    for (const id of [...this._pending.keys()]) {
      const entry = this._settle(id)
      if (entry) entry.reject(error)
    }
  }

  _onLine(rawLine) {
    if (this._closed) return
    let text = String(rawLine == null ? '' : rawLine)
    if (text.endsWith('\r')) text = text.slice(0, -1)
    if (text.trim() === '') return

    let message
    try {
      message = JSON.parse(text)
    } catch {
      // Non-JSON noise on stdout is ignored by design.
      return
    }
    if (!message || typeof message !== 'object' || Array.isArray(message)) return

    // Startup/metadata events (e.g. {"event":"ready",...}) are not responses.
    if (typeof message.event === 'string') {
      if (message.event === 'ready') this.ready = message
      return
    }

    if (message.id === undefined || message.id === null) return
    const id = String(message.id)
    const entry = this._pending.get(id)
    if (!entry) return

    if (message.ok === false) {
      const detail = message.error && typeof message.error === 'object' ? message.error : {}
      this._settle(id)
      entry.reject(
        new EngineError(
          typeof detail.code === 'string' && detail.code ? detail.code : 'engine_error',
          typeof detail.message === 'string' && detail.message
            ? detail.message
            : 'engine request failed',
          { detail },
        ),
      )
      return
    }

    // A successful response means the engine is healthy again.
    this._restartCount = 0
    this._settle(id)
    entry.resolve(message.result !== undefined ? message.result : {})
  }

  _onExit(info) {
    if (this._closed) return
    const deadTransport = this._transport
    this._detach()
    this._transport = null

    // An abort deliberately killed the child. This is not a crash: do not
    // auto-restart (that would leave a fresh Python process alive for no
    // reason); the next request will start a new process through the factory.
    if (this._abortKill) {
      this._abortKill = false
      this._rejectAll(
        new EngineError(
          'engine_aborted',
          'engine process was killed because a request was aborted',
        ),
      )
      return
    }

    const detail = info.error
      ? `spawn error: ${info.error.message || info.error}`
      : `exit code=${info.code ?? 'null'} signal=${info.signal ?? 'null'}`
    const message = this._withStderr(
      `engine process exited unexpectedly (${detail})`,
      deadTransport,
    )

    const canRestart =
      typeof this._factory === 'function' && this._restartCount < this.restartLimit

    const pendingError = new EngineError(
      canRestart ? 'engine_restarted' : 'engine_dead',
      message,
    )

    if (canRestart) {
      this._restartCount += 1
      this._startRestart()
    } else {
      this._dead = true
      this._lastError = new EngineError('engine_dead', message)
    }
    this._rejectAll(pendingError)
  }

  _startRestart() {
    const delay = resolveBackoff(this.backoffMs)
    const promise = sleep(delay)
      .then(() => {
        if (this._closed) throw new EngineError('engine_closed', 'engine client is closed')
        return this._factory()
      })
      .then((transport) => {
        if (this._closed) {
          try {
            transport?.kill?.()
          } catch {
            // ignore
          }
          throw new EngineError('engine_closed', 'engine client is closed')
        }
        if (!transport) {
          throw new EngineError('engine_dead', 'engine transport factory returned no transport')
        }
        this._attach(transport)
        return transport
      })
      .catch((err) => {
        this._dead = true
        this._lastError = asEngineError(err, 'engine_dead')
        throw this._lastError
      })
      .finally(() => {
        if (this._starting === promise) this._starting = null
      })
    this._starting = promise
    // Avoid an unhandled rejection when nobody is awaiting a restart.
    promise.catch(() => {})
    return promise
  }

  _startInitial() {
    if (typeof this._factory !== 'function') {
      throw new EngineError(
        'engine_unavailable',
        'engine client has no transport and no restart/createTransport factory',
      )
    }
    if (this._starting) return this._starting
    const promise = Promise.resolve()
      .then(() => {
        if (this._closed) throw new EngineError('engine_closed', 'engine client is closed')
        return this._factory()
      })
      .then((transport) => {
        if (this._closed) {
          try {
            transport?.kill?.()
          } catch {
            // ignore
          }
          throw new EngineError('engine_closed', 'engine client is closed')
        }
        if (!transport) {
          throw new EngineError('engine_start_failed', 'engine transport factory returned no transport')
        }
        this._attach(transport)
        return transport
      })
      .catch((err) => {
        this._dead = true
        this._lastError = asEngineError(err, 'engine_start_failed')
        throw this._lastError
      })
      .finally(() => {
        if (this._starting === promise) this._starting = null
      })
    this._starting = promise
    promise.catch(() => {})
    return promise
  }

  async _ensureTransport() {
    if (this._closed) throw new EngineError('engine_closed', 'engine client is closed')
    if (this._transport) return this._transport

    if (this._starting) {
      await this._starting
      if (this._transport) return this._transport
    }
    if (this._dead) {
      throw this._lastError || new EngineError('engine_dead', 'engine is not available')
    }
    if (typeof this._factory !== 'function') {
      throw new EngineError(
        'engine_unavailable',
        'engine client has no transport and no restart/createTransport factory',
      )
    }
    return this._startInitial()
  }

  /**
   * Send one request. Resolves to the engine's `result` object; rejects with
   * an EngineError (code: engine_timeout, engine_dead, engine_aborted, or the
   * engine's own error code) on failure.
   */
  async request(op, params = {}, options = {}) {
    if (this._closed) throw new EngineError('engine_closed', 'engine client is closed')
    const signal = options.signal
    if (signal && signal.aborted) {
      throw new EngineError('engine_aborted', `request '${op}' was aborted before it was sent`)
    }

    const transport = await this._ensureTransport()
    if (this._closed) throw new EngineError('engine_closed', 'engine client is closed')

    const id = String(this._nextId)
    this._nextId += 1
    const timeoutMs = options.timeoutMs ?? this.timeoutMs

    return new Promise((resolve, reject) => {
      const entry = {
        id,
        op,
        resolve,
        reject,
        timer: null,
        signal: null,
        onAbort: null,
      }
      this._pending.set(id, entry)

      if (Number.isFinite(timeoutMs) && timeoutMs > 0) {
        entry.timer = setTimeout(() => {
          if (!this._pending.has(id)) return
          this._settle(id)
          reject(
            new EngineError(
              'engine_timeout',
              this._withStderr(
                `engine did not answer op '${op}' (id ${id}) within ${timeoutMs} ms`,
                transport,
              ),
            ),
          )
        }, timeoutMs)
      }

      if (signal && typeof signal.addEventListener === 'function') {
        entry.signal = signal
        entry.onAbort = () => {
          if (!this._pending.has(id)) return
          this._settle(id)
          reject(new EngineError('engine_aborted', `request '${op}' (id ${id}) was aborted`))
          if (this.killOnAbort) {
            const current = this._transport
            if (current) {
              this._abortKill = true
              try {
                current.kill?.()
              } catch {
                // ignore
              }
            }
          }
        }
        try {
          signal.addEventListener('abort', entry.onAbort, { once: true })
        } catch {
          entry.onAbort = null
          entry.signal = null
        }
      }

      try {
        transport.write(JSON.stringify({ id, op, params }))
      } catch (err) {
        this._settle(id)
        reject(
          new EngineError(
            'engine_write_failed',
            this._withStderr(
              `failed to send op '${op}' to the engine: ${err && err.message ? err.message : err}`,
              transport,
            ),
            { cause: err },
          ),
        )
      }
    })
  }

  /** Idempotent shutdown: reject in-flight requests and kill the process. */
  async close() {
    if (this._closed) return
    this._closed = true
    this._dead = true
    this._abortKill = false
    this._rejectAll(new EngineError('engine_closed', 'engine client was closed'))

    if (this._exitHook) {
      try {
        process.removeListener('exit', this._exitHook)
      } catch {
        // ignore
      }
      this._exitHook = null
    }

    const transport = this._transport
    this._detach()
    this._transport = null
    if (!transport) return

    try {
      const result = transport.kill?.()
      if (result && typeof result.then === 'function') await result.catch(() => {})
    } catch {
      // ignore
    }

    if (transport.closed && typeof transport.closed.then === 'function') {
      await Promise.race([transport.closed.catch(() => {}), sleep(2_000)])
    }
  }
}

export function createEngineClient(options = {}) {
  return new EngineClient(options)
}
