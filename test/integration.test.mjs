#!/usr/bin/env node
// Integration test: Node plugin half <-> real Python engine, through both the RPC layer
// and the registered tool surface. No DSH host required.
//
// Run: node test/integration.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createEngineClient, createChildProcessTransport } from '../lib/rpc.js'
import { applyPlugin } from '../lib/plugin.js'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const ENGINE = join(ROOT, 'engine', 'kb_engine.py')

function makeCorpus() {
  const base = mkdtempSync(join(tmpdir(), 'kb-lite-it-'))
  const docs = join(base, 'docs')
  mkdirSync(docs, { recursive: true })
  writeFileSync(join(docs, 'windows.md'), [
    '# Windows 路径坑',
    '',
    '## UNC 路径',
    '',
    'Windows 下访问网络共享必须用双反斜杠开头的 UNC 路径，例如 \\\\server\\share\\file.txt。',
    '单反斜杠会被当成转义，导致 ENOENT。',
    '',
    '## 长路径',
    '',
    '超过 260 字符的路径需要开启长路径支持，或使用 \\\\?\\ 前缀。',
  ].join('\n'), 'utf8')
  writeFileSync(join(docs, 'python.md'), [
    '# Python venv',
    '',
    '## 创建',
    '',
    'python -m venv .venv 在当前目录创建虚拟环境，激活后才能使用其中的包。',
    '',
    '## 坑',
    '',
    'Windows PowerShell 5.1 下执行 activate.ps1 可能被执行策略拦住，要改用 activate.bat。',
  ].join('\n'), 'utf8')
  writeFileSync(join(docs, 'english.txt'), 'The quick brown fox jumps over the lazy dog. Vector search ranks passages by cosine similarity.\n')
  return { base, docs }
}

test('rpc: ping + stats against the real engine', async () => {
  const t = createChildProcessTransport({ argv: ['python', ENGINE], cwd: ROOT, env: { ...process.env, KB_LITE_DENSE: '0' } })
  const client = createEngineClient({ transport: t, timeoutMs: 30000 })
  try {
    const ping = await client.request('ping', {})
    assert.equal(ping.pong, true)
    const { base } = makeCorpus()
    const stats = await client.request('stats', { kb_root: join(base, '.kb-lite') })
    assert.equal(stats.docs, 0)
  } finally {
    await client.close()
  }
})

test('end-to-end: ingest -> search -> ask -> clear via the engine client', async () => {
  const { base, docs } = makeCorpus()
  const kbRoot = join(base, '.kb-lite')
  const t = createChildProcessTransport({ argv: ['python', ENGINE], cwd: ROOT, env: { ...process.env, KB_LITE_DENSE: '0' } })
  const client = createEngineClient({ transport: t, timeoutMs: 120000 })
  try {
    const ing = await client.request('ingest', { kb_root: kbRoot, paths: [docs] })
    assert.ok(ing.added >= 3, 'expected at least 3 documents, got ' + JSON.stringify(ing))

    const zh = await client.request('search', { kb_root: kbRoot, query: '网络共享 UNC 路径', top_k: 3 })
    assert.equal(zh.hits.length > 0, true, 'no hits for a Chinese query')
    assert.match(zh.hits[0].path.replace(/\\/g, '/'), /windows\.md$/)

    const en = await client.request('search', { kb_root: kbRoot, query: 'cosine similarity ranking', top_k: 3 })
    assert.equal(en.hits.length > 0, true, 'no hits for an English query')
    assert.match(en.hits[0].path.replace(/\\/g, '/'), /english\.txt$/)

    const ask = await client.request('ask', { kb_root: kbRoot, query: 'PowerShell 下 activate 脚本被拦住怎么办', top_k: 2 })
    assert.ok(ask.evidence.length > 0)
    assert.match(ask.evidence[0].text, /activate/)

    const stats = await client.request('stats', { kb_root: kbRoot })
    assert.ok(stats.docs >= 3)
    assert.ok(stats.chunks > 0)

    const cleared = await client.request('clear', { kb_root: kbRoot, confirm: true })
    assert.equal(cleared.cleared, true)
  } finally {
    await client.close()
  }
})

test('end-to-end: the five registered tools work through the tool surface', async () => {
  const { base, docs } = makeCorpus()
  const kbRoot = join(base, '.kb-lite')
  const registered = []
  const fakeCtx = {
    tools: { register(def) { registered.push(def); return () => {} } },
    effect(fn) { const d = fn(() => {}); return d },
  }
  const dispose = applyPlugin(fakeCtx, (def) => def, {
    transport: createChildProcessTransport({ argv: ['python', ENGINE], cwd: ROOT }),
    timeoutMs: 120000,
    kbRoot,
  })
  try {
    assert.deepEqual(registered.map(d => d.name).sort(),
      ['kb_ask', 'kb_clear', 'kb_ingest', 'kb_search', 'kb_stats'])

    const byName = Object.fromEntries(registered.map(d => [d.name, d]))
    const ing = await byName.kb_ingest.execute({ paths: [docs], kb_root: kbRoot })
    assert.equal(ing.ok, true)
    assert.ok(ing.added >= 3)

    const hits = await byName.kb_search.execute({ query: '长路径 260 字符', top_k: 3, kb_root: kbRoot })
    assert.equal(hits.ok, true)
    assert.ok(hits.hits.length > 0)
    assert.match(hits.hits[0].path.replace(/\\/g, '/'), /windows\.md$/)

    const ask = await byName.kb_ask.execute({ query: '创建虚拟环境的命令', top_k: 2, kb_root: kbRoot })
    assert.equal(ask.ok, true)
    assert.ok(ask.evidence.length > 0)

    const st = await byName.kb_stats.execute({ kb_root: kbRoot })
    assert.equal(st.ok, true)
    assert.ok(st.docs >= 3)

    const guard = await byName.kb_clear.execute({ kb_root: kbRoot })
    assert.equal(guard.ok, false, 'clear without confirm must fail')
    const cleared = await byName.kb_clear.execute({ kb_root: kbRoot, confirm: true })
    assert.equal(cleared.ok, true)
  } finally {
    await dispose()
    rmSync(base, { recursive: true, force: true })
  }
})
