#!/usr/bin/env node
// Unit tests for lib/plugin.js: a fake DSH ctx + in-memory engine transport.
// No Python process and no @deepseek-ai/* package is required.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import os from 'node:os'
import path from 'node:path'

import { applyPlugin } from '../lib/plugin.js'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitFor(predicate, timeoutMs = 2_000, label = 'condition') {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await sleep(1)
  }
  throw new Error(`waitFor timed out: ${label}`)
}

function makeFakeCtx() {
  return {
    tools: {
      registered: [],
      register(definition) {
        this.registered.push(definition)
        return () => {}
      },
    },
    effects: [],
    effect(fn) {
      const cleanup = fn(() => {})
      this.effects.push(cleanup)
      return cleanup
    },
  }
}

function makeFakeTransport() {
  const lineListeners = new Set()
  const exitListeners = new Set()
  const transport = {
    written: [],
    requests: [],
    killed: false,
    killedCount: 0,
    stderr: '',
    handler: null,
    respond(message) {
      const line = typeof message === 'string' ? message : JSON.stringify(message)
      for (const listener of [...lineListeners]) listener(line)
    },
    exit(info = {}) {
      for (const listener of [...exitListeners]) {
        listener({ code: 1, signal: null, error: null, ...info })
      }
    },
    onLine(cb) {
      lineListeners.add(cb)
      return () => lineListeners.delete(cb)
    },
    onExit(cb) {
      exitListeners.add(cb)
      return () => exitListeners.delete(cb)
    },
    write(line) {
      transport.written.push(line)
      const request = JSON.parse(line)
      transport.requests.push(request)
      if (transport.handler) transport.handler(request, (message) => transport.respond(message))
    },
    kill() {
      transport.killed = true
      transport.killedCount += 1
    },
    stderrTail() {
      return transport.stderr
    },
  }
  return transport
}

function installToolset(transport, options = {}) {
  const ctx = makeFakeCtx()
  const dispose = applyPlugin(ctx, (definition) => definition, {
    transport,
    timeoutMs: 1_000,
    ...options,
  })
  const byName = Object.fromEntries(ctx.tools.registered.map((d) => [d.name, d]))
  return { ctx, dispose, byName }
}

function autoRespond(transport, resultByOp = {}) {
  transport.handler = (request, respond) => {
    const result =
      typeof resultByOp[request.op] === 'function'
        ? resultByOp[request.op](request)
        : (resultByOp[request.op] ?? {})
    respond({ id: request.id, ok: true, result })
  }
}

test('registers exactly the five kb tools with host-compatible definitions', () => {
  const transport = makeFakeTransport()
  const { ctx, dispose, byName } = installToolset(transport)

  try {
    const names = ctx.tools.registered.map((definition) => definition.name).sort()
    assert.deepEqual(names, ['kb_ask', 'kb_clear', 'kb_ingest', 'kb_search', 'kb_stats'])

    for (const definition of ctx.tools.registered) {
      assert.deepEqual(Object.keys(definition).sort(), [
        'description',
        'execute',
        'name',
        'output',
        'parameters',
      ])
      assert.equal(typeof definition.description, 'string')
      assert.ok(definition.description.length > 20)
      assert.equal(typeof definition.execute, 'function')
      assert.equal(definition.output.schema.type, 'json')
      assert.equal(typeof definition.output.render, 'function')
      const blocks = definition.output.render({}, { ok: true, hits: [] })
      assert.deepEqual(blocks, [{ type: 'text', text: JSON.stringify({ ok: true, hits: [] }, null, 2) }])
      assert.equal(typeof definition.parameters, 'object')
    }

    // Render must never throw, even for values JSON cannot serialise.
    const circular = {}
    circular.self = circular
    const blocks = byName.kb_stats.output.render({}, circular)
    assert.equal(blocks.length, 1)
    assert.equal(blocks[0].type, 'text')
    assert.equal(typeof blocks[0].text, 'string')
  } finally {
    dispose()
  }
})

test('kb_search forwards {op:"search"} with clamped top_k and default mode', async () => {
  const transport = makeFakeTransport()
  const { byName, dispose } = installToolset(transport)
  try {
    transport.handler = (request, respond) => {
      respond({ id: request.id, ok: true, result: { hits: [] } })
    }

    const high = await byName.kb_search.execute({
      query: '网络共享',
      top_k: 9999,
      kb_root: 'C:/kb/root',
    })
    assert.equal(high.ok, true)
    assert.deepEqual(high.hits, [])
    assert.equal(transport.requests.length, 1)
    assert.equal(transport.requests[0].op, 'search')
    assert.equal(transport.requests[0].params.query, '网络共享')
    assert.equal(transport.requests[0].params.top_k, 50)
    assert.equal(transport.requests[0].params.mode, 'auto')
    assert.equal(transport.requests[0].params.kb_root, path.resolve('C:/kb/root'))

    const low = await byName.kb_search.execute({ query: 'x', top_k: -12, mode: 'bm25' })
    assert.equal(low.ok, true)
    assert.equal(transport.requests[1].params.top_k, 1)
    assert.equal(transport.requests[1].params.mode, 'bm25')
  } finally {
    await dispose()
  }
})

test('kb_root defaults to KB_LITE_ROOT or ~/.kb-lite and opts.kbRoot overrides both', async () => {
  const saved = process.env.KB_LITE_ROOT
  try {
    delete process.env.KB_LITE_ROOT
    const transport = makeFakeTransport()
    const first = installToolset(transport)
    autoRespond(transport, { stats: { docs: 0 } })
    try {
      const result = await first.byName.kb_stats.execute({})
      assert.equal(result.ok, true)
      assert.equal(
        transport.requests[0].params.kb_root,
        path.join(os.homedir(), '.kb-lite'),
      )
    } finally {
      await first.dispose()
    }

    const envRoot = path.join(os.tmpdir(), 'kb-lite-env-root')
    process.env.KB_LITE_ROOT = envRoot
    const envTransport = makeFakeTransport()
    const second = installToolset(envTransport)
    autoRespond(envTransport, { stats: { docs: 0 } })
    try {
      await second.byName.kb_stats.execute({})
      assert.equal(envTransport.requests[0].params.kb_root, path.resolve(envRoot))
    } finally {
      await second.dispose()
    }

    delete process.env.KB_LITE_ROOT
    const explicitRoot = path.join(os.tmpdir(), 'kb-lite-explicit-root')
    const thirdTransport = makeFakeTransport()
    const third = installToolset(thirdTransport, { kbRoot: explicitRoot })
    autoRespond(thirdTransport, { stats: { docs: 0 } })
    try {
      await third.byName.kb_stats.execute({})
      assert.equal(thirdTransport.requests[0].params.kb_root, path.resolve(explicitRoot))
    } finally {
      await third.dispose()
    }
  } finally {
    if (saved === undefined) delete process.env.KB_LITE_ROOT
    else process.env.KB_LITE_ROOT = saved
  }
})

test('kb_ingest forwards protocol params and requires absolute paths', async () => {
  const transport = makeFakeTransport()
  const { byName, dispose } = installToolset(transport, {
    kbRoot: path.join(os.tmpdir(), 'kb-lite-ingest-root'),
  })
  try {
    autoRespond(transport, { ingest: { added: 1, updated: 0, skipped: 0, errors: [], pending: 0, docs: [], elapsed_ms: 1 } })
    const absolute = path.join(os.tmpdir(), 'kb-lite-doc.md')
    const result = await byName.kb_ingest.execute({
      paths: [absolute],
      recursive: false,
      force: true,
      max_files: 9_999,
      with_embeddings: 'off',
    })
    assert.equal(result.ok, true)
    assert.equal(result.added, 1)
    const params = transport.requests[0].params
    assert.deepEqual(params.paths, [absolute])
    assert.equal(params.kb_root, path.resolve(path.join(os.tmpdir(), 'kb-lite-ingest-root')))
    assert.equal(params.recursive, false)
    assert.equal(params.force, true)
    assert.equal(params.max_files, 500)
    assert.equal(params.with_embeddings, 'off')

    const relative = `definitely-missing-${Date.now()}-${Math.random().toString(16).slice(2)}.md`
    const rejected = await byName.kb_ingest.execute({ paths: [relative] })
    assert.equal(rejected.ok, false)
    assert.equal(rejected.error.code, 'path_not_absolute')
    assert.match(rejected.error.message, /must be absolute/)
    assert.ok(rejected.error.message.includes(os.homedir()))
    assert.equal(transport.requests.length, 1, 'relative path must not reach the engine')
  } finally {
    await dispose()
  }
})

test('tool calls return {ok:false,error} instead of throwing when the transport fails', async () => {
  const transport = makeFakeTransport()
  const { byName, dispose } = installToolset(transport)
  try {
    transport.write = () => {
      throw new Error('pipe closed')
    }
    const result = await byName.kb_search.execute({ query: 'anything' })
    assert.equal(result.ok, false)
    assert.equal(result.error.code, 'engine_write_failed')
    assert.match(result.error.message, /pipe closed/)
  } finally {
    await dispose()
  }
})

test('engine ok:false is surfaced as a structured tool error', async () => {
  const transport = makeFakeTransport()
  const { byName, dispose } = installToolset(transport)
  try {
    transport.handler = (request, respond) => {
      respond({
        id: request.id,
        ok: false,
        error: { code: 'ingest_path_missing', message: 'no such path' },
      })
    }
    const result = await byName.kb_ingest.execute({ paths: [path.join(os.tmpdir(), 'nope.md')] })
    assert.equal(result.ok, false)
    assert.deepEqual(result.error, {
      code: 'ingest_path_missing',
      message: 'no such path',
    })
  } finally {
    await dispose()
  }
})

test('kb_clear refuses without confirm and forwards confirm:true when given', async () => {
  const transport = makeFakeTransport()
  const { byName, dispose } = installToolset(transport)
  try {
    autoRespond(transport, { clear: { cleared: true, removed_docs: 0 } })

    const guarded = await byName.kb_clear.execute({})
    assert.equal(guarded.ok, false)
    assert.equal(guarded.error.code, 'confirm_required')
    assert.equal(transport.requests.length, 0, 'unconfirmed clear must not reach the engine')

    const cleared = await byName.kb_clear.execute({ confirm: true })
    assert.equal(cleared.ok, true)
    assert.equal(cleared.cleared, true)
    assert.equal(transport.requests[0].op, 'clear')
    assert.equal(transport.requests[0].params.confirm, true)
  } finally {
    await dispose()
  }
})

test('exec.signal abort cancels the request and kills the engine child', async () => {
  const transport = makeFakeTransport()
  const { byName, dispose } = installToolset(transport)
  try {
    transport.handler = () => {}
    const controller = new AbortController()
    const pending = byName.kb_search.execute(
      { query: 'slow query' },
      { signal: controller.signal },
    )
    await waitFor(() => transport.requests.length === 1, 2_000, 'request sent')
    controller.abort()
    const result = await pending
    assert.equal(result.ok, false)
    assert.equal(result.error.code, 'engine_aborted')
    assert.equal(transport.killed, true)
  } finally {
    await dispose()
  }
})

test('ingest uses the long timeout and dispose() closes the engine', async () => {
  const transport = makeFakeTransport()
  const { byName, dispose } = installToolset(transport, { ingestTimeoutMs: 25 })
  try {
    transport.handler = () => {}
    const timedOut = await byName.kb_ingest.execute({
      paths: [path.join(os.tmpdir(), 'kb-lite-timeout.md')],
    })
    assert.equal(timedOut.ok, false)
    assert.equal(timedOut.error.code, 'engine_timeout')

    assert.equal(typeof dispose, 'function')
    const pendingDispose = dispose()
    assert.ok(pendingDispose && typeof pendingDispose.then === 'function')
    await pendingDispose
    assert.equal(transport.killed, true)
    await dispose() // idempotent
  } finally {
    await dispose()
  }
})

test('kb_ask clamps top_k and forwards max_chars', async () => {
  const transport = makeFakeTransport()
  const { byName, dispose } = installToolset(transport)
  try {
    autoRespond(transport, {
      ask: { evidence: [{ path: 'x.md', text: 'hello' }], total_chars: 5, mode_used: 'bm25' },
    })
    const result = await byName.kb_ask.execute({ query: 'hi', top_k: 500, max_chars: 1234 })
    assert.equal(result.ok, true)
    assert.equal(result.evidence[0].text, 'hello')
    const params = transport.requests[0].params
    assert.equal(params.top_k, 50)
    assert.equal(params.max_chars, 1234)
    assert.equal(params.query, 'hi')
  } finally {
    await dispose()
  }
})
