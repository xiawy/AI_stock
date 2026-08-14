/** Shared formatting helpers. */

/** Strip <think>...</think> blocks emitted by reasoning models. */
export function stripThinkTags(text) {
  return String(text || '')
    .replace(/<think>[\s\S]*?<\/think>\s*/g, '')
    .trim()
}

export function formatNumber(n) {
  if (n === null || n === undefined) return '-'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return String(n)
}

/** Map a trading signal to display color + Chinese label. */
export function signalStyle(signal) {
  const s = String(signal || '').toUpperCase()
  if (s.includes('BUY')) return { color: '#22c55e', label: '买入', tag: 'success' }
  if (s.includes('SELL')) return { color: '#ef4444', label: '卖出', tag: 'danger' }
  return { color: '#fbbf24', label: '持有', tag: 'warning' }
}
