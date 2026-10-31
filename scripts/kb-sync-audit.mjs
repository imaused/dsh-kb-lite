#!/usr/bin/env node
// Weekly sync audit for kb-lite (run daily; acts only when >=7 days have passed).
//
// Rule (user, 2026-10-24): every 7 days — when days_since_last_audit >= 7, run the
// check and reset the counter to 0 — look for runs of >=2 consecutive days with no
// sync at all (sync-log.jsonl has one line per kb_sync / kb_ingest).
//
// usage:
//   node scripts/kb-sync-audit.mjs [--kb-root <dir>] [--every-days 7] [--min-gap-days 2]
//                                  [--force] [--json]
// exit codes: 0 = audited & clean, or not due; 2 = audited & gaps found; 1 = error.
import fs from 'node:fs'
import path from 'node:path'

const argv = process.argv.slice(2)
const arg = (name, dflt) => {
  const i = argv.indexOf(name)
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt
}
const has = (name) => argv.includes(name)

const kbRoot = path.resolve(arg('--kb-root', process.env.KB_LITE_ROOT || path.join(process.env.HOME || process.env.USERPROFILE || '.', '.kb-lite')))
const everyDays = Number(arg('--every-days', '7'))
const minGapDays = Number(arg('--min-gap-days', '2'))
const force = has('--force')
const json = has('--json')

const statePath = path.join(kbRoot, 'sync-audit.json')
const logPath = path.join(kbRoot, 'sync-log.jsonl')
const auditLogPath = path.join(kbRoot, 'sync-audit-log.jsonl')

const dayKey = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const dayStart = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
const daysBetween = (a, b) => Math.round((dayStart(b) - dayStart(a)) / 86400000)

function readState() {
  try {
    const parsed = JSON.parse(fs.readFileSync(statePath, 'utf8'))
    if (parsed && typeof parsed === 'object') return parsed
  } catch {}
  return {}
}

function readSyncDays() {
  const days = new Map()
  let text = ''
  try {
    text = fs.readFileSync(logPath, 'utf8')
  } catch {
    return days
  }
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue
    let entry
    try {
      entry = JSON.parse(line)
    } catch {
      continue
    }
    const at = new Date(entry.at)
    if (Number.isNaN(at.getTime())) continue
    const key = dayKey(at)
    days.set(key, (days.get(key) ?? 0) + 1)
  }
  return days
}

/** Runs of >=minGap consecutive calendar days with no sync, between first sync and today. */
function findGaps(days, today) {
  const keys = [...days.keys()].sort()
  if (keys.length === 0) return []
  const gaps = []
  let cursor = dayStart(new Date(`${keys[0]}T00:00:00`))
  const last = dayStart(today)
  let runStart = null
  let runLength = 0
  const flush = () => {
    if (runLength >= minGapDays) {
      gaps.push({ from: dayKey(runStart), days: runLength })
    }
    runStart = null
    runLength = 0
  }
  while (cursor <= last) {
    const key = dayKey(cursor)
    if (days.has(key)) {
      flush()
    } else {
      if (runStart === null) runStart = new Date(cursor)
      runLength += 1
    }
    cursor = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() + 1)
  }
  flush()
  return gaps
}

try {
  const state = readState()
  const now = new Date()
  const lastAudit = state.last_audit_at ? new Date(state.last_audit_at) : null
  const due = force || lastAudit === null || daysBetween(lastAudit, now) >= everyDays

  if (!due) {
    if (json) console.log(JSON.stringify({ ok: true, due: false, days_since_audit: daysBetween(lastAudit, now), next_in_days: everyDays - daysBetween(lastAudit, now) }))
    process.exit(0)
  }

  const days = readSyncDays()
  const gaps = findGaps(days, now)
  const lastSync = [...days.keys()].sort().pop() ?? null
  const result = {
    ok: gaps.length === 0,
    due: true,
    audited_at: now.toISOString(),
    every_days: everyDays,
    min_gap_days: minGapDays,
    sync_days: days.size,
    last_sync_day: lastSync,
    days_since_last_sync: lastSync ? daysBetween(new Date(`${lastSync}T00:00:00`), now) : null,
    gaps,
    counter: 0,
  }

  fs.mkdirSync(kbRoot, { recursive: true })
  fs.writeFileSync(
    statePath,
    JSON.stringify({ last_audit_at: result.audited_at, counter: 0, last_result: { ok: result.ok, gaps: gaps.length } }, null, 2),
  )
  fs.appendFileSync(auditLogPath, `${JSON.stringify(result)}\n`)

  if (json) {
    console.log(JSON.stringify(result, null, 2))
  } else if (gaps.length === 0) {
    console.log(`kb-sync-audit: 7-day check done — ${days.size} sync day(s), no gap >= ${minGapDays} day(s); counter reset to 0`)
  } else {
    console.log(`kb-sync-audit: ${gaps.length} gap(s) of >= ${minGapDays} day(s) without sync:`)
    for (const gap of gaps) console.log(`  missing ${gap.days} day(s) starting ${gap.from}`)
    console.log('counter reset to 0 after this audit')
  }
  process.exit(gaps.length === 0 ? 0 : 2)
} catch (error) {
  console.error(`kb-sync-audit: ${error && error.message ? error.message : error}`)
  process.exit(1)
}
