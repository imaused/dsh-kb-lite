import { defineTool } from '@deepseek-ai/dsh-tools'

import { applyPlugin } from './plugin.js'

export const name = 'kb-lite'
export const inject = ['tools']

export function apply(ctx, opts) {
  return applyPlugin(ctx, defineTool, opts)
}
