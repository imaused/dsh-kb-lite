#!/usr/bin/env node
// Run every test suite in order and print one summary. Exit code = failures > 0 ? 1 : 0.
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const steps = [
  { name: 'python engine', cmd: 'python', args: ['test/engine_test.py', '-v'], need: 'test/engine_test.py' },
  { name: 'node unit', cmd: process.execPath, args: ['--test', 'test/*.test.mjs'], need: 'test/rpc.test.mjs' },
  { name: 'index smoke', cmd: process.execPath, args: ['test/index.smoke.mjs'], need: 'test/index.smoke.mjs' },
  { name: 'integration (node -> python)', cmd: process.execPath, args: ['test/integration.test.mjs'], need: 'test/integration.test.mjs' },
]

let failed = 0
const results = []
for (const s of steps) {
  if (!existsSync(join(ROOT, s.need))) {
    results.push({ name: s.name, status: 'SKIP (missing ' + s.need + ')' })
    continue
  }
  const t0 = Date.now()
  const r = spawnSync(s.cmd, s.args, { cwd: ROOT, encoding: 'utf8', timeout: 600_000 })
  const ms = Date.now() - t0
  const ok = r.status === 0
  if (!ok) failed++
  results.push({ name: s.name, status: ok ? 'PASS' : 'FAIL', ms, code: r.status })
  if (!ok) {
    console.log(`\n===== ${s.name} FAILED (exit ${r.status}) =====`)
    console.log((r.stdout || '').slice(-4000))
    console.log((r.stderr || '').slice(-4000))
  }
}

console.log('\n===== summary =====')
for (const r of results) console.log(`${r.status.padEnd(28)} ${r.name}${r.ms ? '  ' + r.ms + 'ms' : ''}`)
console.log(failed === 0 ? '\nALL PASS' : `\n${failed} suite(s) failed`)
process.exit(failed === 0 ? 0 : 1)
