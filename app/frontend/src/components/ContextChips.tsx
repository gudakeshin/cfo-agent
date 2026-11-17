import type { SignalContext } from '../api/types'
import { formatMetric } from '../utils/format'

/** Plan gap, covenant proximity, forecast and anomaly flags behind an insight. */
export function ContextChips({ context, unit }: { context?: SignalContext; unit: string }) {
  if (!context) return null
  const chips: { text: string; tone: 'red' | 'amber' | 'muted'; title?: string }[] = []
  const cov = context.covenant
  if (cov) {
    if (cov.breached) chips.push({ text: `Covenant breached (${cov.name})`, tone: 'red' })
    else if (cov.in_buffer) chips.push({ text: `Inside covenant buffer · headroom ${formatMetric(cov.headroom, unit)}`, tone: 'red' })
    else if (cov.projected_breach_in_weeks)
      chips.push({ text: `Forecast to breach covenant in ~${cov.projected_breach_in_weeks} wk`, tone: 'amber',
        title: 'Trend projection (Holt linear smoothing); not a certainty' })
    else chips.push({ text: `Covenant headroom ${formatMetric(cov.headroom, unit)}`, tone: 'muted' })
  }
  const plan = context.plan
  if (plan)
    chips.push({ text: `${plan.gap_pct > 0 ? '+' : ''}${plan.gap_pct.toFixed(1)}% vs plan (${formatMetric(plan.plan, unit)})`,
      tone: plan.adverse ? 'amber' : 'muted' })
  if (context.anomaly?.is_anomaly)
    chips.push({ text: 'Unusual move for this metric', tone: 'amber', title: `Robust z-score ${context.anomaly.z}` })
  const fc = context.forecast?.[context.forecast.length - 1]
  if (fc && !cov?.projected_breach_in_weeks)
    chips.push({ text: `4-week trend: ${formatMetric(fc.value, unit)}`, tone: 'muted',
      title: `80% range ${formatMetric(fc.low, unit)} – ${formatMetric(fc.high, unit)}` })
  if (!chips.length) return null
  return (
    <div className="context-chips">
      {chips.map((c) => (
        <span key={c.text} className={`badge ${c.tone === 'red' ? 'badge-red' : c.tone === 'amber' ? 'badge-amber' : 'badge-muted'}`} title={c.title}>
          {c.text}
        </span>
      ))}
    </div>
  )
}
