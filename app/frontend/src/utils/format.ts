/** Shared formatting for CFO-facing views. */

const inr = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 })

export function formatNumber(v: number, digits = 2): string {
  return new Intl.NumberFormat('en-IN', { maximumFractionDigits: digits }).format(v)
}

/** Paise → "₹3.20 Cr" / "₹45.10 L" / "₹9,500". */
export function formatPaise(paise: number): string {
  const sign = paise < 0 ? '−' : ''
  const abs = Math.abs(paise / 100)
  if (abs >= 1e7) return `${sign}₹${(abs / 1e7).toFixed(2)} Cr`
  if (abs >= 1e5) return `${sign}₹${(abs / 1e5).toFixed(2)} L`
  return `${sign}₹${inr.format(abs)}`
}

export function formatMetric(value: number | null | undefined, unit: string): string {
  if (value == null) return '—'
  switch (unit) {
    case '₹ Cr':
      return `${value < 0 ? '−' : ''}₹${formatNumber(Math.abs(value))} Cr`
    case '%':
      return `${formatNumber(value, 1)}%`
    case 'days':
      return `${formatNumber(value, 1)} days`
    case 'count':
      return formatNumber(value, 0)
    case 'x':
      return `${formatNumber(value, 2)}x`
    default:
      return formatNumber(value)
  }
}

export function formatDelta(delta: number | null | undefined, unit: string): string {
  if (delta == null || delta === 0) return 'no change'
  const sign = delta > 0 ? '+' : '−'
  const abs = Math.abs(delta)
  switch (unit) {
    case '₹ Cr':
      return `${sign}₹${formatNumber(abs)} Cr`
    case '%':
      return `${sign}${formatNumber(abs, 1)} pts`
    case 'days':
      return `${sign}${formatNumber(abs, 1)} days`
    case 'x':
      return `${sign}${formatNumber(abs, 2)}x`
    default:
      return `${sign}${formatNumber(abs)}`
  }
}

export type PriorityTier = 'red' | 'high' | 'medium' | 'low'

/** Same bars as the pipeline's risk-appetite thresholds (0.8 / 1.2 / 2.0). */
export function priorityTier(score: number, isRedLine: boolean): PriorityTier {
  if (isRedLine) return 'red'
  if (score >= 2) return 'high'
  if (score >= 1.2) return 'medium'
  return 'low'
}

export const PRIORITY_LABEL: Record<PriorityTier, string> = {
  red: 'Red line',
  high: 'High priority',
  medium: 'Medium priority',
  low: 'Watch',
}

/** snake_case / camelCase key → "Sentence case". */
export function humanizeKey(key: string): string {
  const s = key
    .replace(/_paise$/, '')
    .replace(/_id$/, '')
    .replace(/_/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** "metric:dso_days:2026-W28" → "dso_days"; null for non-metric ids. */
export function metricIdFromSignal(signalId: string): string | null {
  const parts = signalId.split(':')
  return parts[0] === 'metric' && parts.length >= 2 ? parts[1] : null
}

/** Short, readable label for a citation chip. */
export function citationLabel(cid: string, metricLabels?: Map<string, string>): string {
  const parts = cid.split(':')
  if (parts[0] === 'metric' && parts.length === 3) {
    const label = metricLabels?.get(parts[1]) ?? humanizeKey(parts[1])
    return `${label} · ${parts[2].replace(/^\d{4}-/, '')}`
  }
  if (parts[0] === 'erp' && parts.length === 3) return parts[2]
  return cid
}

/** Safe localStorage wrappers (private windows / blocked storage). */
export function readPref(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writePref(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    /* ignore */
  }
}
