#!/usr/bin/env node
// Smoke test for lib/index.js with a stubbed @deepseek-ai/dsh-tools package.
// Copies lib/ into a temp directory, writes a stub host package, imports the
// copied entry point and checks that apply(ctx, opts) registers all five tools.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const LIB = path.join(ROOT, 'lib')

function makeTransport() {
  const lineListeners = new Set()
  const exitListeners = new Set()
  return {
    written: [],
    killed: false,
    write(line) {
      this.written.push(line)
      const request = JSON.parse(line)
      const response = JSON.stringify({
        id: request.id,
        ok: true,
        result: { docs: 0, chunks: 0, kb_root: request.params.kb_root },
      })
      for (const listener of [...lineListeners]) listener(response)
    },
    onLine(cb) {
      lineListeners.add(cb)
      return () => lineListeners.delete(cb)
    },
    onExit(cb) {
      exitListeners.add(cb)
      return () => exitListeners.delete(cb)
    },
    kill() {
      this.killed = true
    },
    emitLine(line) {
      for (const listener of [...lineListeners]) listener(line)
    },
  }
}

const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-kb-lite-smoke-'))

try {
  fs.cpSync(LIB, path.join(temp, 'lib'), { recursive: true })

  fs.writeFileSync(
    path.join(temp, 'package.json'),
    JSON.stringify(
      { name: 'dsh-kb-lite-smoke', private: true, type: 'module', version: '0.0.0' },
      null,
      2,
    ),
    'utf8',
  )

  const stubDir = path.join(temp, 'node_modules', '@deepseek-ai', 'dsh-tools')
  fs.mkdirSync(stubDir, { recursive: true })
  fs.writeFileSync(
    path.join(stubDir, 'package.json'),
    JSON.stringify(
      {
        name: '@deepseek-ai/dsh-tools',
        version: '0.0.0-smoke',
        type: 'module',
        main: 'index.js',
        exports: { '.': './index.js' },
      },
      null,
      2,
    ),
    'utf8',
  )
  fs.writeFileSync(
    path.join(stubDir, 'index.js'),
    'export function defineTool(definition) { return definition }\nexport default { defineTool }\n',
    'utf8',
  )

  const entry = pathToFileURL(path.join(temp, 'lib', 'index.js')).href
  const mod = await import(entry)

  assert.equal(mod.name, 'kb-lite')
  assert.deepEqual(mod.inject, ['tools'])
  assert.equal(typeof mod.apply, 'function')

  const registered = []
  const fakeCtx = {
    tools: {
      register(definition) {
        registered.push(definition)
        return () => {}
      },
    },
    effect(fn) {
      return fn(() => {})
    },
  }

  const transport = makeTransport()
  const dispose = mod.apply(fakeCtx, { transport, timeoutMs: 1_000 })
  assert.equal(typeof dispose, 'function', 'apply(ctx, opts) must return a dispose function')

  const names = registered.map((definition) => definition.name).sort()
  assert.deepEqual(names, ['kb_ask', 'kb_clear', 'kb_ingest', 'kb_search', 'kb_stats'])

  for (const definition of registered) {
    assert.equal(typeof definition.output.render, 'function')
    const blocks = definition.output.render({}, { ok: true })
    assert.equal(Array.isArray(blocks), true)
    assert.equal(blocks[0].type, 'text')
    assert.equal(typeof blocks[0].text, 'string')
  }

  assert.equal(transport.written.length, 0, 'apply() must not contact the engine eagerly')

  const statsTool = registered.find((definition) => definition.name === 'kb_stats')
  const stats = await statsTool.execute({})
  assert.equal(stats.ok, true)
  assert.equal(stats.docs, 0)
  assert.equal(transport.written.length, 1, 'the tool must reach the injected transport')

  await dispose()
  assert.equal(transport.killed, true)

  console.log(`smoke: imported ${path.relative(ROOT, entry)}`)
  console.log(`smoke: registered ${registered.length} tools: ${names.join(', ')}`)
  console.log('smoke: defineTool shape + output.render verified')
  console.log('smoke: dispose() killed the injected transport')
  console.log('PASS test/index.smoke.mjs')
} finally {
  fs.rmSync(temp, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 })
}
