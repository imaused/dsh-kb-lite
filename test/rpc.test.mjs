#!/usr/bin/env node
// Unit tests for lib/rpc.js. No Python process is ever spawned: the engine
// transport is injected, and createChildProcessTransport is exercised with a
// fake child process.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import {
  EngineError,
  createChildProcessTransport,
  createEngineClient,
  createLineSplitter,
  resolvePythonPath,
} from '../lib/rpc.js'

const tick = () => new Promise((resolve) => setImmediate(resolve))
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitFor(predicate, timeoutMs = 2_000, label = 'condition') {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await sleep(1)
  }
  throw new Error(`waitFor timed out: ${label}`)
}

/** In-memory transport matching the client's transport contract. */
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
    emitLine(line) {
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
      assert.equal(line.endsWith('\n'), false, 'transport lines must not end with a newline')
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

class FakeChild extends EventEmitter {
  constructor() {
    super()
    this.stdout = new PassThrough()
    this.stderr = new PassThrough()
    this.stdin = new PassThrough()
    this.pid = 4242
    this.killed = false
  }

  kill() {
    this.killed = true
    this.emit('exit', null, 'SIGTERM')
  }
}

test('line splitter buffers partial lines, tolerates CRLF and flushes the tail', () => {
  const splitter = createLineSplitter()

  assert.deepEqual(splitter.push('{"id":"1","ok":tr'), [])
  assert.deepEqual(splitter.push('ue}\r\n{"id":"2"'), ['{"id":"1","ok":true}'])
  assert.deepEqual(splitter.push(',"ok":false}\n{"partial":'), [
    '{"id":"2","ok":false}',
  ])
  assert.equal(splitter.buffered, '{"partial":')
  assert.deepEqual(splitter.flush(), ['{"partial":'])
  assert.equal(splitter.buffered, '')
})

test('child transport frames partial chunks and decodes UTF-8 split mid-character', async () => {
  const child = new FakeChild()
  const transport = createChildProcessTransport({
    argv: ['python', 'engine.py'],
    cwd: process.cwd(),
    spawnFn: () => child,
  })
  const lines = []
  transport.onLine((line) => lines.push(line))

  const message = { id: '1', ok: true, result: { text: '路径与查询' } }
  const raw = Buffer.from(JSON.stringify(message) + '\n', 'utf8')
  const charStart = raw.indexOf(Buffer.from('路'))
  assert.ok(charStart > 0, 'test data must contain a multi-byte character')
  const cut = charStart + 1 // in the middle of the 3-byte UTF-8 sequence

  child.stdout.write(raw.subarray(0, cut))
  await tick()
  assert.deepEqual(lines, [], 'a partial line must not be emitted')

  child.stdout.write(raw.subarray(cut))
  await tick()
  assert.equal(lines.length, 1)
  assert.deepEqual(JSON.parse(lines[0]), message)

  child.stdout.write('{"a":1}\r\n{"a":2}\n')
  await tick()
  assert.deepEqual(lines.slice(1), ['{"a":1}', '{"a":2}'])

  transport.write('{"id":"2","op":"ping"}')
  assert.equal(child.stdin.read().toString('utf8'), '{"id":"2","op":"ping"}\n')

  child.stderr.write('x'.repeat(9_000))
  await tick()
  child.stderr.write('STDERR-TAIL')
  await tick()
  assert.ok(transport.stderrTail().length <= 8 * 1024)
  assert.ok(transport.stderrTail().endsWith('STDERR-TAIL'))

  let exitInfo = null
  transport.onExit((info) => {
    exitInfo = info
  })
  transport.kill()
  assert.equal(child.killed, true)
  assert.equal(exitInfo.code, null)
  assert.equal(exitInfo.signal, 'SIGTERM')
  assert.throws(() => transport.write('{"id":"3"}'), /exited|writable/)
})

test('client correlates out-of-order responses by id', async () => {
  const transport = makeFakeTransport()
  const client = createEngineClient({ transport, timeoutMs: 1_000 })
  try {
    const first = client.request('stats', { kb_root: 'A' })
    const second = client.request('search', { kb_root: 'A', query: 'q' })
    const third = client.request('ask', { kb_root: 'A', query: 'q' })
    await waitFor(() => transport.requests.length === 3, 2_000, 'three requests')

    const [request1, request2, request3] = transport.requests
    assert.notEqual(request1.id, request2.id)
    assert.notEqual(request2.id, request3.id)

    // Answer in reverse order; correlation must still be exact.
    transport.respond({ id: request3.id, ok: true, result: { evidence: ['third'] } })
    transport.respond({ id: request1.id, ok: true, result: { docs: 1 } })
    transport.respond({ id: request2.id, ok: true, result: { hits: ['second'] } })

    assert.deepEqual(await first, { docs: 1 })
    assert.deepEqual(await second, { hits: ['second'] })
    assert.deepEqual(await third, { evidence: ['third'] })
    assert.equal(client.pendingCount, 0)
  } finally {
    await client.close()
  }
})

test('client ignores non-JSON noise and ready events', async () => {
  const transport = makeFakeTransport()
  const client = createEngineClient({ transport, timeoutMs: 1_000 })
  try {
    transport.handler = (request, respond) => {
      transport.emitLine('Python startup noise, not JSON')
      transport.emitLine('{"event":"ready","version":"0.1.0","dense":false,"numpy":false}')
      transport.emitLine('')
      respond({ id: request.id, ok: true, result: { pong: true } })
    }
    assert.deepEqual(await client.request('ping', {}), { pong: true })
    assert.equal(client.ready.version, '0.1.0')
  } finally {
    await client.close()
  }
})

test('timeout rejects with engine_timeout, keeps the process alive, and later requests work', async () => {
  const transport = makeFakeTransport()
  transport.stderr = 'ENGINE-STDERR-DETAIL'
  const client = createEngineClient({ transport, timeoutMs: 25 })
  try {
    await assert.rejects(client.request('ping', {}), (error) => {
      assert.ok(error instanceof EngineError)
      assert.equal(error.code, 'engine_timeout')
      assert.match(error.message, /within 25 ms/)
      assert.match(error.message, /ENGINE-STDERR-DETAIL/)
      return true
    })
    assert.equal(transport.killed, false, 'a timeout must not kill the engine')
    assert.equal(client.pendingCount, 0, 'timed-out request must be removed')

    transport.handler = (request, respond) => {
      respond({ id: request.id, ok: true, result: { pong: true } })
    }
    assert.deepEqual(await client.request('ping', {}), { pong: true })
  } finally {
    await client.close()
  }
})

test('engine ok:false is propagated as an EngineError with the engine code', async () => {
  const transport = makeFakeTransport()
  const client = createEngineClient({ transport, timeoutMs: 1_000 })
  try {
    transport.handler = (request, respond) => {
      respond({
        id: request.id,
        ok: false,
        error: { code: 'bad_request', message: 'query must not be empty' },
      })
    }
    await assert.rejects(client.request('search', { query: '' }), (error) => {
      assert.equal(error.code, 'bad_request')
      assert.equal(error.message, 'query must not be empty')
      assert.equal(error.detail.code, 'bad_request')
      return true
    })
  } finally {
    await client.close()
  }
})

test('abort rejects the request and kills the engine process', async () => {
  const transport = makeFakeTransport()
  const client = createEngineClient({ transport, timeoutMs: 5_000 })
  try {
    const controller = new AbortController()
    const pending = client.request('search', { query: 'x' }, { signal: controller.signal })
    await waitFor(() => transport.requests.length === 1, 2_000, 'request sent')
    controller.abort()
    await assert.rejects(pending, (error) => error.code === 'engine_aborted')
    assert.equal(transport.killed, true)
    assert.equal(client.pendingCount, 0)
  } finally {
    await client.close()
  }
})

test('abort kill is not counted as a crash and the next request starts a new engine', async () => {
  const transports = []
  const make = () => {
    const transport = makeFakeTransport()
    transport.handler = () => {}
    transports.push(transport)
    return transport
  }
  let factoryCalls = 0
  const client = createEngineClient({
    transport: make(),
    restart: () => {
      factoryCalls += 1
      return make()
    },
    timeoutMs: 1_000,
  })

  try {
    const controller = new AbortController()
    const pending = client.request('search', { query: 'x' }, { signal: controller.signal })
    await waitFor(() => transports[0].requests.length === 1, 2_000, 'first request')
    controller.abort()
    await assert.rejects(pending, (error) => error.code === 'engine_aborted')

    transports[0].exit({ code: null, signal: 'SIGTERM' })
    await waitFor(() => client.transport === null, 2_000, 'transport detached')
    assert.equal(factoryCalls, 0, 'an aborted kill must not trigger an auto-restart')
    assert.equal(client.isDead, false)

    const next = client.request('stats', {})
    await waitFor(() => transports.length === 2, 2_000, 'fresh transport')
    await waitFor(() => transports[1].requests.length === 1, 2_000, 'next request')
    assert.equal(factoryCalls, 1)
    transports[1].respond({
      id: transports[1].requests[0].id,
      ok: true,
      result: { docs: 0 },
    })
    assert.deepEqual(await next, { docs: 0 })
  } finally {
    await client.close()
  }
})

test('crash restarts up to 3 times, then reports engine_dead', async () => {
  const transports = []
  const make = () => {
    const transport = makeFakeTransport()
    transport.handler = () => {}
    transports.push(transport)
    return transport
  }
  const first = make()
  let restarts = 0
  const client = createEngineClient({
    transport: first,
    restart: async () => {
      restarts += 1
      await sleep(1)
      return make()
    },
    backoffMs: 1,
    timeoutMs: 500,
    restartLimit: 3,
  })

  try {
    const pending = client.request('stats', { kb_root: 'A' })
    await waitFor(() => first.requests.length === 1, 2_000, 'first request')
    first.exit({ code: 1 })
    await assert.rejects(pending, (error) => error.code === 'engine_restarted')

    await waitFor(() => transports.length === 2, 2_000, 'first restart')
    assert.equal(restarts, 1)
    assert.equal(client.isDead, false)

    // Three more crashes: restart #2, restart #3, then engine_dead.
    for (let index = 1; index <= 3; index += 1) {
      const current = transports[index]
      await waitFor(() => client.transport === current, 2_000, `transport ${index} attached`)
      current.exit({ code: 1 })
      if (index < 3) {
        await waitFor(
          () => transports.length === index + 2,
          2_000,
          `restart ${index + 1}`,
        )
      }
    }

    assert.equal(restarts, 3, 'exactly three automatic restarts')
    assert.equal(transports.length, 4)
    assert.equal(client.isDead, true)
    await assert.rejects(client.request('stats', {}), (error) => {
      assert.equal(error.code, 'engine_dead')
      assert.match(error.message, /exited unexpectedly/)
      return true
    })
  } finally {
    await client.close()
  }
})

test('close is idempotent and kills the transport exactly once', async () => {
  const transport = makeFakeTransport()
  const client = createEngineClient({ transport })
  await client.close()
  await client.close()
  assert.equal(transport.killed, true)
  assert.equal(transport.killedCount, 1)
  await assert.rejects(client.request('ping', {}), (error) => error.code === 'engine_closed')
})

test('resolvePythonPath prefers opts.python, then KB_LITE_PYTHON, and falls back on ENOENT', () => {
  const seen = []
  const result = resolvePythonPath({
    python: 'py',
    env: { KB_LITE_PYTHON: 'python-from-env' },
    probe: (candidate) => {
      seen.push(candidate)
      if (candidate === 'python3') return { ok: true, status: 0 }
      return { ok: false, error: { code: 'ENOENT' }, status: null }
    },
  })
  assert.equal(result.python, 'python3')
  assert.deepEqual(seen, ['py', 'python3'])
  assert.deepEqual(result.candidates, ['py', 'python3'])

  const envSeen = []
  const envResult = resolvePythonPath({
    env: { KB_LITE_PYTHON: 'python-from-env' },
    probe: (candidate) => {
      envSeen.push(candidate)
      return { ok: true, status: 0 }
    },
  })
  assert.equal(envResult.python, 'python-from-env')
  assert.deepEqual(envSeen, ['python-from-env'])

  const allSeen = []
  assert.throws(
    () =>
      resolvePythonPath({
        env: {},
        probe: (candidate) => {
          allSeen.push(candidate)
          return { ok: false, error: { code: 'ENOENT' }, status: null }
        },
      }),
    (error) => {
      assert.equal(error.code, 'python_not_found')
      assert.match(error.message, /python/)
      assert.match(error.message, /python3/)
      assert.match(error.message, /py/)
      return true
    },
  )
  assert.deepEqual(allSeen, ['python', 'python3', 'py'])

  assert.throws(
    () =>
      resolvePythonPath({
        env: {},
        probe: () => ({ ok: false, status: 1, stderr: 'boom' }),
      }),
    (error) => error.code === 'python_start_failed' && /boom/.test(error.message),
  )
})
