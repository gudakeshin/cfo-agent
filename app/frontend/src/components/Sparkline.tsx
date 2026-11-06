import type { MetricHistory } from '../api/types'
import { formatMetric } from '../utils/format'

interface Props {
  history: MetricHistory
  width?: number
  height?: number
}

/** Tiny trend line over the quarter. The last point is coloured by whether the
 * latest move was adverse for this metric. */
export function Sparkline({ history, width = 120, height = 32 }: Props) {
  const pts = history.points.filter((p) => p.value != null) as { week: string; value: number }[]
  if (pts.length < 2) return null
  const values = pts.map((p) => p.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const pad = 3
  const x = (i: number) => pad + (i * (width - pad * 2)) / (pts.length - 1)
  const y = (v: number) => height - pad - ((v - min) / span) * (height - pad * 2)
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ')
  const last = pts[pts.length - 1]
  const prev = pts[pts.length - 2]
  const delta = last.value - prev.value
  const adverse =
    history.adverse_direction == null || delta === 0
      ? null
      : history.adverse_direction === 'up'
        ? delta > 0
        : delta < 0
  const tone = adverse == null ? 'neutral' : adverse ? 'adverse' : 'favourable'
  const summary = `${history.label}: ${formatMetric(pts[0].value, history.unit)} in ${pts[0].week} to ${formatMetric(last.value, history.unit)} in ${last.week}`

  return (
    <svg
      className={`sparkline ${tone}`}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={summary}
    >
      <title>{summary}</title>
      <path d={d} fill="none" strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(pts.length - 1)} cy={y(last.value)} r="2.6" />
    </svg>
  )
}
