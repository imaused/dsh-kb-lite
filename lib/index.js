import { defineTool } from '@deepseek-ai/dsh-tools'

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

export function apply(ctx, opts) {
  const dispose = applyPlugin(ctx, defineTool, opts)
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
