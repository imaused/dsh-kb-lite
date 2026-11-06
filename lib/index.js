import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { applyPlugin } from './plugin.js'

export const name = 'kb-lite'
export const inject = ['tools']

/** The discipline travels with the plugin so every profile that installs it gets it. */
const SYNC_POLICY = [
  'kb-lite index policy: the knowledge base under KB_LITE_ROOT (or ~/.kb-lite) is derived data;',
  'the memory roots are authoritative. After writing memory files at the end of a session, or before',
  'trusting a search that may predate them, call kb_sync (it ingests only changed files and appends a',
  'sync-log line). kb_stats reports indexed_at / newer_count; kb_search / kb_ask add a warning when the',
  'index is older than the roots. Never treat the index as the source of truth.',
].join(' ')

/**
 * Resolve `defineTool` without a hard top-level dependency on one DSH layout.
 *
 * 1. the normal bare specifier (the host resolves it for profile plugins);
 * 2. the application's bundled copy under `resources/app.asar[.unpacked]`;
 * 3. give up quietly — tools are simply not registered and the host still boots.
 *
 * A static `import { defineTool } from '@deepseek-ai/dsh-tools'` makes a
 * package-layout change or a missing export abort the whole plugin module;
 * this keeps the failure local.
 */
async function loadDefineTool() {
  try {
    const mod = await import('@deepseek-ai/dsh-tools')
    if (typeof mod.defineTool === 'function') return mod.defineTool
  } catch {
    // fall through to the bundled copy
  }
  const resources = process.resourcesPath
  if (typeof resources === 'string' && resources !== '') {
    const relative = ['dsh', 'node_modules', '@deepseek-ai', 'dsh-tools', 'lib', 'index.js']
    for (const prefix of ['app.asar', 'app.asar.unpacked', 'app']) {
      try {
        const entry = pathToFileURL(path.join(resources, prefix, ...relative)).href
        const mod = await import(entry)
        if (typeof mod.defineTool === 'function') return mod.defineTool
      } catch {
        // try the next layout
      }
    }
  }
  return undefined
}

const defineTool = await loadDefineTool()

export function apply(ctx, opts) {
  if (typeof defineTool !== 'function') {
    try {
      ctx?.logger?.warn?.(
        '[kb-lite] @deepseek-ai/dsh-tools not found; tools not registered (host unaffected)',
      )
    } catch {
      // nothing else to do
    }
    return undefined
  }
  let dispose
  try {
    dispose = applyPlugin(ctx, defineTool, opts)
  } catch (error) {
    try {
      ctx?.logger?.warn?.(`[kb-lite] tool registration failed: ${error?.message || error}`)
    } catch {
      // nothing else to do
    }
    return undefined
  }
  try {
    if (ctx && typeof ctx.inject === 'function') {
      ctx.inject(['systemPrompt'], (scope) => {
        const prompt = scope.get('systemPrompt')
        if (!prompt || typeof prompt.section !== 'function') return
        scope.effect(() =>
          prompt.section({
            name: 'kb-lite/sync-policy',
            order: 90,
            interpolate: false,
            text: () => SYNC_POLICY,
          }),
        )
      })
    }
  } catch {
    // Hosts without the systemPrompt service keep working: tools only.
  }
  return dispose
}
